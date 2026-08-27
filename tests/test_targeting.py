"""
Tests for the investigation-target invariant.

The agent may choose HOW to investigate. It may not change WHAT it is
investigating. Measured 2026-08-19 over three runs of "Is the
correctly-configured pod in config-faults unhealthy?": two answers described
`missing-configmap-key` instead, both having called
list_pods(only_unhealthy=True) with no workload -- which excludes a healthy pod
by construction. The targeted tools existed; the model did not reach for them.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import agent
import targeting
from test_agent_loop import mock_chat, reply, tool_call


class TestTargetExtraction:
    @pytest.mark.parametrize("question,name,namespace", [
        ("Is the correctly-configured pod in the config-faults namespace unhealthy?",
         "correctly-configured", "config-faults"),
        ("Why is the crasher deployment in demo crashing?", "crasher", None),
        ("What is the status of the crasher pod in the demo namespace?",
         "crasher", "demo"),
        ("describe pod crasher-5964d99948-9g8vg in demo",
         "crasher-5964d99948-9g8vg", None),
        ("check the payments deployment", "payments", None),
    ])
    def test_a_named_entity_is_extracted(self, question, name, namespace):
        target = targeting.target_of(question)

        assert target["name"] == name
        assert target["namespace"] == namespace

    def test_a_service_keeps_its_kind(self):
        assert targeting.target_of("Why is the frontend service unreachable?") == {
            "kind": "service", "name": "frontend", "namespace": None,
        }

    def test_a_namespace_alone_is_still_a_target(self):
        """"What is broken in shop?" must not wander into default."""
        assert targeting.target_of("What is broken in the shop namespace?") == {
            "kind": "namespace", "name": None, "namespace": "shop",
        }

    @pytest.mark.parametrize("question", [
        "Is anything broken anywhere in the cluster?",
        "How much memory is this host using?",
        "What is failing right now?",
    ])
    def test_a_question_naming_nothing_has_no_target(self, question):
        """
        Precision over recall: a wrongly extracted target would rewrite every
        call to a workload that does not exist and break the run, while a
        missed one just leaves the old behaviour in place.
        """
        assert targeting.target_of(question) is None

    @pytest.mark.parametrize("question", [
        "The pod restarted 9 times.",
        "Why is the service unreachable?",
        "describe pod",
    ])
    def test_english_after_a_kind_is_not_a_name(self, question):
        target = targeting.target_of(question)

        assert target is None or target["name"] not in {
            "restarted", "unreachable", "describe",
        }


class TestEnforcement:
    TARGET = {"kind": "workload", "name": "correctly-configured",
              "namespace": "config-faults"}

    # Test 1 and 6: the omitted target argument
    def test_an_unscoped_list_is_retargeted_not_executed_as_is(self):
        arguments, violation = targeting.enforce(
            self.TARGET, "list_pods",
            {"namespace": "config-faults", "only_unhealthy": True},
        )

        assert arguments["workload"] == "correctly-configured"
        assert violation["action"] == "retargeted"

    def test_only_unhealthy_survives_the_rewrite(self):
        """
        The rewrite scopes the call; it does not second-guess the rest of it.
        list_pods lets a named workload override only_unhealthy, which is what
        makes "it is fine" an available answer.
        """
        arguments, _ = targeting.enforce(
            self.TARGET, "list_pods", {"only_unhealthy": True}
        )

        assert arguments["only_unhealthy"] is True

    # Test 2: workload A asked, workload B attempted
    def test_a_different_workload_is_retargeted(self):
        arguments, violation = targeting.enforce(
            self.TARGET, "scan_cluster", {"workload": "missing-configmap-key"}
        )

        assert arguments["workload"] == "correctly-configured"
        assert violation["action"] == "retargeted"

    # Test 3: namespace A asked, namespace B attempted
    def test_a_different_namespace_is_moved_back(self):
        arguments, violation = targeting.enforce(
            self.TARGET, "list_pods", {"namespace": "default"}
        )

        assert arguments["namespace"] == "config-faults"
        assert "not the 'config-faults'" in violation["reason"]

    # Test 4: service A asked, service B attempted
    def test_a_different_service_is_refused(self):
        target = {"kind": "service", "name": "frontend", "namespace": None}
        _, violation = targeting.enforce(
            target, "get_service_endpoints", {"name": "backend"}
        )

        assert violation["action"] == "refused"

    # Test 5: a pod name lifted from an earlier result
    def test_a_pod_of_another_workload_is_refused(self):
        _, violation = targeting.enforce(
            self.TARGET, "describe_pod", {"name": "missing-configmap-key"}
        )

        assert violation["action"] == "refused"

    def test_a_pod_of_the_target_workload_is_allowed(self):
        """The model must still be free to drill into its own target."""
        target = {"kind": "workload", "name": "crasher", "namespace": "demo"}
        _, violation = targeting.enforce(
            target, "describe_pod", {"name": "crasher-5964d99948-9g8vg"}
        )

        assert violation is None

    def test_a_similarly_named_workload_does_not_match(self):
        target = {"kind": "workload", "name": "crasher", "namespace": None}
        _, violation = targeting.enforce(
            target, "describe_pod", {"name": "crasher-two-abc-xyz"}
        )

        assert violation is None or violation["action"] == "refused"

    def test_no_target_means_no_interference(self):
        arguments, violation = targeting.enforce(
            None, "list_pods", {"only_unhealthy": True}
        )

        assert arguments == {"only_unhealthy": True}
        assert violation is None

    def test_host_tools_are_untouched(self):
        """The host collectors have no entity to scope."""
        arguments, violation = targeting.enforce(
            self.TARGET, "get_system_info", {}
        )

        assert violation is None


class TestTheLoopHoldsTheTarget:
    def test_the_call_that_reaches_kubernetes_is_the_retargeted_one(self):
        """End to end: the model omits the workload and the tool still gets it."""
        seen = {}

        def spy(**kwargs):
            seen.update(kwargs)
            return {"correctly-configured": {"status": "Running", "ready": "1/1"}}

        responses = [
            reply(calls=[tool_call("list_pods", {"namespace": "config-faults",
                                                 "only_unhealthy": True})]),
            reply(content="correctly-configured is running normally."),
        ]
        with patch.dict(agent.TOOLS, {"list_pods": spy}), \
             mock_chat(side_effect=responses):
            result = agent.ask(
                "Is the correctly-configured pod in the config-faults namespace unhealthy?"
            )

        assert seen["workload"] == "correctly-configured"
        assert "correctly-configured" in result["answer"]

    def test_the_violation_is_visible_in_the_trace(self):
        """Model mistakes are recorded, not hidden."""
        responses = [
            reply(calls=[tool_call("list_pods", {"only_unhealthy": True})]),
            reply(content="ok"),
        ]
        stub = {"list_pods": lambda **k: {"correctly-configured": {"status": "Running"}}}
        with patch.dict(agent.TOOLS, stub), mock_chat(side_effect=responses):
            result = agent.ask("Is the correctly-configured pod unhealthy?")

        scoped = [c for c in result["tool_calls"] if c.get("scope")]
        assert scoped and scoped[0]["scope"]["action"] == "retargeted"

    def test_a_refused_call_comes_back_as_data_not_an_exception(self):
        """Rule 3: the loop survives, and the model is told what it may see."""
        responses = [
            reply(calls=[tool_call("describe_pod", {"name": "missing-configmap-key",
                                                    "namespace": "config-faults"})]),
            reply(content="ok"),
        ]
        called = MagicMock()
        with patch.dict(agent.TOOLS, {"describe_pod": called}), \
             mock_chat(side_effect=responses):
            events = list(agent.stream(
                "Is the correctly-configured pod in config-faults unhealthy?"
            ))

        called.assert_not_called()
        results = [e for e in events if e["type"] == "tool_result"]
        assert "does not belong to" in json.loads(results[0]["result"])["error"]

    def test_an_untargeted_question_is_left_alone(self):
        """cluster_wide_scan must keep working exactly as it did."""
        seen = {}

        def spy(**kwargs):
            seen.update(kwargs)
            # ImagePullBackOff on purpose: it demands neither logs nor
            # events, so the evidence policy stays out of a test that is
            # about the scan's arguments.
            return {"demo/bad-image": {"status": "ImagePullBackOff", "pods": 1,
                                       "example": "bad-image-1"}}

        responses = [
            reply(calls=[tool_call("scan_cluster", {"only_unhealthy": True})]),
            reply(content="bad-image is failing."),
        ]
        with patch.dict(agent.TOOLS, {"scan_cluster": spy}), \
             mock_chat(side_effect=responses):
            agent.ask("Is anything broken anywhere in the cluster?")

        assert "workload" not in seen


class TestUnlabelledTargets:
    """
    "Why is crasher-svc unreachable?" names its target and never says what kind
    of thing it is, so target_of finds nothing and the invariant did not bind.
    Guessing from the text alone would be worse than not guessing -- a target
    the cluster has never heard of rewrites every call and breaks the run --
    so the guess is checked against the cluster first.
    """

    @pytest.mark.parametrize("question,expected", [
        ("Why is crasher-svc unreachable?", ["crasher-svc"]),
        ("Why is memory-hog failing?", ["memory-hog"]),
        ("Is crasher-svc affecting log-shipper?", ["crasher-svc", "log-shipper"]),
    ])
    def test_object_shaped_tokens_are_candidates(self, question, expected):
        assert targeting.candidate_names(question) == expected

    @pytest.mark.parametrize("question", [
        "Is anything broken anywhere in the cluster?",
        "What is failing right now?",
        "How much memory is this host using?",
    ])
    def test_ordinary_english_yields_no_candidate(self, question):
        assert targeting.candidate_names(question) == []

    def test_a_version_is_not_a_workload(self):
        assert "1.21" not in targeting.candidate_names("Why is nginx:1.21 failing?")

    def test_one_resolving_candidate_becomes_the_target(self):
        target = targeting.confirm(
            ["crasher-svc"],
            lambda n: {"kind": "service", "namespace": "demo"} if n == "crasher-svc" else None,
        )

        assert target == {"kind": "service", "name": "crasher-svc", "namespace": "demo"}

    def test_the_namespace_the_lookup_found_is_carried(self):
        """
        The resolver had to find the object to confirm it, so it already knows
        where it lives. Throwing that away left the model guessing `default`:
        measured live 2026-08-21, asked why crasher-svc was unreachable, the
        run looked in `default`, found nothing and reported that the service
        does not exist. It exists, in `demo`.
        """
        target = targeting.confirm(
            ["crasher-svc"], lambda n: {"kind": "service", "namespace": "demo"}
        )

        assert target["namespace"] == "demo"

    def test_two_resolving_candidates_are_refused(self):
        """
        The question mentions two real things. Picking the first is the
        entity-scoping mistake this module exists to prevent, so it declines.
        """
        assert targeting.confirm(
            ["a-one", "b-two"], lambda n: {"kind": "workload", "namespace": "demo"}
        ) is None

    def test_a_candidate_the_cluster_never_heard_of_is_not_a_target(self):
        assert targeting.confirm(["ghost-xyz"], lambda n: None) is None

    def test_an_unreachable_cluster_leaves_the_target_unset(self):
        """A wrong target is worse than none, so a failed lookup guesses nothing."""
        def broken(name):
            raise ConnectionError("no cluster")

        with patch.object(agent, "scan_cluster", broken):
            assert agent._resolve_entity("anything") is None

    def test_a_workload_resolves_before_a_service_is_consulted(self):
        looked_up = MagicMock(return_value=None)
        with patch.object(agent, "scan_cluster",
                          lambda **k: {"demo/memory-hog": {"status": "OOMKilled"}}), \
             patch.object(agent, "service_namespace", looked_up):
            assert agent._resolve_entity("memory-hog")["kind"] == "workload"

        looked_up.assert_not_called()

    def test_a_service_resolves_when_no_workload_does(self):
        with patch.object(agent, "scan_cluster",
                          lambda **k: {"result": "no workload named crasher-svc"}), \
             patch.object(agent, "service_namespace", lambda n: "demo"):
            resolved = agent._resolve_entity("crasher-svc")

        assert resolved == {"kind": "service", "namespace": "demo"}

    def test_the_loop_binds_an_unlabelled_target_it_could_confirm(self):
        seen = {}

        def spy(**kwargs):
            seen.update(kwargs)
            return {"memory-hog-abc": {"status": "OOMKilled", "ready": "0/1"}}

        responses = [
            reply(calls=[tool_call("list_pods", {"namespace": "demo"})]),
            reply(content="memory-hog was OOMKilled."),
        ]
        with patch.object(agent, "_resolve_entity",
                          lambda n: {"kind": "workload", "namespace": "demo"}), \
             patch.dict(agent.TOOLS, {"list_pods": spy}), \
             mock_chat(side_effect=responses):
            agent.ask("Why is memory-hog failing?")

        assert seen["workload"] == "memory-hog"

    def test_the_loop_stays_out_when_nothing_confirms(self):
        seen = {}

        def spy(**kwargs):
            seen.update(kwargs)
            return {"demo/bad-image": {"status": "ImagePullBackOff", "pods": 1,
                                       "example": "bad-image-1"}}

        responses = [
            reply(calls=[tool_call("scan_cluster", {"only_unhealthy": True})]),
            reply(content="bad-image is failing."),
        ]
        with patch.object(agent, "_resolve_entity", lambda n: None), \
             patch.dict(agent.TOOLS, {"scan_cluster": spy}), \
             mock_chat(side_effect=responses):
            agent.ask("Why is ghost-workload failing?")

        assert "workload" not in seen


class TestTheServiceAsymmetryIsDeliberate:
    """
    A workload-targeted run may read any service in its own namespace; a
    service-targeted run may read only the service it was asked about.

    That asymmetry looks like a hole and is not. The service fronting a
    workload is named differently by convention -- workload `crasher`, service
    `crasher-svc` -- so refusing on a name mismatch would refuse the single
    most useful call in diagnosing an unreachable workload. What bounds the
    exposure instead is the namespace, which is rewritten for these calls like
    every other.

    Mutation testing is why this is written down: nothing pinned either half,
    so `and` could become `or` in enforce() and no test noticed.
    """

    WORKLOAD = {"kind": "workload", "name": "crasher", "namespace": "demo"}
    SERVICE = {"kind": "service", "name": "crasher-svc", "namespace": "demo"}

    def test_a_workload_run_may_read_a_differently_named_service(self):
        """The convention case, and the reason the name is not checked."""
        _, violation = targeting.enforce(
            self.WORKLOAD, "get_service_endpoints",
            {"name": "crasher-svc", "namespace": "demo"})

        assert violation is None

    def test_a_workload_run_may_read_another_service_in_its_namespace(self):
        """
        Pinned as intended rather than tolerated. Diagnosing "why is this
        unreachable" legitimately reaches a service the workload talks to, and
        there is no reliable way to tell that from an unrelated one by name.
        """
        _, violation = targeting.enforce(
            self.WORKLOAD, "get_service_endpoints",
            {"name": "payments-svc", "namespace": "demo"})

        assert violation is None

    def test_but_not_one_in_another_namespace(self):
        """
        The namespace is what actually bounds it, so this is the assertion
        that carries the security claim.
        """
        arguments, violation = targeting.enforce(
            self.WORKLOAD, "get_service_endpoints",
            {"name": "payments-svc", "namespace": "other-team"})

        assert violation["action"] == "retargeted"
        assert arguments["namespace"] == "demo"

    def test_a_service_run_is_held_to_its_own_service(self):
        _, violation = targeting.enforce(
            self.SERVICE, "get_service_endpoints",
            {"name": "payments-svc", "namespace": "demo"})

        assert violation["action"] == "refused"

    def test_a_call_naming_no_service_is_not_refused(self):
        """
        There is nothing off-target about it, and refusing would push the
        model into guessing a name to satisfy the guard.
        """
        _, violation = targeting.enforce(
            self.SERVICE, "get_service_endpoints", {"namespace": "demo"})

        assert violation is None
