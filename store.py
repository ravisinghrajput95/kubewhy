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

    def create_job(self, job_id, question, at):
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "state": "queued",
                "question": question,
                "created_at": at,
                "finished_at": None,
                "result": None,
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

    def purge_jobs(self, cutoff):
        with self._lock:
            for job_id in [
                k for k, v in self._jobs.items() if v["created_at"] < cutoff
            ]:
                del self._jobs[job_id]


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
                "created_at REAL NOT NULL, finished_at REAL, result TEXT)"
            )

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

    def create_job(self, job_id, question, at):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs (id, state, question, created_at) VALUES (?,?,?,?)",
                (job_id, "queued", question, at),
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

    def purge_jobs(self, cutoff):
        with self._connect() as connection:
            connection.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))


def build(path=None):
    """
    A store from configuration. In memory unless a path is set.

    Defaulting to disk would put a database file next to every CLI question,
    and the CLI has nothing to remember.
    """
    path = STATE_DB if path is None else path
    return SqliteStore(path) if path else MemoryStore()


def new_job_id():
    return uuid.uuid4().hex


def now():
    """Wall clock, and only here, so tests have one thing to freeze."""
    return time.time()
