"""
Tests for the watch-backed pod cache.

The cache adds a failure a live read does not have: being wrong because it is
old. In a triage tool that means describing a fault that is already fixed. So
what is tested here is mostly refusal -- every state where the cache cannot
prove it is current must come back as None so the caller does a live read.
"""

import time
from unittest.mock import MagicMock, patch

from kubernetes import client

import podcache
from conftest import make_pod


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
