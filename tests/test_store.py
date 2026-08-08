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
