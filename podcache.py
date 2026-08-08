"""
A watch-backed pod cache, so a cluster-wide scan stops re-reading the cluster.

`scan_cluster` returns ~146 tokens and *transfers* every pod object to do it --
roughly 7KB each, so ~7MB on a thousand-pod cluster, every scan. No server-side
filter helps: a CrashLoopBackOff pod has `phase: Running`, so the interesting
pods are indistinguishable to the API server. The only real fix is to stop
asking repeatedly and keep a local copy the API server updates.

**Off unless asked for.** `TRIAGE_POD_CACHE=1`. A watch is a long-lived
connection and a background thread, which is right for the controller and the
UI and wrong for a CLI process answering one question and exiting.

**The failure mode this adds, and how it is contained.** A cache can be wrong
in a way a live read cannot: it can be *stale*, and stale pod state in a
triage tool means confidently describing a fault that is already fixed, or
missing one that just started. So freshness is not assumed. Every read checks
when the cache last heard from the API server, and anything older than
`max_age` is refused -- the caller falls back to a live read rather than being
served something old. A cache that cannot prove it is current is treated as no
cache at all.
"""

import logging
import os
import threading
import time

from kubernetes import watch

log = logging.getLogger(__name__)

ENABLED = os.getenv("TRIAGE_POD_CACHE", "").lower() in ("1", "true", "yes")

# Older than this and a read is refused. Deliberately short: the cost of being
# wrong here is a wrong diagnosis, and the cost of falling back is one list
# call -- exactly what happened before this existed.
MAX_AGE = int(os.getenv("TRIAGE_POD_CACHE_MAX_AGE", "60"))

# The watch is re-established on this cycle regardless. Long-lived watches are
# dropped by API servers and load balancers, and an expired exec credential is
# only refreshed when the next request is built.
RECONNECT_SECONDS = 300


class PodCache:
    """
    Every pod in the cluster, kept current by a watch rather than re-listed.

    The contract is deliberately narrow: pods() returns a list or None, and
    None means "ask the API server yourself". Every not-currently-trustworthy
    state -- not started, still filling, watch broken, gone stale -- collapses
    into that one answer, because a caller that has to distinguish them will
    eventually get one wrong.
    """

    def __init__(self, api=None, max_age=MAX_AGE):
        self.max_age = max_age
        self._api = api
        self._pods = {}
        self._lock = threading.Lock()
        self._synced_at = None
        self._thread = None
        self._stop = threading.Event()

    # -- reading ------------------------------------------------------------

    @property
    def fresh(self):
        with self._lock:
            if self._synced_at is None:
                return False
            return (time.monotonic() - self._synced_at) <= self.max_age

    def pods(self):
        """Every cached pod, or None when the cache cannot vouch for itself."""
        with self._lock:
            if self._synced_at is None:
                return None
            if (time.monotonic() - self._synced_at) > self.max_age:
                log.warning("pod_cache_stale falling back to a live read")
                return None
            return list(self._pods.values())

    # -- filling ------------------------------------------------------------

    def _client(self):
        """
        The configured client, not a fresh one.

        Building client.CoreV1Api() here bypassed kubeconfig loading entirely,
        so the cache watched a client with no host and every connection failed
        instantly -- invisibly, because the watch loop is designed to survive
        failure. Imported inside the function because k8s_pods_info imports
        this module.
        """
        if self._api is not None:
            return self._api
        from routers.k8s_pods_info import _api

        return _api()

    def _list(self):
        """
        Seed from a full list, and keep the resourceVersion to watch from.

        The initial list is the one unavoidable full transfer. Everything after
        it is deltas, which is the entire point.
        """
        api = self._client()
        listing = api.list_pod_for_all_namespaces(_request_timeout=30)
        with self._lock:
            self._pods = {
                (p.metadata.namespace, p.metadata.name): p for p in listing.items
            }
            self._synced_at = time.monotonic()
        return listing.metadata.resource_version

    def _apply(self, kind, pod):
        key = (pod.metadata.namespace, pod.metadata.name)
        with self._lock:
            if kind == "DELETED":
                self._pods.pop(key, None)
            else:
                self._pods[key] = pod
            # Any event proves the connection is alive, which is what freshness
            # is really asserting -- a quiet cluster is not a stale cache.
            self._synced_at = time.monotonic()

    def _run(self):
        while not self._stop.is_set():
            try:
                version = self._list()
                api = self._client()
                stream = watch.Watch().stream(
                    api.list_pod_for_all_namespaces,
                    resource_version=version,
                    timeout_seconds=RECONNECT_SECONDS,
                )
                for event in stream:
                    if self._stop.is_set():
                        return
                    self._apply(event["type"], event["object"])
                # The stream ended on its timeout. Re-list rather than resume:
                # the resourceVersion may have aged out, and a 410 mid-stream
                # is how a cache silently starts missing pods.
            except Exception as exc:  # noqa: BLE001 - the watch must not die
                log.warning("pod_cache_watch_failed", extra={"error": str(exc)})
                # Mark unusable immediately. Serving the last known state while
                # disconnected is the exact failure this class is designed to
                # refuse.
                with self._lock:
                    self._synced_at = None
                self._stop.wait(5)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()


_cache = None
_cache_lock = threading.Lock()


def pods_or_none():
    """
    The cached pods when there is a trustworthy cache, otherwise None.

    Starts the cache on first use so no surface has to know about lifecycle,
    and returns None on that first call -- the watch has not synced yet, and
    waiting for it would make the first scan slower than the read it replaces.
    """
    if not ENABLED:
        return None

    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = PodCache().start()
    return _cache.pods()
