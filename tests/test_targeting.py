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

    def test_the_namespace_is_the_scan_key_and_not_the_workload_name(self):
        """
        `demo/memory-hog` splits at the slash and the namespace is the LEFT
        half. Taking the right half instead resolves every target into a
        namespace named after the workload, which does not exist -- the
        wrong-entity failure this module exists to prevent, arriving through
        the resolver rather than through the model. The existing workload case
        asserts only `kind`, so both halves of this line survived two mutation
        passes.
        """
        with patch.object(agent, "scan_cluster",
                          lambda **k: {"demo/memory-hog": {"status": "OOMKilled"}}):
            assert agent._resolve_entity("memory-hog") == {
                "kind": "workload", "namespace": "demo"}

    def test_the_truncation_marker_is_not_the_key_it_resolves_from(self):
        """
        A truncated scan carries `_truncated` alongside the workloads, and it
        is first in the document. Reading it as the workload key gives a name
        with no slash in it, so the target resolves with `namespace: None` and
        the run falls back to `default` -- which is the same live failure the
        service branch above was written for.
        """
        truncated = {"_truncated": "3 more not shown",
                     "demo/memory-hog": {"status": "OOMKilled"}}
        with patch.object(agent, "scan_cluster", lambda **k: truncated):
            assert agent._resolve_entity("memory-hog")["namespace"] == "demo"

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


class TestTheQuestionIsParsedTheWayItIsWritten:
    """
    Eight survivors in `targeting.py`, the module whose whole job is that the
    agent may choose HOW to investigate but not WHAT. Each of these is a place
    where a mutation makes it target something the question did not name.
    """

    def test_both_ways_of_writing_a_namespace_are_read(self):
        """
        `match.group(1) or match.group(2)` -- the two alternatives in the
        namespace pattern. Only one phrasing was ever asserted, so the pattern
        could have been reading the same group twice and half the questions
        people write would have lost their namespace.
        """
        assert targeting.target_of(
            "why is crasher failing in the demo namespace?")["namespace"] == "demo"
        assert targeting.target_of(
            "why is crasher failing in namespace demo?")["namespace"] == "demo"

    def test_the_name_and_the_kind_are_not_swapped(self):
        """
        The group-index table: `(_NAME_FIRST, 1, 2), (_KIND_FIRST, 2, 1)`. Both
        orderings have to read the name from their own name group. Swapping
        them makes the *kind* the target -- which is the failure recorded above
        the table, where kind-first ran first and "service unreachable" made
        the adjective the target.
        """
        for question in ("is the crasher deployment unhealthy?",
                         "is the deployment crasher unhealthy?"):
            target = targeting.target_of(question)
            assert target["name"] == "crasher", question
            assert target["kind"] == "workload"

    def test_a_bare_kind_word_is_not_a_workload_name(self):
        """
        `candidate in _ALL_KINDS or candidate == namespace`. "the deployment"
        with no name after it is a category, not an object, and targeting a
        workload literally called `deployment` sends every tool call at
        something no cluster has.
        """
        for question in ("is the deployment failing?",
                         "why is the statefulset broken?",
                         "is the cronjob unhealthy?"):
            assert targeting.target_of(question) is None, question

    def test_a_namespaced_name_compares_against_its_bare_half(self):
        """
        `parts[1] if len(parts) == 2 else value.lower()`. A tool asked about
        `demo/crasher` is asking about the same workload as `crasher`; taking
        the wrong half, or the wrong length, makes the guard retarget a call
        that was already correct.
        """
        assert targeting._same_workload("demo/crasher", "crasher") is True
        assert targeting._same_workload("demo/crasher", "log-shipper") is False
        # Three parts is not a namespace/name pair, so it falls through to a
        # full compare rather than guessing which segment is the name.
        assert targeting._same_workload("a/b/c", "c") is False

    def test_a_name_is_listed_once_however_often_it_is_asked_about(self):
        """
        `name in _NOT_A_NAME or name in _ALL_KINDS or name in seen`, joined by
        `or` so any one of them is enough to skip. Joined by `and` a name has
        to be all three, and a question mentioning one workload twice offers it
        as two candidates -- which `confirm` then refuses as ambiguous, so the
        run loses its target because the question repeated itself.
        """
        names = targeting.candidate_names(
            "why is log-shipper failing, and is log-shipper related to web-1?")

        assert names == ["log-shipper", "web-1"]

    def test_the_kind_is_read_from_the_kind_group(self):
        """
        Sharper than the case above, and it took a measurement to find out why.
        `_kind_of` returns "workload" for anything it does not recognise, so
        reading the kind from the *name* group still yields "workload" for an
        ordinary workload question and the swap is invisible. A service is what
        distinguishes them: read from the wrong group, `crasher-svc` is not a
        recognised kind and the target comes back a workload.
        """
        for question in ("is the crasher-svc service unreachable?",
                         "is the service crasher-svc unreachable?"):
            target = targeting.target_of(question)
            assert target["kind"] == "service", question
            assert target["name"] == "crasher-svc"

    def test_a_kind_word_in_the_name_position_is_still_not_a_name(self):
        """
        `candidate in _ALL_KINDS`, reached only when the pattern actually
        matches a name/kind pair. "the pod deployment" matches with `pod` in
        the name position, and `pod` is a category however it is written.
        """
        assert targeting.target_of("is the pod deployment unhealthy?") is None

    def test_an_unscoped_call_is_scoped_and_a_scoped_one_is_left_alone(self):
        """
        `if not given` in `enforce`. Inverted, the guard fills in an argument
        the model already supplied and leaves an empty one empty -- so a call
        that was correctly scoped gets rewritten, and the unscoped call this
        exists to catch goes out unscoped. Both directions, because only the
        pair distinguishes them.
        """
        target = {"kind": "workload", "name": "crasher", "namespace": "demo"}

        arguments, note = targeting.enforce(
            target, "list_pods", {"namespace": "demo"})
        assert arguments["workload"] == "crasher"
        assert note["action"] == "retargeted"
        assert "not scoped" in note["reason"]

        arguments, note = targeting.enforce(
            target, "list_pods", {"namespace": "demo", "workload": "crasher"})
        assert arguments["workload"] == "crasher"
        assert note is None, "a call already scoped to the target was rewritten"

    def test_a_second_resolving_candidate_stops_the_lookups(self):
        """
        `if len(found) > 1: return None` inside the loop. The outcome is
        already guarded after it -- `len(found) != 1` refuses the same case --
        so what this line buys is not the refusal but the *short circuit*: once
        two candidates have resolved the answer is settled, and every further
        `resolver` call is a cluster lookup whose result cannot change it.
        """
        calls = []

        def resolver(name):
            calls.append(name)
            return {"kind": "workload", "namespace": "demo"}

        assert targeting.confirm(["a-one", "b-two", "c-three"], resolver) is None
        assert calls == ["a-one", "b-two"], (
            "it kept resolving after the answer was settled")
