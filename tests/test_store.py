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


# A real server, never a fake: the Postgres path exists so two replicas can
# share state, and nothing about that is exercised by a stub. CI runs one as a
# service container, so this is configured there and skipped only on a
# workstation that has not started one -- and the skip says so out loud rather
# than reporting a pass it did not earn.
PG_DSN = os.getenv("TRIAGE_TEST_PG_DSN", "")


@pytest.fixture
def pg_dsn():
    if not PG_DSN:
        pytest.skip("TRIAGE_TEST_PG_DSN is unset; no Postgres to test against")
    return PG_DSN


def _fresh_postgres(dsn):
    """A PostgresStore with the tables emptied, so cases cannot leak into
    each other the way a per-test tmp_path stops the SQLite ones doing."""
    db = store.PostgresStore(dsn)
    with db._pool.connection() as connection:
        connection.execute("TRUNCATE reports, emissions, lease, jobs")
    return db


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def state(request, tmp_path):
    # One generator, so every branch yields: mixing `return` and `yield` in a
    # fixture makes the returning branches raise "did not yield a value".
    if request.param == "memory":
        yield store.MemoryStore()
    elif request.param == "sqlite":
        yield store.SqliteStore(str(tmp_path / "state.db"))
    else:
        if not PG_DSN:
            pytest.skip("TRIAGE_TEST_PG_DSN is unset; no Postgres to test against")
        db = _fresh_postgres(PG_DSN)
        yield db
        db.close()


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


class TestListingRecentInvestigations:
    """
    `list_jobs` exists for the operator console's Recent panel. Read-only and
    additive: every other consumer already had one job id in hand.
    """

    def _both(self, tmp_path):
        return [store.MemoryStore(), store.SqliteStore(str(tmp_path / "s.db"))]

    def test_newest_first(self, tmp_path):
        for s in self._both(tmp_path):
            s.create_job("a", "why is web down?", at=100.0)
            s.create_job("b", "what is broken?", at=300.0)
            s.create_job("c", "check the nodes", at=200.0)

            assert [j["id"] for j in s.list_jobs()] == ["b", "c", "a"]

    def test_the_limit_is_honoured(self, tmp_path):
        for s in self._both(tmp_path):
            for i in range(30):
                s.create_job(f"j{i}", f"q{i}", at=float(i))

            assert len(s.list_jobs(limit=5)) == 5

    def test_an_empty_store_lists_nothing(self, tmp_path):
        for s in self._both(tmp_path):
            assert s.list_jobs() == []

    def test_state_and_question_are_carried(self, tmp_path):
        for s in self._both(tmp_path):
            s.create_job("a", "why is crasher failing?", at=1.0)
            s.update_job("a", "done", result={"answer": "x"}, at=2.0)

            job = s.list_jobs()[0]

            assert job["question"] == "why is crasher failing?"
            assert job["state"] == "done"

    def test_a_listing_does_not_carry_result_bodies(self, tmp_path):
        """
        A listing is a list. The stored result of a deep investigation is tens
        of kilobytes, and the sidebar needs a question and a state.
        """
        s = store.SqliteStore(str(tmp_path / "s.db"))
        s.create_job("a", "q", at=1.0)
        s.update_job("a", "done", result={"answer": "x" * 5000}, at=2.0)

        assert "result" not in s.list_jobs()[0]


class TestJobsInterruptedByARestart:
    """
    Persistence made this visible rather than causing it.

    Without a state file a job that was `running` when the process died simply
    vanished, and the caller polling for it got a 404 and knew to ask again.
    With one it survives, still marked `running`, with no thread anywhere that
    will ever finish it -- so the caller polls an investigation that cannot
    complete. That is worse than the 404 it replaced.
    """

    def test_a_running_job_is_failed_with_a_reason(self, state):
        state.create_job("j1", "why?", 100.0)
        state.update_job("j1", "running")

        assert state.fail_interrupted(200.0) == 1

        job = state.get_job("j1")
        assert job["state"] == "failed"
        assert "restarted" in job["result"]["error"]
        assert job["finished_at"] == 200.0

    def test_a_queued_job_is_failed_too(self, state):
        """
        It never started, so nothing will start it. Queued is as stranded as
        running and only looks less so.
        """
        state.create_job("j1", "why?", 100.0)

        assert state.fail_interrupted(200.0) == 1
        assert state.get_job("j1")["state"] == "failed"

    def test_a_finished_job_is_left_alone(self, state):
        state.create_job("j1", "why?", 100.0)
        state.update_job("j1", "done", {"answer": "because"}, 150.0)

        assert state.fail_interrupted(200.0) == 0

        job = state.get_job("j1")
        assert job["state"] == "done"
        assert job["result"] == {"answer": "because"}
        assert job["finished_at"] == 150.0

    def test_an_already_failed_job_is_not_rewritten(self, state):
        """
        Its own error is why it failed; replacing that with "the process
        restarted" would erase the only diagnosis anyone has.
        """
        state.create_job("j1", "why?", 100.0)
        state.update_job("j1", "failed", {"error": "ollama refused"}, 150.0)

        state.fail_interrupted(200.0)
        assert state.get_job("j1")["result"]["error"] == "ollama refused"

    def test_nothing_to_do_is_not_an_error(self, state):
        assert state.fail_interrupted(200.0) == 0

    def test_several_at_once(self, state):
        for job_id in ("j1", "j2", "j3"):
            state.create_job(job_id, "why?", 100.0)
        state.update_job("j2", "running")
        state.update_job("j3", "done", {"answer": "x"}, 150.0)

        assert state.fail_interrupted(200.0) == 2

    def test_the_reason_tells_the_caller_what_to_do(self, state):
        """
        "failed" alone reads as a broken investigation. It was not broken; it
        was interrupted, and asking again will work.
        """
        state.create_job("j1", "why?", 100.0)
        state.update_job("j1", "running")
        state.fail_interrupted(200.0)

        assert "Ask again" in state.get_job("j1")["result"]["error"]


class TestTwoReplicasShareOneStore:
    """
    The property that makes `replicas > 1` possible at all.

    None of this can be tested against SQLite, which is the point: these cases
    exist because a shared state file was the thing standing between kubewhy
    and a second replica, and a single-writer store cannot demonstrate its own
    replacement.
    """

    def test_one_replica_sees_what_another_recorded(self, pg_dsn):
        a = _fresh_postgres(pg_dsn)
        b = store.PostgresStore(pg_dsn)
        try:
            a.record_report("demo/web", at=1000)
            assert b.last_reported("demo/web") == 1000
        finally:
            a.close()
            b.close()

    def test_the_hourly_ceiling_is_shared_not_per_replica(self, pg_dsn):
        # Two replicas each reporting twice must spend four of one budget, not
        # two of two budgets -- otherwise scaling out multiplies the noise the
        # ceiling exists to cap.
        a = _fresh_postgres(pg_dsn)
        b = store.PostgresStore(pg_dsn)
        try:
            a.record_report("demo/one", at=1000)
            a.record_report("demo/two", at=1001)
            b.record_report("demo/three", at=1002)
            b.record_report("demo/four", at=1003)
            assert a.reports_since(0) == 4
            assert b.reports_since(0) == 4
        finally:
            a.close()
            b.close()

    def test_only_one_replica_holds_the_lease(self, pg_dsn):
        a = _fresh_postgres(pg_dsn)
        b = store.PostgresStore(pg_dsn)
        try:
            assert a.claim_lease("pod-a/1", at=1000) is True
            assert b.claim_lease("pod-b/2", at=1030) is False
        finally:
            a.close()
            b.close()

    def test_the_lease_passes_on_when_the_holder_stops_renewing(self, pg_dsn):
        a = _fresh_postgres(pg_dsn)
        b = store.PostgresStore(pg_dsn)
        try:
            a.claim_lease("pod-a/1", at=1000)
            # Past the ttl: the holder is gone and did not say so.
            assert b.claim_lease("pod-b/2", at=1000 + 121) is True
        finally:
            a.close()
            b.close()

    def test_a_racing_claim_produces_exactly_one_winner(self, pg_dsn):
        """
        The check and the claim must happen inside one statement.

        Read-then-write across two round trips is the obvious way to write
        this and it is wrong: both replicas read a free lease, both write, and
        both proceed to announce every finding. Twelve threads against one row
        is the cheapest way to make that failure show up.
        """
        import concurrent.futures

        _fresh_postgres(pg_dsn).close()
        holders = [f"pod-{i}/1" for i in range(12)]

        def claim(holder):
            db = store.PostgresStore(pg_dsn)
            try:
                return db.claim_lease(holder, at=2000)
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(claim, holders))

        assert sum(results) == 1, (
            f"{sum(results)} replicas believed they held the lease; "
            "the claim is not atomic"
        )


class TestOneReplicaDoesNotFailAnothersWork:
    """
    `fail_interrupted` without an owner is correct for exactly one writer and
    destructive for more than one: a pod restarting would close out its live
    siblings' investigations, telling the caller their question was
    interrupted by a restart that happened to a different process.
    """

    def test_an_owner_closes_out_only_its_own(self, pg_dsn):
        db = _fresh_postgres(pg_dsn)
        try:
            db.create_job("mine", "q1", at=1, owner="pod-a")
            db.create_job("theirs", "q2", at=2, owner="pod-b")

            assert db.fail_interrupted(at=10, owner="pod-a") == 1

            assert db.get_job("mine")["state"] == "failed"
            assert db.get_job("theirs")["state"] == "queued", (
                "a restarting replica failed a job belonging to a live one"
            )
        finally:
            db.close()

    def test_without_an_owner_it_still_closes_everything(self, pg_dsn):
        # The single-replica and CLI paths must keep the behaviour they had.
        db = _fresh_postgres(pg_dsn)
        try:
            db.create_job("a", "q", at=1, owner="pod-a")
            db.create_job("b", "q", at=2, owner=None)
            assert db.fail_interrupted(at=10) == 2
        finally:
            db.close()

    def test_the_sqlite_twin_scopes_by_owner_too(self, tmp_path):
        # Same contract, so the two paths cannot drift.
        db = store.SqliteStore(str(tmp_path / "s.db"))
        db.create_job("mine", "q1", at=1, owner="pod-a")
        db.create_job("theirs", "q2", at=2, owner="pod-b")
        assert db.fail_interrupted(at=10, owner="pod-a") == 1
        assert db.get_job("theirs")["state"] == "queued"

    def test_the_memory_twin_scopes_by_owner_too(self):
        db = store.MemoryStore()
        db.create_job("mine", "q1", at=1, owner="pod-a")
        db.create_job("theirs", "q2", at=2, owner="pod-b")
        assert db.fail_interrupted(at=10, owner="pod-a") == 1
        assert db.get_job("theirs")["state"] == "queued"
