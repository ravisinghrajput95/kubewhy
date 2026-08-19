"""
Tests for the state that outlives the process.

Both implementations run against the same tests: an in-memory store that
quietly did nothing would let a broken SQLite path pass everything, which is
the failure mode a null object invites.
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import controller
import store


@pytest.fixture(params=["memory", "sqlite"])
def state(request, tmp_path):
    if request.param == "memory":
        return store.MemoryStore()
    return store.SqliteStore(str(tmp_path / "state.db"))


class TestBothImplementationsAgree:
    def test_an_unseen_key_has_no_last_report(self, state):
        assert state.last_reported("demo/web") is None

    def test_a_report_is_remembered(self, state):
        state.record_report("demo/web", 1000.0)
        assert state.last_reported("demo/web") == 1000.0

    def test_reports_are_counted_within_a_window(self, state):
        for at in (100.0, 200.0, 300.0):
            state.record_report(f"demo/{at}", at)

        assert state.reports_since(0) == 3
        assert state.reports_since(250) == 1

    def test_an_undone_report_frees_its_slot(self, state):
        state.record_report("demo/web", 100.0)
        state.undo_report("demo/web", 100.0, None)

        assert state.reports_since(0) == 0
        assert state.last_reported("demo/web") is None

    def test_an_undone_report_restores_the_previous_timestamp(self, state):
        """
        Rolling back must not clear an older cooldown that was still running.

        Deleting the row outright would let the next event report immediately,
        which is the noise the cooldown exists to prevent.
        """
        state.record_report("demo/web", 100.0)
        state.record_report("demo/web", 200.0)
        state.undo_report("demo/web", 200.0, 100.0)

        assert state.last_reported("demo/web") == 100.0

    def test_undo_leaves_a_newer_report_alone(self, state):
        """A slot spent after ours supersedes the refund rather than losing it."""
        state.record_report("demo/web", 100.0)
        state.record_report("demo/web", 200.0)
        state.undo_report("demo/web", 100.0, None)

        assert state.last_reported("demo/web") == 200.0

    def test_undo_frees_exactly_one_slot_at_a_shared_instant(self, state):
        """
        Two workloads recorded at the same instant, one refunded.

        A delete keyed on the timestamp alone would take both and hand back a
        slot nobody spent, quietly raising the hourly ceiling.
        """
        state.record_report("demo/web", 100.0)
        state.record_report("demo/api", 100.0)
        state.undo_report("demo/web", 100.0, None)

        assert state.reports_since(0) == 1
        assert state.last_reported("demo/api") == 100.0

    def test_a_job_round_trips(self, state):
        state.create_job("job-1", "why is crasher failing?", 10.0)
        assert state.get_job("job-1")["state"] == "queued"

        state.update_job("job-1", "done", {"answer": "db refused"}, 20.0)
        job = state.get_job("job-1")

        assert job["state"] == "done"
        assert job["result"] == {"answer": "db refused"}

    def test_an_unknown_job_is_none_not_an_empty_job(self, state):
        assert state.get_job("never-issued") is None

    def test_old_jobs_are_purged(self, state):
        state.create_job("old", "q", 10.0)
        state.create_job("new", "q", 100.0)
        state.purge_jobs(50.0)

        assert state.get_job("old") is None
        assert state.get_job("new") is not None


class TestSurvivingARestart:
    def test_a_cooldown_outlives_the_process(self, tmp_path):
        """
        The defect this exists for: a restart forgot what it had reported, so
        a rollout re-announced every failure in the cluster.
        """
        path = str(tmp_path / "state.db")

        first = controller.Budget(cooldown=1800, state=store.SqliteStore(path))
        assert first.allow("demo/web", now=1_000_000.0) is True

        # A new process, a new store, the same file.
        second = controller.Budget(cooldown=1800, state=store.SqliteStore(path))

        assert second.allow("demo/web", now=1_000_060.0) is False
        assert second.allow("demo/web", now=1_000_000.0 + 1801) is True

    def test_in_memory_forgets_which_is_the_old_behaviour(self):
        first = controller.Budget(cooldown=1800, state=store.MemoryStore())
        first.allow("demo/web", now=1_000_000.0)

        assert controller.Budget(
            cooldown=1800, state=store.MemoryStore()
        ).allow("demo/web", now=1_000_060.0) is True

    def test_the_hourly_ceiling_also_survives(self, tmp_path):
        path = str(tmp_path / "state.db")
        first = controller.Budget(max_per_hour=2, state=store.SqliteStore(path))
        first.allow("a", now=1000.0)
        first.allow("b", now=1001.0)

        second = controller.Budget(max_per_hour=2, state=store.SqliteStore(path))

        assert second.allow("c", now=1002.0) is False


class TestJobApi:
    def test_a_job_is_accepted_without_running_the_model(self):
        import app

        with patch.object(app, "JOBS", store.MemoryStore()), patch.object(
            app, "ask", return_value={"answer": "x", "confidence": "grounded"}
        ):
            client = TestClient(app.app)
            response = client.post("/ask/jobs", json={"question": "why?"})

            assert response.status_code == 202
            assert response.json()["state"] == "queued"

    def test_an_unknown_job_is_a_404(self):
        import app

        with patch.object(app, "JOBS", store.MemoryStore()):
            assert TestClient(app.app).get("/ask/jobs/nope").status_code == 404

    def test_a_job_runs_to_done_and_carries_its_answer(self):
        """
        The round trip the README long claimed did not exist: submit, poll,
        read the answer without having held a connection open.
        """
        import time

        import app

        with patch.object(app, "JOBS", store.MemoryStore()), patch.object(
            app, "ask", return_value={"answer": "db refused", "confidence": "grounded"}
        ):
            client = TestClient(app.app)
            job_id = client.post("/ask/jobs", json={"question": "why?"}).json()["id"]

            # The work runs on a thread, so the state is not "done" the moment
            # the 202 lands -- that is the entire point of the endpoint.
            for _ in range(100):
                job = client.get(f"/ask/jobs/{job_id}").json()
                if job["state"] in ("done", "failed"):
                    break
                time.sleep(0.05)

            assert job["state"] == "done"
            assert job["result"]["answer"] == "db refused"
            assert job["finished_at"]

    def test_a_failing_job_is_readable_rather_than_lost(self):
        """A job that raised must be reportable; silence would leave a poller
        waiting forever on a question that already failed."""
        import time

        import app

        with patch.object(app, "JOBS", store.MemoryStore()), patch.object(
            app, "ask", side_effect=RuntimeError("ollama is down")
        ):
            client = TestClient(app.app)
            job_id = client.post("/ask/jobs", json={"question": "why?"}).json()["id"]

            for _ in range(100):
                job = client.get(f"/ask/jobs/{job_id}").json()
                if job["state"] in ("done", "failed"):
                    break
                time.sleep(0.05)

            assert job["state"] == "failed"
            assert "ollama is down" in job["result"]["error"]


class TestControllerLease:
    """
    Two controllers on one state file deliver every finding twice: dedup is
    keyed on the workload, so a second process has its own idea of what has
    already been reported. A rollout produces exactly that pairing.
    """

    def test_the_first_claim_succeeds(self, tmp_path):
        db = store.SqliteStore(str(tmp_path / "s.db"))

        assert db.claim_lease("pod-a/1", at=1000) is True

    def test_a_second_holder_is_refused_while_the_lease_is_live(self, tmp_path):
        db = store.SqliteStore(str(tmp_path / "s.db"))
        db.claim_lease("pod-a/1", at=1000)

        assert db.claim_lease("pod-b/2", at=1030) is False

    def test_the_holder_can_renew_its_own_lease(self, tmp_path):
        db = store.SqliteStore(str(tmp_path / "s.db"))
        db.claim_lease("pod-a/1", at=1000)

        assert db.claim_lease("pod-a/1", at=1030) is True

    def test_a_stale_lease_is_taken_over(self, tmp_path):
        """
        A SIGKILLed controller never releases anything, so the claim has to
        expire by itself or the next one could never start.
        """
        db = store.SqliteStore(str(tmp_path / "s.db"))
        db.claim_lease("dead-pod/1", at=1000)

        assert db.claim_lease("new-pod/2", at=1000 + 121) is True

    def test_the_lease_survives_a_restart(self, tmp_path):
        path = str(tmp_path / "s.db")
        store.SqliteStore(path).claim_lease("pod-a/1", at=1000)

        assert store.SqliteStore(path).claim_lease("pod-b/2", at=1030) is False

    def test_in_memory_always_holds_the_lease(self):
        """There is no second process to exclude, and the CLI must not block."""
        assert store.MemoryStore().claim_lease("anything", at=0) is True
