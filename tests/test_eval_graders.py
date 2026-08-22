"""
Tests for the eval graders.

The graders decide what counts as a pass, so a wrong one does not fail loudly
-- it publishes a plausible number that means nothing. `validate.py` already
treats the suite as code; this treats the scoring the same way.

No cluster and no model: the graders are pure functions over a finding dict.
"""

import importlib.util
import json
import os

EVALS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evals")


def _load(name):
    """Import a script from evals/, which is not a package on sys.path."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(EVALS, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller_eval = _load("run_controller_eval")
tools_named_but_not_called = controller_eval.tools_named_but_not_called
grade = controller_eval.grade

agent_eval = _load("run_eval")
grade_answer = agent_eval.grade


def finding(diagnosis, tool_calls):
    return {"diagnosis": diagnosis, "tool_calls": tool_calls}


class TestToolsNamedButNotCalled:
    """
    Reports a fact and takes no view on what it means.

    Seen live on GKE: for a failing CronJob the controller delivered a numbered
    list of tool calls to make. Every tool was available; none were used. But
    the same signal fires on an answer that diagnoses and then suggests a next
    step, so the verdict belongs in grade(), which knows the root cause.
    """

    def test_the_gke_failure_is_caught(self):
        """The observed text, near enough verbatim."""
        answer = (
            "To find the root cause: 1. Check termination reason: call "
            "describe_pod on the pod. 2. Inspect logs: use get_pod_logs to see "
            "why it exited."
        )

        assert tools_named_but_not_called(finding(answer, [])) == [
            "describe_pod",
            "get_pod_logs",
        ]

    def test_a_tool_that_was_actually_called_is_not_flagged(self):
        """
        Referring to a tool you used is reporting, not planning.

        This is the check that keeps the detector from firing on every honest
        answer that names its sources.
        """
        answer = "get_pod_logs shows 'FATAL: upstream returned 503', so the sync failed."

        assert tools_named_but_not_called(finding(answer, ["get_pod_logs"])) == []

    def test_a_plain_diagnosis_is_not_flagged(self):
        answer = "The container exited 1 after the upstream returned 503."

        assert tools_named_but_not_called(finding(answer, ["get_pod_logs"])) == []

    def test_it_reports_only_the_tools_that_were_skipped(self):
        answer = "describe_pod shows exit code 1. Next, run get_pod_logs to see why."

        assert tools_named_but_not_called(
            finding(answer, ["describe_pod"])
        ) == ["get_pod_logs"]

    def test_an_empty_diagnosis_does_not_explode(self):
        """diagnose() can return an answer of None; the grader still has to run."""
        assert tools_named_but_not_called({"diagnosis": None, "tool_calls": []}) == []
        assert tools_named_but_not_called({}) == []


class TestAPlanAndAPostscriptAreNotTheSameThing:
    """
    The conflation this detector shipped with.

    Both behaviours name a tool they did not call. One delivers an alert with
    no root cause in it; the other delivers the root cause and adds a
    suggestion. Grading them alike meant a correct answer could fail for
    being helpful, which is how it came to be mistrusted.
    """

    CASE = {
        "workload": "nightly-sync",
        "expect_all": [["503", "upstream"]],
    }

    def _grade(self, answer, tool_calls):
        return grade(
            self.CASE,
            {"diagnosis": answer, "tool_calls": tool_calls, "confidence": "grounded"},
            f"nightly-sync is unhealthy in demo\n{answer}",
        )

    def test_a_plan_with_no_root_cause_still_fails(self):
        answer = (
            "To find the root cause: 1. Check termination reason: call "
            "describe_pod. 2. Inspect logs: use get_pod_logs."
        )
        ok, failures, notes = self._grade(answer, [])

        assert ok is False
        assert any("instead of calling them" in f for f in failures)
        assert notes == []

    def test_a_diagnosis_that_suggests_a_next_step_passes(self):
        """
        The false positive. The root cause is here and it is correct; the
        last sentence is a suggestion, not a substitute for the answer.
        """
        answer = (
            "The sync failed because the upstream returned 503. "
            "Next, run get_service_endpoints to confirm the upstream is down."
        )
        ok, failures, notes = self._grade(answer, ["get_pod_logs"])

        assert ok is True, failures
        assert notes == [
            "suggested get_service_endpoints as a next step, after reporting "
            "the root cause"
        ]

    def test_a_clean_diagnosis_produces_neither(self):
        answer = "The sync failed because the upstream returned 503."
        ok, failures, notes = self._grade(answer, ["get_pod_logs"])

        assert (ok, failures, notes) == (True, [], [])

    def test_a_wrong_answer_is_not_excused_by_naming_no_tools(self):
        """Missing substance fails on its own; the plan check is not load-bearing."""
        answer = "The pod is unhealthy."
        ok, failures, notes = self._grade(answer, ["get_pod_logs"])

        assert ok is False
        assert any("missing" in f for f in failures)
        assert not any("instead of calling them" in f for f in failures)


class TestForbidReadsAgainstTheAnswer:
    """
    The same conflation, in the other eval, found by measuring it.

    `healthy_workload_not_substituted` asks what is wrong with a workload that
    is fine, in a namespace full of workloads that are not. Its forbid list
    holds the neighbours' names, and a bare substring match cannot tell

        "the issue is that bad-image cannot pull its image"     -- substitution
        "healthy-web is fine; bad-image is unhealthy"           -- an aside

    apart. All four failures recorded with their answer text -- 2 of 30 in a
    replay probe, 2 of 20 live -- were the second, so the number this case
    published described the grader rather than the agent.
    """

    CASE = {
        "name": "healthy_workload_not_substituted",
        "expect_any": ["no issue", "healthy", "running", "fine"],
        "forbid": ["memory-hog", "crasher", "bad-image", "oomkilled"],
    }

    def _grade(self, answer, tools=()):
        return grade_answer(
            self.CASE,
            {"answer": answer, "tool_calls": [{"name": t} for t in tools]},
        )

    def test_the_verdict_plus_a_true_aside_is_a_note(self):
        """Recorded live on 2026-08-17, near enough verbatim."""
        answer = (
            "The healthy-web deployment in the demo namespace is running "
            "normally with 2 ready replicas. No issues detected. Other "
            "deployments in demo (like bad-image, memory-hog) are unhealthy "
            "and may require investigation."
        )
        ok, failures, notes = self._grade(answer)

        assert ok is True, failures
        assert notes == [
            "named 'memory-hog' alongside the answer",
            "named 'bad-image' alongside the answer",
        ]

    def test_the_substitution_still_fails(self):
        """No verdict on the workload asked about, and a fault for another."""
        answer = (
            "The issue is that the image nginx:this-tag-does-not-exist cannot "
            "be pulled, so bad-image is stuck in ImagePullBackOff."
        )
        ok, failures, notes = self._grade(answer)

        assert ok is False
        assert "wrongly claimed 'bad-image'" in failures
        assert notes == []

    def test_a_clean_verdict_produces_neither(self):
        ok, failures, notes = self._grade("healthy-web is running normally.")

        assert (ok, failures, notes) == (True, [], [])

    def test_a_case_with_no_expectations_keeps_forbid_hard(self):
        """
        Nothing to condition on means nothing is softened. Otherwise adding a
        forbid-only case would quietly produce a check that can never fail.
        """
        case = {"name": "no_expectations", "forbid": ["oomkilled"]}
        ok, failures, notes = grade_answer(
            case, {"answer": "the pod was oomkilled", "tool_calls": []}
        )

        assert ok is False
        assert failures == ["wrongly claimed 'oomkilled'"]
        assert notes == []

    def test_an_unmet_expect_all_group_also_hardens_forbid(self):
        """
        `answered` is every positive expectation, not just expect_any -- a case
        that got half of what it asked for has not answered the question.
        """
        case = {
            "name": "half_right",
            "expect_any": ["healthy"],
            "expect_all": [["2 replicas"]],
            "forbid": ["memory-hog"],
        }
        ok, failures, notes = grade_answer(
            case,
            {"answer": "healthy-web is healthy. memory-hog is not.", "tool_calls": []},
        )

        assert ok is False
        assert "wrongly claimed 'memory-hog'" in failures
        assert notes == []

    def test_tool_expectations_are_untouched_by_the_verdict(self):
        """A correct-sounding answer built from nothing still fails."""
        case = dict(self.CASE, expect_tools=["scan_cluster"])
        ok, failures, _ = grade_answer(
            case, {"answer": "healthy-web is running normally.", "tool_calls": []}
        )

        assert ok is False
        assert failures == ["never called scan_cluster"]


scan_probe = _load("probe_scan_summary")
entries_of = scan_probe.entries_of
named_in = scan_probe.named_in


class TestScanSummaryProbeReadsTheToolResult:
    """
    The probe's whole claim is "the tool returned these and the answer named
    those", so a parsing slip here does not produce a wrong number -- it
    produces a defect that is not there. Both halves are worth pinning: an
    error mistaken for an empty list would score every run as dropping
    everything, and a workload named only by its example pod would be counted
    as dropped when it was carried.
    """

    LISTING = json.dumps({
        "demo/nightly-sync": {"status": "Error", "pods": 2,
                              "example": "nightly-sync-297-abc", "fault": "crash"},
        "demo/never-ready": {"status": "Running", "pods": 1,
                             "example": "never-ready-7d8-vfp"},
        "_truncated": "3 more not shown, across 1 namespace(s)",
    })

    def test_position_is_the_order_the_model_read(self):
        entries = entries_of(self.LISTING)

        assert [e["workload"] for e in entries] == ["nightly-sync", "never-ready"]
        assert [e["position"] for e in entries] == [0, 1]

    def test_truncation_notice_is_not_a_workload(self):
        assert all(e["key"] != "_truncated" for e in entries_of(self.LISTING))

    def test_fault_falls_back_to_status_when_it_adds_nothing(self):
        """
        `scan_cluster` omits `fault` where it would only repeat `status`, so a
        probe reading `fault` directly would bucket those entries under None
        and report a fault class that does not exist.
        """
        entries = {e["workload"]: e for e in entries_of(self.LISTING)}

        assert entries["nightly-sync"]["fault"] == "crash"
        assert entries["never-ready"]["fault"] == "Running"

    def test_two_faults_on_one_workload_keep_their_own_rows(self):
        """`ns/name:fault` is a key shape scan_cluster really emits."""
        entries = entries_of(json.dumps({
            "demo/rollout:image-pull": {"status": "ImagePullBackOff", "pods": 1,
                                        "example": "rollout-new-1"},
            "demo/rollout:crash": {"status": "CrashLoopBackOff", "pods": 2,
                                   "example": "rollout-old-1"},
        }))

        assert [e["workload"] for e in entries] == ["rollout", "rollout"]
        assert [e["fault"] for e in entries] == ["image-pull", "crash"]

    def test_an_error_is_not_an_empty_cluster(self):
        """
        The difference between "the tool listed nothing" and "the tool
        failed" is the difference between a run that dropped every entry and
        a run that was never given any.
        """
        assert entries_of(json.dumps({"error": "kubernetes API error 404"})) == []
        assert entries_of(json.dumps({"result": "no unhealthy workloads"})) == []
        assert entries_of("not json at all") == []

    def test_naming_the_example_pod_counts_as_carried(self):
        entry = entries_of(self.LISTING)[1]

        assert named_in("never-ready-7d8-vfp is not passing readiness", entry)
        assert named_in("demo/never-ready is Running but not ready", entry)
        assert named_in("NEVER-READY is not ready", entry)
        assert not named_in("everything else looks fine", entry)


class TestScanSummaryProbeShuffle:
    """
    The shuffle exists to break the confound between position and identity,
    and it is only evidence if it changes nothing else about the result.
    """

    RESULT = {
        "demo/a": {"status": "CrashLoopBackOff", "pods": 1, "example": "a-1"},
        "demo/b": {"status": "Error", "pods": 2, "example": "b-1"},
        "demo/c": {"status": "Running", "pods": 1, "example": "c-1"},
        "_truncated": "1 more not shown, across 1 namespace(s)",
    }

    def _wrap(self, seed, result):
        import agent

        real = agent.TOOLS["scan_cluster"]
        try:
            agent.TOOLS["scan_cluster"] = lambda **kwargs: result
            return scan_probe.shuffled(seed)()
        finally:
            agent.TOOLS["scan_cluster"] = real

    def test_the_entries_survive_the_permutation(self):
        out = self._wrap(1, self.RESULT)

        assert dict(out) == dict(self.RESULT)

    def test_truncation_notice_stays_last(self):
        """It is a message about the list, not a member of it."""
        for seed in range(8):
            assert list(self._wrap(seed, self.RESULT))[-1] == "_truncated"

    def test_some_seed_reorders_the_workloads(self):
        orders = {tuple(self._wrap(seed, self.RESULT)) for seed in range(8)}

        assert len(orders) > 1

    def test_an_error_passes_through_untouched(self):
        failure = {"error": "kubernetes API error 404"}

        assert self._wrap(0, failure) == failure


class TestFabricatedDetailsAreScored:
    """
    Until 2026-08-19 this suite scored the root cause and ignored the
    grounding verdict entirely, so a correct RCA wrapped around an invented
    figure passed. All three observed cases were that shape.
    """

    CASE = {
        "name": "oom",
        "expect_any": ["oomkilled"],
        "require_grounded": True,
    }

    def test_a_fabricated_figure_fails_a_strict_case(self):
        ok, failures, _ = grade_answer(self.CASE, {
            "answer": "memory-hog was OOMKilled at its 512Mi limit.",
            "tool_calls": [],
            "unverified": ["512"],
        })

        assert ok is False
        assert any("unverified" in f for f in failures)

    def test_the_same_answer_measured_passes(self):
        ok, failures, _ = grade_answer(self.CASE, {
            "answer": "memory-hog was OOMKilled at its 64Mi limit.",
            "tool_calls": [],
            "unverified": [],
        })

        assert ok is True
        assert failures == []

    def test_a_lenient_case_records_it_as_a_note_instead(self):
        """
        The ten original cases keep measuring what they measured, so every
        published number stays comparable -- but the fabrication is still
        visible rather than silently dropped.
        """
        lenient = {"name": "oom", "expect_any": ["oomkilled"]}
        ok, failures, notes = grade_answer(lenient, {
            "answer": "memory-hog was OOMKilled at its 512Mi limit.",
            "tool_calls": [],
            "unverified": ["512"],
        })

        assert ok is True
        assert failures == []
        assert any("512" in note for note in notes)

    def test_a_wrong_answer_still_fails_on_the_root_cause(self):
        ok, failures, _ = grade_answer(self.CASE, {
            "answer": "The pod is fine.", "tool_calls": [], "unverified": [],
        })

        assert ok is False
        assert any("oomkilled" in f for f in failures)

class TestControllerGraderAcceptsEveryVerdict:
    """
    The controller grader reads grounding.VERDICTS rather than a tuple of its
    own. Pinned because the drift was silent: a correct diagnosis was reported
    as a failure, with a reason that reads like a defect in the agent.
    """

    def test_no_verdict_is_rejected_as_unusable(self):
        import grounding
        from evals.run_controller_eval import grade

        case = {"workload": "crasher", "expect_all": [["database"]]}
        for verdict in sorted(grounding.VERDICTS):
            finding = {"confidence": verdict, "diagnosis": "crasher cannot "
                       "reach the database", "tool_calls": ["get_pod_logs"]}
            ok, failures, _ = grade(case, finding,
                                    "crasher cannot reach the database")
            assert not any("no usable confidence" in f for f in failures), \
                f"{verdict} was rejected"


class TestFixturesArePresentBeforeAnyModelTime:
    """
    A case declares `needs` and, until 2026-08-22, nothing read it.

    A sixteen-case set ran against a cluster carrying only broken-pods.yaml
    and reported 36/48. Six cases were unreachable, and the damage was in both
    directions: four failed for want of a workload, and two *passed*, because
    "is this healthy?" is satisfied by an answer about a pod that is not there.
    A missing fixture is a silent defect, so the check has to refuse rather
    than warn.
    """

    def _cases(self):
        return [
            {"name": "plain", "needs": None},
            {"name": "adversarial", "needs": "demo/adversarial.yaml"},
        ]

    # The real return shape, and it is the whole point of these three. The
    # first version of this check asked `not isinstance(pods, dict)` and was
    # green, because the test fed it a list: list_pods() returns a dict keyed
    # by pod name, so the check called every populated namespace empty and the
    # eval refused to start.
    POPULATED = {"log-injector-85c5c67578-qb26b": {"status": "Error",
                                                   "ready": "0/1",
                                                   "restarts": 5}}

    def test_the_shape_list_pods_actually_returns_counts_as_populated(self):
        assert agent_eval.populated(self.POPULATED)
        assert not agent_eval.populated({})
        assert not agent_eval.populated({"error": "namespace not found"})

    def test_an_unapplied_fixture_is_reported_by_namespace(self, monkeypatch):
        monkeypatch.setattr(agent_eval.k8s, "list_pods", lambda namespace: {})
        absent = agent_eval.fixtures_present(self._cases())
        assert absent == {"demo/adversarial.yaml": ["adversarial", "adversarial-b"]}

    def test_a_populated_cluster_reports_nothing_missing(self, monkeypatch):
        monkeypatch.setattr(agent_eval.k8s, "list_pods",
                            lambda namespace: self.POPULATED)
        assert agent_eval.fixtures_present(self._cases()) == {}

    def test_an_error_from_the_api_counts_as_absent(self, monkeypatch):
        # list_pods returns {"error": ...} rather than raising. Treating that
        # dict as truthy would read a broken cluster as a populated one.
        monkeypatch.setattr(agent_eval.k8s, "list_pods",
                            lambda namespace: {"error": "namespace not found"})
        assert "demo/adversarial.yaml" in agent_eval.fixtures_present(self._cases())

    def test_cases_without_a_fixture_need_no_cluster_lookup(self, monkeypatch):
        def refuse(namespace):
            raise AssertionError("looked up a namespace for a case with no needs")

        monkeypatch.setattr(agent_eval.k8s, "list_pods", refuse)
        assert agent_eval.fixtures_present([{"name": "plain"}]) == {}


def _echo_satisfiable(case):
    """
    Whether repeating the question back would satisfy every expectation.

    One echoed term carries a whole group, since `expect_any` passes on one
    term and each `expect_all` group passes on one. But a case with a second
    group the question does not contain still bites, so the criterion is
    *every* group being echoable, not any of them -- which is what makes
    "name the pod you were asked about" a legitimate group to keep rather
    than a defect to remove.
    """
    question = case["question"].lower()
    groups = list(case.get("expect_all") or [])
    if case.get("expect_any"):
        groups.append(case["expect_any"])
    if not groups:
        return False
    return all(
        any(term.lower() in question for term in group) for group in groups
    )


class TestExpectationsThatTheQuestionAlreadySatisfies:
    """
    An expectation the question already satisfies tests nothing.

    Found 2026-08-22 by reading a void set rather than by suspecting it. Two
    cases scored PASS 3/3 while answering that the workload does not exist:
    `healthy_workload_with_no_logs` matched on `fine`, inside the workload's
    own name `quiet-and-fine`, and `unhealthy_question_about_a_healthy_pod`'s
    only expectation was the pod's name. Both have been repaired; these tests
    are what stop them, or a new case, from drifting back.

    The criterion is every group being echoable, not any of them. Requiring
    the answer to name its subject is legitimate -- it is what the second
    case is about -- as long as some other group asks for something the
    question does not already contain.
    """

    def test_no_case_can_be_passed_by_repeating_the_question(self):
        # All five are repaired as of 2026-08-22. The list is empty and has to
        # stay empty: a new case whose expectations its own question satisfies
        # fails here rather than quietly scoring a pass on an echo.
        from evals.cases import CASES

        offenders = sorted(c["name"] for c in CASES if _echo_satisfiable(c))
        assert offenders == [], (
            "these cases pass on an answer that only repeats the question: "
            f"{offenders}"
        )

    def test_the_two_repaired_cases_reject_a_does_not_exist_answer(self, ):
        """
        Production's grader, the answers that actually scored PASS, no cluster.

        These are the recorded texts from results/think-OFF-16cases-n3-VOID,
        where the fixtures had never been applied. Replayed over every answer
        either case has on record -- 40 of them across nine sets -- the new
        expectations change exactly these three plus three more of the same
        shape, and one further run that described the broken neighbours
        instead of the pod it was asked about. Every legitimate answer still
        passes.
        """
        from evals.cases import CASES

        recorded = {
            "healthy_workload_with_no_logs":
                'The workload "quiet-and-fine" does not exist in the '
                '"adversarial" namespace.',
            "unhealthy_question_about_a_healthy_pod":
                'The pod or workload named "correctly-configured" in the '
                'namespace "config-faults" does not exist in the cluster. '
                'Please verify the name and namespace of the workload.',
        }
        for case in CASES:
            if case["name"] not in recorded:
                continue
            result = {
                "answer": recorded[case["name"]],
                "tool_calls": [{"name": "scan_cluster", "arguments": {}}],
                "confidence": "grounded",
                "unverified": [],
            }
            passed, reasons, _ = grade_answer(case, result)
            assert not passed, f"{case['name']} still passes on a missing pod"

    def test_a_correct_answer_still_passes(self):
        # The other half, and the one that would catch an expectation tightened
        # into something no real answer says. Both texts are recorded runs.
        from evals.cases import CASES

        recorded = {
            "healthy_workload_with_no_logs":
                "The workload `quiet-and-fine` in the `adversarial` namespace "
                "is running normally. No issues were found.",
            "unhealthy_question_about_a_healthy_pod":
                "The pod `correctly-configured` in the `config-faults` "
                "namespace is **running normally**. It shows a status of "
                '"Running", with `1/1` ready containers and zero restarts.',
        }
        for case in CASES:
            if case["name"] not in recorded:
                continue
            result = {
                "answer": recorded[case["name"]],
                "tool_calls": [{"name": "scan_cluster", "arguments": {}}],
                "confidence": "grounded",
                "unverified": [],
            }
            passed, reasons, _ = grade_answer(case, result)
            assert passed, f"{case['name']} no longer passes a correct answer: {reasons}"

    def test_naming_the_subject_stays_allowed_as_one_group_of_several(self):
        # The rule must not forbid "answer about the pod you were asked
        # about", which is the point of the second case. A name-only group is
        # fine beside a group the question does not contain.
        case = {
            "question": "Is the correctly-configured pod unhealthy?",
            "expect_all": [["running normally"], ["correctly-configured"]],
        }
        assert not _echo_satisfiable(case)
        case["expect_all"] = [["correctly-configured"], ["unhealthy"]]
        assert _echo_satisfiable(case)
