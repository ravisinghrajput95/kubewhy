"""
Tests for the contradiction stage.

The rules are deterministic, so every case here is a crafted (answer, evidence)
pair. Two things are being pinned: that a real contradiction is caught, and --
harder, and the reason the first draft of this module was wrong -- that a
correct answer is not.

The false-positive half is not decoration. Replaying the recorded corpus
against the first version of these rules produced six false positives and zero
true ones; the rules that survived are the ones that replay could not break.
"""
import json

import pytest

import contradiction
import grounding


def ev(*pairs):
    return [{"id": f"tool-{i}", "tool": tool, "result": json.dumps(body)}
            for i, (tool, body) in enumerate(pairs, 1)]


OOM_POD = ("describe_pod", {
    "pod": "memory-hog-x", "namespace": "demo", "status": "CrashLoopBackOff",
    "containers": {"hog": {"limits": {"memory": "64Mi"},
                           "last_termination": {"reason": "OOMKilled",
                                                "exit_code": 137}}}})
ERROR_POD = ("describe_pod", {
    "pod": "crasher-x", "namespace": "demo", "status": "CrashLoopBackOff",
    "containers": {"crasher": {"last_termination": {"reason": "Error",
                                                    "exit_code": 1}}}})
HEALTHY_POD = ("list_pods", {
    "healthy-web-x": {"status": "Running", "ready": "1/1", "restarts": 0}})
SERVICE_WITH_ENDPOINTS = ("get_service_endpoints", {
    "service": "crasher-svc", "namespace": "demo", "selector": {"app": "crasher"},
    "ready_endpoints": ["10.244.0.12"], "not_ready_endpoints": []})


class TestTheFindingThisStageExistsFor:
    def test_an_oomkill_blamed_on_the_application_is_contradicted(self):
        """
        F-03, exactly as the adversarial report recorded it. Every value in
        this sentence was measured, so the old checker scored it grounded.
        """
        answer = ("The pod is in CrashLoopBackOff, which means the container "
                  "exited with an application error.")

        verdict = grounding.check(answer, ev(OOM_POD))

        assert verdict["confidence"] == grounding.CONTRADICTED
        found = verdict["contradictions"][0]
        assert found["rule"] == "imposed_termination_vs_application_cause"
        assert found["measured"] == "last_termination.reason = oomkilled"

    def test_the_contradiction_carries_a_citation(self):
        verdict = grounding.check(
            "The container exited with an application error.", ev(OOM_POD))

        cited = verdict["contradictions"][0]["evidence"][0]
        assert cited["tool"] == "describe_pod"
        assert cited["field"] == "last_termination.reason"

    def test_a_service_with_endpoints_is_not_a_service_without_pods(self):
        """
        A real recorded wrong answer, from think-OFF-16cases-n3. It scored
        insufficient_evidence -- "nothing to check" -- while the tool it had
        just called reported a ready endpoint.
        """
        answer = ("The `crasher-svc` service in the `demo` namespace does not "
                  "have any associated pods.")

        verdict = grounding.check(answer, ev(SERVICE_WITH_ENDPOINTS))

        assert verdict["confidence"] == grounding.CONTRADICTED
        assert verdict["contradictions"][0]["rule"] == \
            "service_has_endpoints_vs_claimed_none"

    def test_blaming_memory_when_the_container_chose_its_exit(self):
        verdict = grounding.check(
            "The container ran out of memory and was killed.", ev(ERROR_POD))

        assert verdict["confidence"] == grounding.CONTRADICTED
        assert verdict["contradictions"][0]["rule"] == \
            "termination_reason_vs_memory_cause"


class TestWhatMustNotBeCalledAContradiction:
    """
    Every one of these is a correct or defensible answer. A rule that fires
    here is worse than no rule at all: a checker that cries wolf on good work
    is one people learn to ignore, and then it protects nothing.
    """

    def test_hedged_reasoning_stays_an_inference(self):
        answer = ("The container exited with what is likely an application "
                  "error.")

        verdict = grounding.check(answer, ev(OOM_POD))

        assert verdict["confidence"] != grounding.CONTRADICTED
        assert verdict["contradictions"] == []

    def test_a_denied_phrase_is_not_an_asserted_one(self):
        """
        Found by corpus replay. "no OOMKilled ... reported" contains the word
        and asserts its absence; the first version of these rules matched on
        presence and scored a correct sentence as a contradiction.
        """
        answer = ("This is not a resource exhaustion issue (no OOMKilled or "
                  "memory limits reported); the container fails at runtime.")

        assert grounding.check(answer, ev(ERROR_POD))["contradictions"] == []

    def test_quoted_log_output_is_not_a_resource_claim(self):
        """
        The other corpus false positive, and the reason the numeric rule now
        requires the word `limit` or `request`. The stress fixture logs
        "dispatching hogs: 0 cpu, 0 io, 1 vm, 0 hdd", and six correct answers
        quoting it were read as claiming a CPU limit of zero.
        """
        pod = ("describe_pod", {
            "pod": "memory-hog-x", "namespace": "demo",
            "containers": {"hog": {"limits": {"cpu": "100m", "memory": "64Mi"}}}})
        answer = ("The log shows it dispatching hogs with 0 cpu, 0 io, 1 vm "
                  "and 0 hdd, which stresses the memory subsystem.")

        assert grounding.check(answer, ev(pod))["contradictions"] == []

    def test_a_genuinely_absent_thing_may_be_called_absent(self):
        """
        `nginx:this-tag-does-not-exist cannot be found` is the correct answer
        to the image-pull case, and the phrase is in the evidence itself.
        """
        pod = ("describe_pod", {
            "pod": "bad-image-x", "namespace": "demo",
            "status": "ImagePullBackOff",
            "containers": {"app": {"image": "nginx:this-tag-does-not-exist"}}})
        answer = ("The image `nginx:this-tag-does-not-exist` does not exist "
                  "in the registry.")

        assert grounding.check(answer, ev(pod))["contradictions"] == []

    def test_a_service_with_no_endpoints_may_be_called_empty(self):
        empty = ("get_service_endpoints", {
            "service": "typo-svc", "namespace": "demo",
            "selector": {"app": "web-frontend"},
            "ready_endpoints": [], "not_ready_endpoints": []})
        answer = "The service typo-svc does not have any associated pods."

        assert grounding.check(answer, ev(empty))["contradictions"] == []

    def test_silence_in_the_evidence_produces_no_finding(self):
        """
        A fact that is absent is absent. "The tools did not say" is what
        `unverified` already means, and inventing a contradiction from silence
        would make this stage the very thing it was written to catch.
        """
        bare = ("list_pods", {"mystery-x": {"status": "Running"}})
        answer = "The container exited with an application error."

        assert grounding.check(answer, ev(bare))["contradictions"] == []

    def test_a_correct_oomkill_diagnosis_stays_grounded(self):
        answer = ("The container hog was OOMKilled after exceeding its 64Mi "
                  "memory limit.")

        verdict = grounding.check(answer, ev(OOM_POD))

        assert verdict["confidence"] == "grounded"
        assert verdict["contradictions"] == []

    def test_a_healthy_pod_correctly_reported_healthy(self):
        answer = "The pod healthy-web-x is running normally with 1/1 ready."

        assert grounding.check(answer, ev(HEALTHY_POD))["contradictions"] == []


class TestTheStatusContract:
    def test_the_four_statuses_are_distinguishable(self):
        """
        SUPPORTED / CONTRADICTED / INSUFFICIENT / INFERENCE, as distinct claim
        statuses rather than one bucket.
        """
        answer = ("The container exited with an application error. It has a "
                  "137 exit code. It is possibly a memory leak.")

        claims = grounding.check(answer, ev(OOM_POD))["claims"]
        statuses = {c["status"] for c in claims}

        assert grounding.CONTRADICTED in statuses
        assert "observed" in statuses or "unverified" in statuses
        assert "inferred" in statuses

    def test_contract_surfaces_contradictions_separately(self):
        verdict = grounding.check(
            "The container exited with an application error.", ev(OOM_POD))
        contract = grounding.contract(verdict)

        assert contract["contradictions"]
        assert contract["contradictions"][0]["measured"] == \
            "last_termination.reason = oomkilled"
        # Not folded into unknowns: a reader scanning for what went wrong has
        # to find these before the observations.
        assert contract["contradictions"][0]["claim"] not in contract["unknowns"]

    def test_contradicted_is_a_recognised_verdict(self):
        assert grounding.CONTRADICTED in grounding.VERDICTS

    def test_a_contradiction_outranks_a_correct_claim_in_the_same_answer(self):
        """
        An answer may trace ten values and still be wrong about the eleventh.
        Reporting it as grounded because of the ten is the failure this stage
        removes.
        """
        answer = ("The pod memory-hog-x has a 64Mi memory limit and exit code "
                  "137. The container exited with an application error.")

        verdict = grounding.check(answer, ev(OOM_POD))

        assert verdict["confidence"] == grounding.CONTRADICTED
        assert any(c["status"] == "observed" for c in verdict["claims"])


class TestFactExtraction:
    @pytest.mark.parametrize("value,expected", [
        ("1/1", True), ("0/1", False), ("2/2", True), ("1/2", False),
        (True, True), (False, False), ("nonsense", None), (None, None),
    ])
    def test_readiness_in_every_shape_a_tool_reports_it(self, value, expected):
        assert contradiction._ready_fraction(value) is expected

    def test_one_unready_container_makes_the_pod_unready(self):
        entries = [{"text": json.dumps({
            "pod": "p", "containers": {"a": {"ready": True},
                                       "b": {"ready": False}}}),
            "source": {}}]

        assert contradiction.facts(entries)["ready"] is False
