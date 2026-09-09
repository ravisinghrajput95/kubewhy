"""
Tests for the watch-backed pod cache.

The cache adds a failure a live read does not have: being wrong because it is
old. In a triage tool that means describing a fault that is already fixed. So
what is tested here is mostly refusal -- every state where the cache cannot
prove it is current must come back as None so the caller does a live read.
"""

import importlib
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client

import podcache
from conftest import make_pod


@pytest.fixture
def reload_podcache():
    """
    Re-execute the module with TRIAGE_POD_CACHE set, and put it back after.

    ENABLED is read at import time, so nothing short of a reload can reach the
    expression that parses it -- which is why every test patched the flag
    instead and the parse itself went unexercised.
    """
    original = os.environ.get("TRIAGE_POD_CACHE")

    def load(value):
        if value is None:
            os.environ.pop("TRIAGE_POD_CACHE", None)
        else:
            os.environ["TRIAGE_POD_CACHE"] = value
        return importlib.reload(podcache)

    yield load

    if original is None:
        os.environ.pop("TRIAGE_POD_CACHE", None)
    else:
        os.environ["TRIAGE_POD_CACHE"] = original
    importlib.reload(podcache)


def cache(max_age=60):
    return podcache.PodCache(api=MagicMock(), max_age=max_age)


class TestItRefusesWhenItCannotVouchForItself:
    def test_before_the_first_sync_there_is_no_cache(self):
        assert cache().pods() is None

    def test_stale_beyond_max_age_is_refused(self):
        c = cache(max_age=30)
        c._pods = {("demo", "web"): make_pod(name="web")}
        c._synced_at = time.monotonic() - 31

        assert c.pods() is None

    def test_fresh_within_max_age_is_served(self):
        c = cache(max_age=30)
        c._pods = {("demo", "web"): make_pod(name="web")}
        c._synced_at = time.monotonic()

        assert [p.metadata.name for p in c.pods()] == ["web"]

    def test_a_broken_watch_invalidates_immediately(self):
        """
        Serving the last known state while disconnected from the API server is
        the exact failure this class exists to refuse.
        """
        c = cache()
        c._pods = {("demo", "web"): make_pod(name="web")}
        c._synced_at = time.monotonic()
        c._api.list_pod_for_all_namespaces.side_effect = Exception("connection reset")
        # Let exactly one iteration run: the backoff wait ends the loop, so
        # this exercises the failure path rather than skipping the body.
        with patch.object(c._stop, "wait", side_effect=lambda _: c._stop.set()):
            c._run()

        assert c.pods() is None


class TestItTracksTheCluster:
    def test_a_listing_seeds_it(self):
        c = cache()
        c._api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[make_pod(name="web")],
            metadata=client.V1ListMeta(resource_version="42"),
        )

        assert c._list() == "42"
        assert len(c.pods()) == 1

    def test_an_added_pod_appears(self):
        c = cache()
        c._synced_at = time.monotonic()
        c._apply("ADDED", make_pod(name="new"))

        assert [p.metadata.name for p in c.pods()] == ["new"]

    def test_a_deleted_pod_disappears(self):
        c = cache()
        c._apply("ADDED", make_pod(name="going"))
        c._apply("DELETED", make_pod(name="going"))

        assert c.pods() == []

    def test_an_event_proves_the_connection_is_alive(self):
        """A quiet cluster is not a stale cache; a silent one is."""
        c = cache(max_age=30)
        c._synced_at = time.monotonic() - 29
        c._apply("MODIFIED", make_pod(name="web"))

        assert c.fresh is True


class TestDisabledByDefault:
    def test_nothing_starts_unless_asked(self):
        with patch.object(podcache, "ENABLED", False):
            assert podcache.pods_or_none() is None


class TestTheFlagThatTurnsItOn:
    """
    "Off unless asked for" is the property that keeps a CLI process from
    opening a watch, and it was asserted only by patching `ENABLED` directly
    -- so the expression that computes it was never run in a test at all.
    Inverting `in` to `not in` survived: `TRIAGE_POD_CACHE=1` would have
    disabled the cache and leaving it unset would have opened a watch in every
    one-shot command.
    """

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes"])
    def test_the_documented_values_enable_it(self, reload_podcache, value):
        assert reload_podcache(value).ENABLED is True

    @pytest.mark.parametrize("value", [None, "", "0", "no", "off"])
    def test_anything_else_leaves_it_off(self, reload_podcache, value):
        assert reload_podcache(value).ENABLED is False


class TestTheFreshnessBoundary:
    """
    `fresh` and `pods()` each carry their own copy of the max_age rule, and
    both boundaries were open: `<=` tightened to `<` and `>` relaxed to `>=`
    both survived, because every case sat well inside or well outside the
    window. At exactly max_age the cache is still current, and refusing there
    costs a live read on every scan of a cluster whose events arrive on a
    round cadence.

    `fresh` also had no case for the un-synced cache, so `return False`
    flipping to `True` survived -- a cache that had never heard from the API
    server at all would have described itself as current.
    """

    def test_an_unsynced_cache_is_not_fresh(self):
        assert cache().fresh is False

    def test_exactly_max_age_is_still_fresh(self):
        c = cache(max_age=60)
        c._synced_at = 1000.0

        with patch.object(podcache.time, "monotonic", return_value=1060.0):
            assert c.fresh is True

    def test_a_moment_past_max_age_is_not(self):
        """The counter: the property has to be able to go False."""
        c = cache(max_age=60)
        c._synced_at = 1000.0

        with patch.object(podcache.time, "monotonic", return_value=1060.5):
            assert c.fresh is False

    def test_pods_are_still_served_at_exactly_max_age(self):
        c = cache(max_age=60)
        c._pods = {("demo", "web"): "the-pod"}
        c._synced_at = 1000.0

        with patch.object(podcache.time, "monotonic", return_value=1060.0):
            assert c.pods() == ["the-pod"]

    def test_pods_are_refused_a_moment_later(self):
        c = cache(max_age=60)
        c._pods = {("demo", "web"): "the-pod"}
        c._synced_at = 1000.0

        with patch.object(podcache.time, "monotonic", return_value=1060.5):
            assert c.pods() is None


class TestStartingTheWatch:
    """
    `start()` is guarded by `if self._thread is None`, and nothing asserted
    what it built. Inverting the guard survived -- `start()` would have
    returned without ever launching the watch, and the cache would then have
    sat empty and refused every read, which looks exactly like a cache that
    is merely cold.

    The thread must also be a daemon: a non-daemon watch keeps the
    interpreter alive after the CLI has printed its answer.
    """

    def test_it_launches_one_daemon_thread_and_only_one(self):
        c = cache()

        with patch.object(podcache.PodCache, "_run", lambda self: None):
            c.start()
            first = c._thread
            c.start()

        assert first is not None
        assert first.daemon is True
        assert c._thread is first
        first.join(timeout=1)


class TestTheModuleLevelCache:
    """
    `pods_or_none` is the only entry point anything outside this module uses.
    Both of its guards survived mutation: dropping the `not` from
    `if not ENABLED` would open a watch in a process that had opted out, and
    inverting `if _cache is None` would call `.pods()` on None the first time
    anyone asked.
    """

    def test_a_disabled_cache_is_never_constructed(self):
        with patch.object(podcache, "ENABLED", False), \
             patch.object(podcache, "_cache", None), \
             patch.object(podcache, "PodCache") as built:
            assert podcache.pods_or_none() is None

        assert built.call_count == 0

    def test_an_enabled_cache_is_built_once_and_consulted(self):
        made = MagicMock()
        made.start.return_value = made
        made.pods.return_value = ["a-pod"]

        with patch.object(podcache, "ENABLED", True), \
             patch.object(podcache, "_cache", None), \
             patch.object(podcache, "PodCache", return_value=made) as built:
            assert podcache.pods_or_none() == ["a-pod"]
            assert podcache.pods_or_none() == ["a-pod"]

        assert built.call_count == 1


class TestTheWatchCadencesAreDeliberate:
    """
    Three numbers, each with a stated reason above it and none of them read by
    a test. They are the cache's whole operational behaviour: how often it
    rebuilds, how long it waits for the one unavoidable full transfer, and how
    fast it retries after the watch dies.
    """

    def test_the_watch_is_rebuilt_every_five_minutes(self):
        """
        Long-lived watches are dropped by API servers and load balancers, and
        an expired exec credential is only refreshed when the next request is
        built -- so the cycle is not an optimisation, it is what stops a
        long-running controller quietly holding a dead connection and a stale
        token.
        """
        assert podcache.RECONNECT_SECONDS == 300

    def test_the_initial_list_is_bounded(self):
        """
        The full transfer is the one request that can be large, and it is made
        while the cache reports itself unsynced. Unbounded, a hung API server
        leaves it unsynced forever instead of failing and retrying.
        """
        seen = {}

        class Api:
            def list_pod_for_all_namespaces(self, **kwargs):
                seen.update(kwargs)
                return SimpleNamespace(
                    items=[], metadata=SimpleNamespace(resource_version="1"))

        cache = podcache.PodCache()
        with patch.object(cache, "_client", lambda: Api()):
            cache._list()

        assert seen["_request_timeout"] == 30

    def test_a_failed_watch_backs_off_before_retrying(self):
        """
        Five seconds, and the wait is on the stop event rather than `sleep` so
        a shutdown is not held up by it. Zero would spin against a dead API
        server; a long wait leaves the cache refusing reads for no reason.
        """
        waits = []
        cache = podcache.PodCache()

        def blow_up(*args, **kwargs):
            raise RuntimeError("watch died")

        with patch.object(cache, "_list", blow_up), \
             patch.object(cache._stop, "wait", lambda s: (waits.append(s),
                                                          cache._stop.set())[0]):
            cache._run()

        assert waits == [5]
