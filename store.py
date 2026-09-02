"""
State that outlives the process: controller dedup, and /ask jobs.

These were one problem wearing two hats. The controller forgot what it had
already reported whenever it restarted, so a rollout re-announced every
failure in the cluster; /ask held a request open for the whole diagnosis
because there was nowhere to put a result the caller could come back for. Both
wanted the same thing -- somewhere to write down a small fact and read it back
later.

**Not the cluster.** A ConfigMap or a Lease would need write RBAC, and this
project's first rule is that nothing it does can change a cluster. Being able
to write state is exactly the capability the read-only guarantee exists to
withhold, so the store is local: SQLite from the standard library, no new
dependency, no service to run.

    TRIAGE_STATE_DB=/var/lib/kubewhy/state.db

Unset, everything runs in memory and behaves as it always did -- which is what
you want for the CLI, where a database file for a single question would be
absurd.

**On more than one replica.** A file gets you restart survival, not a second
replica: two pods with two files is two of everything, and two pods sharing
one RWX volume is SQLite over network storage, which is a good way to corrupt
it. The chart still pins one replica. What changes is that the interface below
is the seam -- a Redis or Postgres implementation slots in without touching
the controller or the API, and the restart case, which is the one that
actually bites, is fixed today.

Wall clock throughout, never monotonic: a monotonic timestamp means nothing to
the next process, and persisting one would have the restart read every
cooldown as expired or eternal depending on uptime.
"""

import json
import os
import sqlite3
import threading
import time

# A job in one of these states has a thread behind it, or had one. After a
# restart it has neither, and nothing will move it along.
_UNFINISHED = ("queued", "running")

_INTERRUPTED = (
    "the process restarted while this investigation was running; it was not "
    "resumed. Ask again."
)
import uuid

STATE_DB = os.getenv("TRIAGE_STATE_DB", "")

# Jobs are answers to questions somebody asked minutes ago, not records.
JOB_TTL_SECONDS = int(os.getenv("TRIAGE_JOB_TTL", str(24 * 3600)))


class MemoryStore:
    """
    The default, and the whole behaviour before any of this existed.

    Kept as a real implementation rather than a null object so the two paths
    are exercised by the same tests: an in-memory store that silently did
    nothing would let a broken SQLite path pass everything.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last = {}
        self._emissions = []
        self._jobs = {}

    def last_reported(self, key):
        with self._lock:
            return self._last.get(key)

    def record_report(self, key, at):
        with self._lock:
            self._last[key] = at
            self._emissions.append(at)

    def reports_since(self, cutoff):
        with self._lock:
            self._emissions = [t for t in self._emissions if t >= cutoff]
            return len(self._emissions)

    def claim_lease(self, holder, at, ttl=120):
        """
        In memory there is no second process to exclude; the lease is always
        this one's. Present so the controller can call it unconditionally.
        """
        return True

    def undo_report(self, key, at, previous):
        with self._lock:
            # Any emission with this timestamp will do -- they are counted,
            # never identified, so removing "some row at this instant" and
            # removing "our row" are the same operation.
            try:
                self._emissions.remove(at)
            except ValueError:
                pass
            # Only if the slot is still ours. With a zero cooldown the same
            # key can be spent again before we get here, and restoring the
            # older value would then suppress a report that was legitimately
            # allowed after ours.
            if self._last.get(key) != at:
                return
            if previous is None:
                self._last.pop(key, None)
            else:
                self._last[key] = previous

    def create_job(self, job_id, question, at, owner=None):
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "state": "queued",
                "question": question,
                "created_at": at,
                "finished_at": None,
                "result": None,
                "owner": owner,
            }

    def update_job(self, job_id, state, result=None, at=None):
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["state"] = state
                job["result"] = result
                job["finished_at"] = at

    def get_job(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list_jobs(self, limit=25):
        """
        Recent investigations, newest first, without their result bodies.

        Read-only and additive: the operator console needs a list of what has
        been asked so someone can go back to a diagnosis, and every other
        consumer of this store already had one job id in hand. Nothing about
        how an investigation runs changes.

        The result is omitted deliberately -- a listing is a list, and a deep
        investigation's stored result is tens of kilobytes.
        """
        with self._lock:
            rows = sorted(self._jobs.values(),
                          key=lambda j: j.get("created_at") or 0, reverse=True)
            return [{k: v for k, v in row.items() if k != "result"}
                    for row in rows[:limit]]

    def purge_jobs(self, cutoff):
        with self._lock:
            for job_id in [
                k for k, v in self._jobs.items() if v["created_at"] < cutoff
            ]:
                del self._jobs[job_id]

    def fail_interrupted(self, at, owner=None):
        """See the SQLite twin. Always zero here: memory died with the process."""
        with self._lock:
            interrupted = [j for j in self._jobs.values()
                           if j["state"] in _UNFINISHED
                           and (owner is None or j.get("owner") == owner)]
            for job in interrupted:
                job["state"] = "failed"
                job["result"] = {"error": _INTERRUPTED}
                job["finished_at"] = at
        return len(interrupted)


class SqliteStore:
    """
    The same store, on disk, so a restart remembers.

    A connection per call rather than one shared handle: SQLite's own locking
    is what makes concurrent access safe, and a long-lived connection shared
    across the watch thread, the worker and the API is how you get
    "database is locked" under exactly the load that matters.
    """

    def __init__(self, path):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._setup()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        # WAL so a reader is never blocked by the writer -- the API answering
        # a status poll must not wait on the controller recording a finding.
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _setup(self):
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reports "
                "(key TEXT PRIMARY KEY, last_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS emissions (at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS lease "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), holder TEXT NOT NULL,"
                " renewed_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "id TEXT PRIMARY KEY, state TEXT NOT NULL, question TEXT NOT NULL,"
                "created_at REAL NOT NULL, finished_at REAL, result TEXT,"
                "owner TEXT)"
            )
            # Added when replicas became possible. A state file written before
            # that has every other column and not this one, and the first
            # query naming it would fail on a database that is otherwise fine.
            # SQLite has no ADD COLUMN IF NOT EXISTS, so ask first.
            columns = {row["name"] for row in
                       connection.execute("PRAGMA table_info(jobs)")}
            if "owner" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")

    def claim_lease(self, holder, at, ttl=120):
        """
        Whether this process may act as *the* controller for this state file.

        Two controllers sharing a state DB is not a hypothetical: a rollout
        overlaps old and new pods, and both watch the same cluster. Dedup is
        keyed on the workload, so two of them means every finding is delivered
        twice -- the exact noise the cooldown and hourly ceiling exist to stop,
        reintroduced by the deployment doing its normal job.

        A row with a TTL rather than a file lock: a file lock dies with the
        process and says nothing about a controller that was SIGKILLed, while a
        stale timestamp expires on its own. The holder renews as it works, so a
        live controller keeps its claim and a dead one loses it after `ttl`.

        Advisory. It cannot stop a second controller that ignores the answer,
        which is the same guarantee a Lease gives in-cluster, and it is honest
        about the single-writer design rather than pretending SQLite over an
        RWX volume would be safe.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT holder, renewed_at FROM lease WHERE id = 1"
            ).fetchone()

            if row and row["holder"] != holder and at - row["renewed_at"] < ttl:
                return False

            connection.execute(
                "INSERT INTO lease (id, holder, renewed_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET holder = ?, renewed_at = ?",
                (holder, at, holder, at),
            )
        return True

    def last_reported(self, key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_at FROM reports WHERE key = ?", (key,)
            ).fetchone()
        return row["last_at"] if row else None

    def record_report(self, key, at):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO reports (key, last_at) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET last_at = excluded.last_at",
                (key, at),
            )
            connection.execute("INSERT INTO emissions (at) VALUES (?)", (at,))

    def reports_since(self, cutoff):
        with self._connect() as connection:
            connection.execute("DELETE FROM emissions WHERE at < ?", (cutoff,))
            row = connection.execute("SELECT COUNT(*) AS n FROM emissions").fetchone()
        return row["n"]

    def undo_report(self, key, at, previous):
        with self._connect() as connection:
            # By rowid, so exactly one row goes even if two workloads were
            # recorded at the same instant. A bare DELETE ... WHERE at = ?
            # would take both and hand back a slot nobody spent.
            connection.execute(
                "DELETE FROM emissions WHERE rowid = "
                "(SELECT rowid FROM emissions WHERE at = ? LIMIT 1)",
                (at,),
            )
            # The last_at guard is the same "only if still ours" check the
            # memory store makes: a newer report for this key supersedes the
            # refund rather than being rolled back by it.
            if previous is None:
                connection.execute(
                    "DELETE FROM reports WHERE key = ? AND last_at = ?", (key, at)
                )
            else:
                connection.execute(
                    "UPDATE reports SET last_at = ? WHERE key = ? AND last_at = ?",
                    (previous, key, at),
                )

    def create_job(self, job_id, question, at, owner=None):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs (id, state, question, created_at, owner) "
                "VALUES (?,?,?,?,?)",
                (job_id, "queued", question, at, owner),
            )

    def update_job(self, job_id, state, result=None, at=None):
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET state = ?, result = ?, finished_at = ? WHERE id = ?",
                (state, json.dumps(result) if result is not None else None, at, job_id),
            )

    def get_job(self, job_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if not row:
            return None
        job = dict(row)
        job["result"] = json.loads(job["result"]) if job["result"] else None
        return job

    def list_jobs(self, limit=25):
        """Recent investigations, newest first. See the in-memory twin."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, state, question, created_at, finished_at FROM jobs "
                "ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def purge_jobs(self, cutoff):
        with self._connect() as connection:
            connection.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))

    def fail_interrupted(self, at, owner=None):
        """
        Close out jobs this process was running when it died. Returns how many.

        `owner` narrows it to one replica's jobs; see the Postgres twin for
        why that matters the moment there is more than one writer. On a
        single-writer state file the default of None is the whole story.

        Called at startup, and it exists because persistence made a bug
        visible rather than causing one. Without a state file a job that was
        `running` when the pod restarted simply vanished, and the caller
        polling for it got a 404 and knew to ask again. With one it survives
        -- still marked `running`, with no thread anywhere that will ever
        finish it. The caller polls an investigation that cannot complete,
        which is worse than the 404 it replaced.

        Nothing here can resume the work: the thread is gone and the question
        is not idempotent to re-run silently. Marking it failed with a reason
        is the honest close, and re-asking is the caller's decision.
        """
        with self._connect() as connection:
            marks = ",".join("?" * len(_UNFINISHED))
            owned = "" if owner is None else " AND owner = ?"
            extra = () if owner is None else (owner,)
            rows = connection.execute(
                f"SELECT id FROM jobs WHERE state IN ({marks}){owned}",
                (*_UNFINISHED, *extra),
            ).fetchall()
            if rows:
                connection.execute(
                    f"UPDATE jobs SET state = 'failed', result = ?, "
                    f"finished_at = ? WHERE state IN ({marks}){owned}",
                    (json.dumps({"error": _INTERRUPTED}), at,
                     *_UNFINISHED, *extra),
                )
        return len(rows)


class PostgresStore:
    """
    The same store again, in a database two processes can share.

    This is the seam the runbook pointed at. Everything above `store.py` --
    the controller's dedup, the hourly ceiling, the lease, `/ask/jobs` -- was
    already written against one process holding one SQLite file, and none of
    it changes here. What changes is that the file stops being the reason
    there can only be one process.

    **SQLite on an RWX volume is not the alternative.** Two writers over a
    shared filesystem corrupt it; that is why `ui.replicas > 1` was refused
    rather than documented as risky.

    A pool rather than SqliteStore's connection-per-call: there the cost was
    opening a local file, here it is a TCP connect and an auth round trip on
    every dedup check. A long-lived shared connection is still wrong for the
    reason the SQLite twin gives -- the watch thread, the worker and the API
    all touch this concurrently -- so the pool hands each caller its own.
    """

    def __init__(self, dsn, min_size=1, max_size=8):
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise RuntimeError(
                "TRIAGE_STATE_DB is a postgresql:// DSN but psycopg is not "
                "installed. It ships in the container image; a checkout needs "
                "`pip install 'psycopg[binary,pool]'`."
            ) from exc

        self.dsn = dsn
        self._pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size,
                                    open=True, kwargs={"autocommit": False})
        self._setup()

    def close(self):
        self._pool.close()

    def _setup(self):
        with self._pool.connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reports "
                "(key TEXT PRIMARY KEY, last_at DOUBLE PRECISION NOT NULL)"
            )
            # A surrogate key because emissions are counted, never identified,
            # and `undo_report` must delete exactly one of two rows written at
            # the same instant. SQLite got this from its implicit rowid.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS emissions "
                "(id BIGSERIAL PRIMARY KEY, at DOUBLE PRECISION NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS lease "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), holder TEXT NOT NULL,"
                " renewed_at DOUBLE PRECISION NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "id TEXT PRIMARY KEY, state TEXT NOT NULL, question TEXT NOT NULL,"
                "created_at DOUBLE PRECISION NOT NULL, finished_at DOUBLE PRECISION,"
                "result TEXT, owner TEXT)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_created_at ON jobs (created_at DESC)"
            )

    def claim_lease(self, holder, at, ttl=120):
        """
        The SQLite twin's read-then-write, collapsed into one statement.

        Two controllers racing is the case this exists for, so the check and
        the claim cannot be two round trips with a gap between them: both
        would read a free lease and both would take it. `ON CONFLICT DO UPDATE
        ... WHERE` makes the decision inside the row lock, and `RETURNING`
        reports whether it happened -- no row back means somebody else holds a
        claim that has not expired.
        """
        with self._pool.connection() as connection:
            row = connection.execute(
                "INSERT INTO lease (id, holder, renewed_at) VALUES (1, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET holder = EXCLUDED.holder, "
                "renewed_at = EXCLUDED.renewed_at "
                "WHERE lease.holder = EXCLUDED.holder "
                "   OR EXCLUDED.renewed_at - lease.renewed_at >= %s "
                "RETURNING holder",
                (holder, at, ttl),
            ).fetchone()
        return row is not None

    def last_reported(self, key):
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT last_at FROM reports WHERE key = %s", (key,)
            ).fetchone()
        return row[0] if row else None

    def record_report(self, key, at):
        with self._pool.connection() as connection:
            connection.execute(
                "INSERT INTO reports (key, last_at) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET last_at = EXCLUDED.last_at",
                (key, at),
            )
            connection.execute("INSERT INTO emissions (at) VALUES (%s)", (at,))

    def reports_since(self, cutoff):
        with self._pool.connection() as connection:
            connection.execute("DELETE FROM emissions WHERE at < %s", (cutoff,))
            row = connection.execute("SELECT count(*) FROM emissions").fetchone()
        return row[0]

    def undo_report(self, key, at, previous):
        with self._pool.connection() as connection:
            connection.execute(
                "DELETE FROM emissions WHERE id = "
                "(SELECT id FROM emissions WHERE at = %s LIMIT 1)",
                (at,),
            )
            # "Only if still ours", exactly as the other two: a newer report
            # for this key supersedes the refund rather than being undone by it.
            if previous is None:
                connection.execute(
                    "DELETE FROM reports WHERE key = %s AND last_at = %s", (key, at)
                )
            else:
                connection.execute(
                    "UPDATE reports SET last_at = %s WHERE key = %s AND last_at = %s",
                    (previous, key, at),
                )

    def create_job(self, job_id, question, at, owner=None):
        with self._pool.connection() as connection:
            connection.execute(
                "INSERT INTO jobs (id, state, question, created_at, owner) "
                "VALUES (%s,%s,%s,%s,%s)",
                (job_id, "queued", question, at, owner),
            )

    def update_job(self, job_id, state, result=None, at=None):
        with self._pool.connection() as connection:
            connection.execute(
                "UPDATE jobs SET state = %s, result = %s, finished_at = %s "
                "WHERE id = %s",
                (state, json.dumps(result) if result is not None else None,
                 at, job_id),
            )

    def get_job(self, job_id):
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT id, state, question, created_at, finished_at, result, owner "
                "FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
        if not row:
            return None
        job = dict(zip(("id", "state", "question", "created_at", "finished_at",
                        "result", "owner"), row, strict=True))
        job["result"] = json.loads(job["result"]) if job["result"] else None
        return job

    def list_jobs(self, limit=25):
        with self._pool.connection() as connection:
            rows = connection.execute(
                "SELECT id, state, question, created_at, finished_at FROM jobs "
                "ORDER BY created_at DESC LIMIT %s", (limit,)
            ).fetchall()
        return [dict(zip(("id", "state", "question", "created_at", "finished_at"),
                         row, strict=True)) for row in rows]

    def purge_jobs(self, cutoff):
        with self._pool.connection() as connection:
            connection.execute("DELETE FROM jobs WHERE created_at < %s", (cutoff,))

    def fail_interrupted(self, at, owner=None):
        """
        Close out interrupted jobs -- **this replica's**, when one is named.

        The single-writer twins can fail every unfinished job at startup,
        because the only process that could have been running one was the one
        that just died. That is false the moment a second replica exists: a
        pod restarting would mark its live siblings' investigations `failed`
        while their threads are still working, and the caller polling one
        would be told it was interrupted by a restart that happened to a
        different pod.

        So a replica closes out its own. `owner=None` keeps the old behaviour
        for the single-replica and CLI paths, where there is nothing else to
        confuse it with. Jobs owned by a replica that never comes back are
        left for `purge_jobs`; wrongly failing a live investigation is worse
        than a stale row that expires on its own.
        """
        with self._pool.connection() as connection:
            if owner is None:
                rows = connection.execute(
                    "UPDATE jobs SET state = 'failed', result = %s, finished_at = %s "
                    "WHERE state = ANY(%s) RETURNING id",
                    (json.dumps({"error": _INTERRUPTED}), at, list(_UNFINISHED)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "UPDATE jobs SET state = 'failed', result = %s, finished_at = %s "
                    "WHERE state = ANY(%s) AND owner = %s RETURNING id",
                    (json.dumps({"error": _INTERRUPTED}), at,
                     list(_UNFINISHED), owner),
                ).fetchall()
        return len(rows)


def build(path=None):
    """
    A store from configuration. In memory unless a path is set.

    Defaulting to disk would put a database file next to every CLI question,
    and the CLI has nothing to remember.
    """
    path = STATE_DB if path is None else path
    if not path:
        return MemoryStore()
    if is_shared_dsn(path):
        return PostgresStore(path)
    return SqliteStore(path)


# The schemes that mean "a database two replicas can share". Anything else in
# TRIAGE_STATE_DB is a filesystem path, which is the single-writer case.
_SHARED_SCHEMES = ("postgresql://", "postgres://")


def is_shared_dsn(value):
    """
    Whether this state location is safe for more than one replica.

    The chart asks the same question before it will accept `replicas > 1`, so
    the answer lives here rather than being spelled twice.
    """
    return str(value).startswith(_SHARED_SCHEMES)


def new_job_id():
    return uuid.uuid4().hex


def now():
    """Wall clock, and only here, so tests have one thing to freeze."""
    return time.time()
