"""
Tests for the watch controller.

The watching is the easy part. These concentrate on the logic that decides
what *not* to send, because a controller that posts thirty messages during an
incident gets muted and then uninstalled -- at which point nothing else about
it matters.
"""

from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client

import controller as ctrl
from conftest import container_status, make_pod


def owned_pod(name="web-abc123-xyz", owner="web-abc123", **kwargs):
    pod = make_pod(name=name, **kwargs)
    pod.metadata.owner_references = [
        client.V1OwnerReference(
            api_version="apps/v1", kind="ReplicaSet", name=owner, uid="u", controller=True
        )
    ]
    pod.metadata.uid = name
    return pod


@pytest.fixture
def controller():
    c = ctrl.Controller(sink=MagicMock(), budget=ctrl.Budget(cooldown=60, max_per_hour=5))
    c.start_time = -1000  # not "recently started", so startup skip is inactive
    return c


class TestWorkloadGrouping:
    def test_replicaset_hash_is_trimmed(self):
        """Ten crashing replicas must produce one finding, not ten."""
        assert ctrl.workload_of(owned_pod(owner="payments-api-6f79bc6fcb")) == "payments-api"

    def test_bare_pod_has_no_workload(self):
        pod = make_pod()
        pod.metadata.owner_references = None
        assert ctrl.workload_of(pod) is None

    def test_non_replicaset_owner_used_directly(self):
        pod = make_pod()
        pod.metadata.owner_references = [
            client.V1OwnerReference(
                api_version="batch/v1", kind="Job", name="nightly-import", uid="u"
            )
        ]
        assert ctrl.workload_of(pod) == "nightly-import"


class TestDetection:
    def test_flags_watched_status(self, controller):
        pod = owned_pod(
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")]
        )
        assert controller.interesting(pod) == "CrashLoopBackOff"

    def test_ignores_healthy_pod(self, controller):
        assert controller.interesting(owned_pod()) is None

    def test_ignores_transient_states(self, controller):
        # ContainerCreating resolves on its own; waking someone for it is noise.
        pod = owned_pod(
            statuses=[container_status(ready=False, waiting_reason="ContainerCreating")]
        )
        assert controller.interesting(pod) is None

    def test_ignores_terminating_pod(self, controller):
        pod = owned_pod(
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")]
        )
        pod.metadata.deletion_timestamp = "2026-08-04T00:00:00Z"
        assert controller.interesting(pod) is None

    def test_ignores_recovered_pod_with_old_restarts(self, controller):
        """A pod that crashed an hour ago but is ready now is not a problem."""
        pod = owned_pod(
            statuses=[container_status(ready=True, restart_count=9, terminated_reason=None)]
        )
        assert controller.interesting(pod) is None


class TestBudget:
    def test_first_finding_allowed(self):
        assert ctrl.Budget().allow("ns/web/OOMKilled") is True

    def test_same_workload_suppressed_during_cooldown(self):
        budget = ctrl.Budget(cooldown=1800)
        key = "ns/web/CrashLoopBackOff"

        assert budget.allow(key, now=0) is True
        assert budget.allow(key, now=60) is False
        assert budget.allow(key, now=1900) is True

    def test_different_workloads_independent(self):
        budget = ctrl.Budget(cooldown=1800)
        assert budget.allow("ns/web/OOMKilled", now=0) is True
        assert budget.allow("ns/api/OOMKilled", now=0) is True

    def test_same_workload_different_fault_is_separate(self):
        budget = ctrl.Budget(cooldown=1800)
        assert budget.allow("ns/web/OOMKilled", now=0) is True
        assert budget.allow("ns/web/ImagePullBackOff", now=0) is True

    def test_global_ceiling_caps_a_failure_storm(self):
        """Fifty workloads failing at once must not produce fifty messages."""
        budget = ctrl.Budget(cooldown=1800, max_per_hour=5)
        allowed = sum(budget.allow(f"ns/workload-{i}/OOMKilled", now=0) for i in range(50))
        assert allowed == 5

    def test_ceiling_window_rolls_off(self):
        budget = ctrl.Budget(cooldown=0, max_per_hour=2)
        assert budget.allow("a", now=0) is True
        assert budget.allow("b", now=1) is True
        assert budget.allow("c", now=2) is False
        assert budget.allow("c", now=3700) is True


class TestFaultClasses:
    """
    One fault, one message.

    Found by running the controller against a real cluster: a bad image
    reported ErrImagePull and then ImagePullBackOff, and deduping on the raw
    status posted a separate diagnosis for each.
    """

    def test_image_pull_statuses_collapse(self, controller):
        first = owned_pod(
            name="p1", statuses=[container_status(ready=False, waiting_reason="ErrImagePull")]
        )
        second = owned_pod(
            name="p2",
            statuses=[container_status(ready=False, waiting_reason="ImagePullBackOff")],
        )

        assert controller.enqueue(first, "ErrImagePull") is True
        assert controller.enqueue(second, "ImagePullBackOff") is False

    def test_crash_statuses_collapse(self, controller):
        # A crashing container alternates between these two indefinitely.
        pod = owned_pod(
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")]
        )
        assert controller.enqueue(pod, "CrashLoopBackOff") is True
        assert controller.enqueue(pod, "Error") is False

    def test_oomkill_and_crashloop_collapse(self, controller):
        """
        Also found on a live cluster: an OOM-killed pod restarts and enters
        CrashLoopBackOff, so treating them as separate faults posted the OOM
        diagnosis and then a near-identical crash diagnosis a minute later.
        """
        pod = owned_pod(
            statuses=[container_status(ready=False, terminated_reason="OOMKilled")]
        )
        assert controller.enqueue(pod, "OOMKilled") is True
        assert controller.enqueue(pod, "CrashLoopBackOff") is False

    def test_genuinely_different_faults_still_separate(self, controller):
        # A bad image and a crashing container are different problems and
        # deserve different messages.
        pod = owned_pod(
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")]
        )
        assert controller.enqueue(pod, "CrashLoopBackOff") is True
        assert controller.enqueue(pod, "ImagePullBackOff") is True


class TestQueue:
    def test_enqueue_respects_budget(self, controller):
        pod = owned_pod(
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")]
        )
        assert controller.enqueue(pod, "CrashLoopBackOff") is True
        assert controller.enqueue(pod, "CrashLoopBackOff") is False

    def test_full_queue_drops_rather_than_growing(self, controller):
        """Stale findings delivered late are worse than dropped ones."""
        controller.work = ctrl.queue.Queue(maxsize=1)
        controller.budget = ctrl.Budget(cooldown=0, max_per_hour=100)
        pod = owned_pod(
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")]
        )

        assert controller.enqueue(pod, "CrashLoopBackOff") is True
        assert controller.enqueue(pod, "CrashLoopBackOff") is False


class TestDiagnosis:
    def test_builds_finding_from_agent_result(self, controller):
        pod = owned_pod(
            statuses=[container_status(ready=False, terminated_reason="OOMKilled")]
        )
        fake = {
            "answer": "memory limit too low",
            "confidence": "grounded",
            "unverified": [],
            "tool_calls": [{"name": "describe_pod", "arguments": {}}],
        }

        with patch.object(ctrl.agent, "ask", return_value=fake), patch.object(
            controller, "count_affected", return_value=3
        ):
            finding = controller.diagnose(pod, "OOMKilled")

        assert finding["workload"] == "web"
        assert finding["replicas"] == 3
        assert finding["diagnosis"] == "memory limit too low"
        assert finding["tool_calls"] == ["describe_pod"]

    def test_agent_failure_does_not_propagate(self, controller):
        """One bad diagnosis must not kill the controller."""
        pod = owned_pod(
            statuses=[container_status(ready=False, terminated_reason="OOMKilled")]
        )
        with patch.object(ctrl.agent, "ask", side_effect=RuntimeError("ollama down")):
            assert controller.diagnose(pod, "OOMKilled") is None
