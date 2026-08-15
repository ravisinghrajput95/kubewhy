"""
Tests for the watch controller.

The watching is the easy part. These concentrate on the logic that decides
what *not* to send, because a controller that posts thirty messages during an
incident gets muted and then uninstalled -- at which point nothing else about
it matters.
"""

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client

import controller as ctrl
from conftest import container_status, make_pod

# Fixed, and timezone-aware because the Kubernetes client returns aware
# datetimes -- subtracting a naive one raises rather than being wrong quietly.
NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)


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


class TestAStatusThatIsOnlyAFaultWhenItLasts:
    """
    ContainerCreating cannot go in WATCHED and cannot stay out of it.

    Every image pull passes through it, so watching it outright means
    diagnosing every ordinary start-up. But a volume naming a ConfigMap or
    Secret that does not exist parks a pod there permanently, and the
    controller was silent about that for as long as the pod existed. Elapsed
    time is the only thing that separates the two.
    """

    def stuck_pod(self, age_s, reason="ContainerCreating", started=True):
        pod = owned_pod(statuses=[container_status(ready=False, waiting_reason=reason)])
        when = NOW - dt.timedelta(seconds=age_s)
        if started:
            pod.status.start_time = when
        else:
            pod.metadata.creation_timestamp = when
        return pod

    def test_a_pod_stuck_past_the_threshold_is_reported(self, controller):
        pod = self.stuck_pod(ctrl.STUCK_AFTER + 60)
        assert controller.interesting(pod, now=NOW) == "ContainerCreating"

    def test_a_pod_that_is_merely_starting_is_left_alone(self, controller):
        """The false positive that matters. 30s is an ordinary image pull."""
        assert controller.interesting(self.stuck_pod(30), now=NOW) is None

    def test_the_boundary_is_inclusive(self, controller):
        assert controller.interesting(self.stuck_pod(ctrl.STUCK_AFTER - 1), now=NOW) is None
        assert controller.interesting(self.stuck_pod(ctrl.STUCK_AFTER), now=NOW) is not None

    def test_podinitializing_is_treated_the_same(self, controller):
        pod = self.stuck_pod(ctrl.STUCK_AFTER + 60, reason="PodInitializing")
        assert controller.interesting(pod, now=NOW) == "PodInitializing"

    def test_creation_timestamp_is_the_fallback(self, controller):
        """A pod the kubelet never accepted has no start_time."""
        pod = self.stuck_pod(ctrl.STUCK_AFTER + 60, started=False)
        assert pod.status.start_time is None
        assert controller.interesting(pod, now=NOW) == "ContainerCreating"

    def test_a_pod_with_no_timestamp_at_all_is_not_guessed_about(self, controller):
        pod = owned_pod(
            statuses=[container_status(ready=False, waiting_reason="ContainerCreating")]
        )
        assert pod.status.start_time is None
        assert controller.stuck_for(pod, now=NOW) is None
        assert controller.interesting(pod, now=NOW) is None

    def test_age_alone_does_not_make_a_status_interesting(self, controller):
        """
        A long-running healthy pod is old. Age is only ever a qualifier on a
        status already in STUCK_WHEN_SLOW, never a reason on its own.
        """
        pod = owned_pod(statuses=[container_status(ready=True)])
        pod.status.start_time = NOW - dt.timedelta(days=30)
        assert controller.interesting(pod, now=NOW) is None

    def test_a_watched_status_still_reports_immediately(self, controller):
        """The threshold must not delay the faults that were never transient."""
        pod = owned_pod(
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")]
        )
        pod.status.start_time = NOW - dt.timedelta(seconds=5)
        assert controller.interesting(pod, now=NOW) == "CrashLoopBackOff"

    def test_a_terminating_pod_is_still_ignored(self, controller):
        """Deletion beats every other rule, including this one."""
        pod = self.stuck_pod(ctrl.STUCK_AFTER + 600)
        pod.metadata.deletion_timestamp = NOW
        assert controller.interesting(pod, now=NOW) is None


class TestRefundingASlotNothingWasSaidWith:
    """
    A spend and a delivered message are not the same event.

    The hourly ceiling is global, so a slot spent on a finding that was never
    posted is taken from every other workload in the cluster.
    """

    def test_a_refunded_slot_returns_to_the_ceiling(self):
        budget = ctrl.Budget(cooldown=0, max_per_hour=2)
        first = budget.spend("a", now=0)
        budget.spend("b", now=1)
        assert budget.allow("c", now=2) is False

        budget.refund(first)
        assert budget.allow("c", now=3) is True

    def test_a_refunded_workload_can_report_again_immediately(self):
        budget = ctrl.Budget(cooldown=1800, max_per_hour=12)
        receipt = budget.spend("ns/nightly-sync/Error", now=0)
        assert budget.allow("ns/nightly-sync/Error", now=60) is False

        budget.refund(receipt)
        assert budget.allow("ns/nightly-sync/Error", now=60) is True

    def test_a_refund_does_not_clear_a_cooldown_that_predates_it(self):
        budget = ctrl.Budget(cooldown=1800, max_per_hour=12)
        assert budget.allow("ns/web/Error", now=0) is True
        receipt = budget.spend("ns/web/Error", now=1900)

        budget.refund(receipt)
        # Back to the 0 report, whose cooldown expired at 1800 -- not to no
        # report at all, which would have been reportable at t=1.
        assert budget.allow("ns/web/Error", now=1000) is False

    def test_refunding_nothing_is_harmless(self):
        """diagnose() refunds unconditionally; direct callers pass no receipt."""
        ctrl.Budget().refund(None)

    def test_spend_reports_the_same_decision_as_allow(self):
        budget = ctrl.Budget(cooldown=1800, max_per_hour=12)
        assert budget.spend("k", now=0) is not None
        assert budget.spend("k", now=60) is None

    def test_a_vanished_workload_gives_back_its_hourly_slot(self, controller):
        """
        The case this was built for: a CronJob collected before the model
        reads it. Twelve of these in an hour used to silence the cluster.
        """
        gone = owned_pod(statuses=[container_status(ready=False, terminated_reason="Error")])
        receipt = controller.budget.spend("demo/nightly-sync/Error", now=0)

        with patch.object(controller, "still_there", return_value=None), \
                patch.object(ctrl.agent, "ask") as ask:
            assert controller.diagnose(gone, "Error", receipt=receipt) is None

        assert not ask.called
        assert controller.budget.store.reports_since(0) == 0

    def test_a_full_queue_gives_back_its_slot(self, controller):
        """
        Nothing was queued, so nothing will ever be posted for that slot.

        Keeping it would let a failure storm that overflows the queue also eat
        the ceiling, silencing the workloads whose findings did get in.
        """
        controller.work = ctrl.queue.Queue(maxsize=1)
        web = owned_pod(
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")]
        )
        api = owned_pod(
            name="api-def456-xyz",
            owner="api-def456",
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")],
        )

        with patch.object(ctrl.agent, "capture_pod_logs", return_value=[]):
            assert controller.enqueue(web, "CrashLoopBackOff") is True
            # A different workload, so this is the queue refusing it rather
            # than the cooldown.
            assert controller.enqueue(api, "CrashLoopBackOff") is False

        assert controller.budget.store.reports_since(0) == 1

    def test_a_failed_diagnosis_keeps_its_slot(self, controller):
        """
        Deliberately not refunded, unlike a vanished pod.

        The fault is still real and still present. The spent slot is the only
        thing pacing retries while Ollama is down.
        """
        pod = owned_pod(statuses=[container_status(ready=False, terminated_reason="Error")])
        receipt = controller.budget.spend("demo/web/Error", now=0)

        with patch.object(controller, "still_there", return_value=pod), \
                patch.object(ctrl.agent, "ask", side_effect=RuntimeError("ollama down")):
            assert controller.diagnose(pod, "Error", receipt=receipt) is None

        assert controller.budget.store.reports_since(0) == 1


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


class TestStartupSuppression:
    """
    Pods already broken at boot are known problems; diagnosing the backlog on
    startup is how a bot gets muted in its first minute.
    """

    def test_records_uids_only_during_startup_window(self, controller):
        controller.start_time = ctrl.time.monotonic()  # window is open
        assert controller.in_startup_window() is True

        controller.start_time = ctrl.time.monotonic() - 999
        assert controller.in_startup_window() is False

    def test_uid_set_stops_growing_after_startup(self, controller):
        """
        The set used to be added to on every event forever, which on a cluster
        with Job churn leaks for no benefit -- after the window the
        membership test changes nothing.
        """
        controller.start_time = ctrl.time.monotonic() - 999  # window closed

        api = MagicMock()
        events = [
            {"object": owned_pod(name=f"pod-{i}")} for i in range(200)
        ]
        with patch.object(ctrl.watch, "Watch") as w:
            w.return_value.stream.return_value = events
            controller.watch_once(api)

        assert controller.preexisting_uids == set()

    def test_preexisting_pods_stay_suppressed_after_window(self, controller):
        controller.start_time = ctrl.time.monotonic()
        broken = owned_pod(
            name="already-broken",
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")],
        )

        api = MagicMock()
        with patch.object(ctrl.watch, "Watch") as w:
            w.return_value.stream.return_value = [{"object": broken}]
            controller.watch_once(api)

        assert "already-broken" in controller.preexisting_uids

        # The same pod reappearing later must still be ignored: it was broken
        # before we arrived, and nothing has changed.
        controller.start_time = ctrl.time.monotonic() - 999
        with patch.object(ctrl.watch, "Watch") as w, patch.object(
            controller, "enqueue"
        ) as enqueue:
            w.return_value.stream.return_value = [{"object": broken}]
            controller.watch_once(api)

        enqueue.assert_not_called()

    def test_new_pod_after_startup_is_diagnosed(self, controller):
        controller.start_time = ctrl.time.monotonic() - 999
        fresh = owned_pod(
            name="broke-just-now",
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")],
        )

        api = MagicMock()
        with patch.object(ctrl.watch, "Watch") as w, patch.object(
            controller, "enqueue", return_value=True
        ) as enqueue:
            w.return_value.stream.return_value = [{"object": fresh}]
            controller.watch_once(api)

        enqueue.assert_called_once()


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
        ), patch.object(controller, "still_there", return_value=pod):
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
        with patch.object(ctrl.agent, "ask", side_effect=RuntimeError("ollama down")), \
                patch.object(controller, "still_there", return_value=pod):
            assert controller.diagnose(pod, "OOMKilled") is None


class TestThePodMayBeGoneAlready:
    """
    The watch reports a pod; the diagnosis runs a minute or two later.

    For a CronJob failing every minute with failedJobsHistoryLimit: 2 that gap
    is longer than the pod's whole life. Measured on the demo cluster: every
    tool call about the handed-over pod returned
    {"error": "kubernetes API error 404: ... not found"}, three runs of three,
    and the model -- given nothing to reason from -- replied with a plan for
    investigating rather than a diagnosis. Read as a prompt defect for weeks;
    it is a race.
    """

    @staticmethod
    def _api(read=None, listed=()):
        api = MagicMock()
        if read is None:
            api.read_namespaced_pod.side_effect = client.ApiException(status=404)
        else:
            api.read_namespaced_pod.return_value = read
        api.list_namespaced_pod.return_value = MagicMock(items=list(listed))
        return api

    def test_the_original_pod_is_used_when_it_still_exists(self, controller):
        pod = owned_pod(statuses=[container_status(ready=False, terminated_reason="OOMKilled")])

        with patch.object(ctrl, "_api", return_value=self._api(read=pod)):
            assert controller.still_there(pod, "OOMKilled") is pod

    def test_a_collected_pod_is_replaced_by_a_live_one(self, controller):
        """The whole point: same workload, same fault, still there."""
        gone = owned_pod(
            name="nightly-sync-29772408-4bn2r",
            statuses=[container_status(ready=False, terminated_reason="Error")],
        )
        live = owned_pod(
            name="nightly-sync-29772414-xh4t9",
            statuses=[container_status(ready=False, terminated_reason="Error")],
        )

        with patch.object(ctrl, "_api", return_value=self._api(listed=[live])):
            chosen = controller.still_there(gone, "Error")

        assert chosen.metadata.name == "nightly-sync-29772414-xh4t9"

    def test_a_workload_that_has_gone_quiet_is_not_diagnosed(self, controller):
        """
        No live pod carries the fault, so there is nothing to look at.

        Posting here would mean an alert about a pod that no longer exists,
        built from 404s -- worse than silence.
        """
        gone = owned_pod(statuses=[container_status(ready=False, terminated_reason="Error")])

        with patch.object(ctrl, "_api", return_value=self._api(listed=[])):
            assert controller.still_there(gone, "Error") is None

    def test_diagnose_gives_up_rather_than_asking_about_a_dead_pod(self, controller):
        gone = owned_pod(statuses=[container_status(ready=False, terminated_reason="Error")])

        with patch.object(controller, "still_there", return_value=None), \
                patch.object(ctrl.agent, "ask") as ask:
            assert controller.diagnose(gone, "Error") is None

        assert not ask.called, "asked the model about a pod known to be gone"

    def test_an_api_failure_is_not_read_as_a_collected_pod(self, controller):
        """
        A 503 says nothing about whether the pod exists.

        Treating every error as "gone" would silently drop findings during
        exactly the API trouble most worth hearing about.
        """
        pod = owned_pod(statuses=[container_status(ready=False, terminated_reason="Error")])
        api = MagicMock()
        api.read_namespaced_pod.side_effect = client.ApiException(status=503)

        with patch.object(ctrl, "_api", return_value=api):
            assert controller.still_there(pod, "Error") is pod

        assert not api.list_namespaced_pod.called

    def test_the_newest_replacement_wins(self, controller):
        """An older replacement is the one about to be collected next."""
        import datetime as dt

        gone = owned_pod(name="nightly-sync-old-aaa",
                         statuses=[container_status(ready=False, terminated_reason="Error")])
        older = owned_pod(name="nightly-sync-1",
                          statuses=[container_status(ready=False, terminated_reason="Error")])
        newer = owned_pod(name="nightly-sync-2",
                          statuses=[container_status(ready=False, terminated_reason="Error")])
        base = dt.datetime(2026, 8, 10, 12, 0, tzinfo=dt.timezone.utc)
        older.metadata.creation_timestamp = base
        newer.metadata.creation_timestamp = base + dt.timedelta(minutes=1)

        with patch.object(ctrl, "_api", return_value=self._api(listed=[older, newer])):
            assert controller.still_there(gone, "Error").metadata.name == "nightly-sync-2"

    def test_a_different_fault_is_not_substituted(self, controller):
        """
        A replacement has to share the fault, or the alert describes one
        problem while naming another.
        """
        gone = owned_pod(statuses=[container_status(ready=False, terminated_reason="Error")])
        other = owned_pod(
            name="nightly-sync-other",
            statuses=[container_status(ready=False, waiting_reason="ImagePullBackOff")],
        )

        with patch.object(ctrl, "_api", return_value=self._api(listed=[other])):
            assert controller.still_there(gone, "Error") is None


class TestEvidenceIsCapturedWhileThePodIsAlive:
    """
    still_there() narrowed the race; it did not close it.

    A CronJob pod lives ~120s and a diagnosis takes 89-126s, so the live
    replacement it substitutes is collected mid-diagnosis too -- measured 0/3
    on nightly-sync both before and after that change. The only evidence that
    survives is evidence read before the queue.
    """

    def test_logs_are_captured_in_the_shape_ask_expects(self, controller):
        with patch.object(ctrl.agent, "get_pod_logs", return_value={"logs": "FATAL: 503"}):
            evidence = controller.capture_evidence(owned_pod())

        assert len(evidence) == 1
        assert evidence[0]["name"] == "get_pod_logs"
        assert "FATAL: 503" in evidence[0]["result"]
        assert evidence[0]["captured_at"]

    def test_an_error_result_is_not_passed_off_as_evidence(self, controller):
        """
        Tools return {"error": ...} as data. Forwarding one would hand the
        model an error message dressed up as a measurement.
        """
        with patch.object(ctrl.agent, "get_pod_logs", return_value={"error": "404 not found"}):
            assert controller.capture_evidence(owned_pod()) == []

    def test_a_raising_tool_is_not_fatal(self, controller):
        """Failing to capture puts us back where we were, which was survivable."""
        with patch.object(ctrl.agent, "get_pod_logs", side_effect=RuntimeError("boom")):
            assert controller.capture_evidence(owned_pod()) == []

    def test_capture_happens_only_after_the_budget_agrees(self, controller):
        """
        Otherwise every deduped event costs an extra log read on a cluster
        that is already having a bad day.
        """
        pod = owned_pod(
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")]
        )
        with patch.object(controller, "capture_evidence", return_value=[]) as capture:
            assert controller.enqueue(pod, "CrashLoopBackOff") is True
            assert controller.enqueue(pod, "CrashLoopBackOff") is False  # cooldown

        assert capture.call_count == 1

    def test_evidence_reaches_the_model(self, controller):
        pod = owned_pod(statuses=[container_status(ready=False, terminated_reason="Error")])
        evidence = [{"name": "get_pod_logs", "arguments": {}, "result": "{}"}]
        fake = {"answer": "a", "confidence": "grounded", "unverified": [],
                "tool_calls": []}

        with patch.object(ctrl.agent, "ask", return_value=fake) as ask, \
                patch.object(controller, "still_there", return_value=pod), \
                patch.object(controller, "count_affected", return_value=1):
            controller.diagnose(pod, "Error", evidence)

        assert ask.call_args.kwargs["prefetched"] is evidence
