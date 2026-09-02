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

    def test_an_absence_claim_attaches_to_the_nearest_named_thing(self):
        """
        A live false positive, from the first 432 real runs this stage ever
        saw. The clause is the *correct* answer to the stuck-volume case: the
        ConfigMap does not exist, the pod very much does. `ConfigMap` is not
        one of the entity kinds the pattern knows, so the rule paired "does
        not exist" with the only labelled entity it could find -- the pod.
        """
        pod = ("describe_pod", {
            "pod": "missing-configmap-volume", "namespace": "config-faults",
            "status": "ContainerCreating"})
        answer = ("The pod `missing-configmap-volume` is stuck in "
                  "**ContainerCreating** because the ConfigMap `nginx-conf` "
                  "referenced in its volume configuration does not exist in "
                  "the `config-faults` namespace.")

        assert grounding.check(answer, ev(pod))["contradictions"] == []

    @pytest.mark.parametrize("marked", [
        "`nginx-conf`",
        "**nginx-conf**",
        "*nginx-conf*",
        '"nginx-conf"',
        "'nginx-conf'",
    ])
    def test_it_attaches_however_the_answer_marks_the_name_up(self, marked):
        """
        The fix above only recognised quotes and backticks, and the model
        writes the same clause in markdown bold about as often. Replayed over
        the 1469 recorded runs carrying a draft: the backticked spelling was
        let through and the bold one fired, four false contradictions on
        `stuck_volume_needs_events` and each of them against the correct
        answer to that case.
        """
        pod = ("describe_pod", {
            "pod": "missing-configmap-volume", "namespace": "config-faults",
            "status": "ContainerCreating"})
        answer = (f"The pod `missing-configmap-volume` is stuck because the "
                  f"ConfigMap {marked} referenced in its volume configuration "
                  f"does not exist in the `config-faults` namespace.")

        assert grounding.check(answer, ev(pod))["contradictions"] == []

    def test_an_undelimited_name_is_deliberately_not_covered(self):
        """
        The same false positive, and the trade to close it is worse than the
        cost of leaving it. The only test available for a bare name is its
        SHAPE, which would also swallow "the pod X in the config-faults
        namespace does not exist" -- where the absence really is about X.
        That buys a false negative on a true contradiction, which is the
        failure this rule exists to catch. Recorded here so the gap is a
        decision rather than an oversight.
        """
        pod = ("describe_pod", {
            "pod": "missing-configmap-volume", "namespace": "config-faults",
            "status": "ContainerCreating"})
        answer = ("The pod missing-configmap-volume is stuck because the "
                  "ConfigMap nginx-conf does not exist.")

        assert grounding.check(answer, ev(pod))["contradictions"] != []

    def test_the_entity_delimiter_cannot_pair_with_a_later_apostrophe(self):
        """
        Any-of-three let the pod's own closing backtick pair with the next
        apostrophe in the sentence, so the guard read an ordinary possessive
        as a second identifier and went quiet on a claim it should have
        caught. The delimiter has to close with itself.

        A residual this does not remove: two apostrophes in the same clause
        still pair with each other. The {2,} minimum keeps it rare and the
        cost is a missed contradiction rather than a false one, which is the
        safer direction for this rule to fail in.
        """
        pod = ("describe_pod", {"pod": "crasher-abc123", "namespace": "demo",
                                "status": "CrashLoopBackOff"})
        answer = ("The pod `crasher-abc123` in the kubelet's own view does "
                  "not exist.")

        found = grounding.check(answer, ev(pod))["contradictions"]

        assert found and found[0]["rule"] == "claimed_absent_but_measured_present"

    def test_a_bare_absence_claim_about_the_entity_still_fires(self):
        """The behaviour the fix must not cost."""
        pod = ("describe_pod", {"pod": "crasher-abc123", "namespace": "demo",
                                "status": "CrashLoopBackOff"})
        answer = "The pod `crasher-abc123` does not exist in the demo namespace."

        found = grounding.check(answer, ev(pod))["contradictions"]

        assert found and found[0]["rule"] == "claimed_absent_but_measured_present"

    def test_advice_about_avoiding_a_thing_is_not_a_claim_it_happened(self):
        """
        The second live false positive. Forward-looking advice, read as an
        assertion that the container had been OOM-killed. The negation window
        did not treat "avoid" as attenuating, and no recorded answer in the
        corpus had ever phrased it this way -- which is why only running live
        found it.
        """
        answer = ("- While not directly related to the exit code 1, ensure the "
                  "pod has sufficient resources (CPU/memory) to avoid "
                  "OOMKilled, though this is secondary to the connectivity "
                  "issue.")

        assert grounding.check(answer, ev(ERROR_POD))["contradictions"] == []

    @pytest.mark.parametrize("framing", [
        "ensure it has headroom to avoid OOMKilled",
        "raise the limit to prevent OOMKilled",
        "there is a risk of OOMKilled if traffic grows",
        "guard against OOMKilled by raising the limit",
    ])
    def test_prospective_framing_generally(self, framing):
        assert grounding.check(framing, ev(ERROR_POD))["contradictions"] == []

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


class TestTheOomSpellingsTheModelActuallyUses:
    """
    The tuple carried "oom killed" and "oom-killed" and nothing else in that
    shape, so an answer blaming "the OOM killer" -- the commonest English
    spelling of the same claim -- was not recognised at all.

    Found by measuring, not by reading. On
    scoping_quiet_workload_beside_loud_one at n=5, all five answers named OOM
    as the cause; two said "oomkilled" and were caught, three said "OOM
    killer" or "OOM kills" and came back `grounded`. The case scored 3/5 while
    every one of its answers was wrong, and the three passes looked exactly
    like a fix working. Replayed over the corpus the same hole had six more
    recorded runs scored `passed: True` on a contradicted claim, including
    gpt-4o-mini on this case.
    """

    KILLED = ("describe_pod", {
        "pod": "slow-starter-1", "namespace": "demo",
        "status": "CrashLoopBackOff",
        "containers": {"web": {"ready": False, "restarts": 5, "limits": {},
                               "last_termination": {"reason": "Error",
                                                    "exit_code": 137}}}})

    @pytest.mark.parametrize("wording", [
        "the container was killed by the OOM killer",
        "restarting due to Out-Of-Memory (OOM) kills",
        "this is an OOM kill",
        "the pod was OOMKilled",
        "restarting due to Out-Of-Memory (OOM) termination",
        "the container ran out of memory",
    ])
    def test_each_spelling_is_caught(self, wording):
        answer = f"The slow-starter deployment is restarting: {wording}."
        found = grounding.check(answer, ev(self.KILLED))["contradictions"]

        assert found, f"not recognised: {wording!r}"
        assert found[0]["rule"] == "termination_reason_vs_memory_cause"

    @pytest.mark.parametrize("wording", [
        "set a memory limit to avoid the OOM killer",
        "raise the limit to prevent an OOM kill",
        "this was not an OOM kill",
    ])
    def test_the_negation_and_advice_guards_still_hold(self, wording):
        """The widened stem must not cost the two guards already measured."""
        answer = f"The slow-starter deployment is restarting. {wording}."

        assert grounding.check(answer, ev(self.KILLED))["contradictions"] == []

    def test_a_genuinely_oomkilled_pod_is_left_alone(self):
        """
        The rule only fires when the recorded reason is something OTHER than
        an imposed termination. A pod the kubelet really did record as
        OOMKilled must not be contradicted for saying so, however spelled.
        """
        oom = ("describe_pod", {
            "pod": "memory-hog-1", "namespace": "demo", "status": "OOMKilled",
            "containers": {"web": {"ready": False, "restarts": 4,
                                   "last_termination": {"reason": "OOMKilled",
                                                        "exit_code": 137}}}})
        answer = "memory-hog was killed by the OOM killer."

        assert grounding.check(answer, ev(oom))["contradictions"] == []


class TestTheFactsMutationTestingFoundUntested:
    """
    Survivors from `evals/mutate.py contradiction.py`, 2026-08-30. Each one is
    a mutation the suite did not notice, on a line whose behaviour is real and
    reachable — the harness reports them as questions, and these were the ones
    whose answer was "no test covers this".
    """

    def facts_for(self, doc):
        import contradiction
        return contradiction.facts([{"text": json.dumps(doc)}])

    def test_one_unready_container_makes_the_whole_pod_unready(self):
        """
        `found["ready"] = found.get("ready", True) and state`. Swapping the
        `and` for an `or` lets a single ready container mark a pod ready while
        another is failing, which would silence `ready_vs_claimed_not_ready`
        on exactly the pods it exists for. Nothing tested a pod with two
        containers in different states.
        """
        mixed = {"pod": "p", "namespace": "demo", "status": "Running",
                 "containers": {"a": {"ready": True}, "b": {"ready": False}}}

        assert self.facts_for(mixed)["ready"] is False

    def test_every_container_ready_makes_the_pod_ready(self):
        """The control, so the test above cannot pass by always being False."""
        both = {"pod": "p", "namespace": "demo", "status": "Running",
                "containers": {"a": {"ready": True}, "b": {"ready": True}}}

        assert self.facts_for(both)["ready"] is True

    def test_a_claim_at_the_very_start_of_the_answer_still_counts(self):
        """
        `_asserted` guards with `start < 0`. As `start <= 0` a claim opening
        the sentence reads as not asserted, so an answer that leads with the
        wrong cause — the most emphatic place to put it — would not be
        contradicted at all.
        """
        pod = ("describe_pod", {
            "pod": "slow-starter-1", "namespace": "demo",
            "status": "CrashLoopBackOff",
            "containers": {"web": {"ready": False, "restarts": 5,
                                   "last_termination": {"reason": "Error",
                                                        "exit_code": 137}}}})
        answer = "OOMKilled is why slow-starter-1 keeps restarting."

        found = grounding.check(answer, ev(pod))["contradictions"]

        assert found and found[0]["rule"] == "termination_reason_vs_memory_cause"

    def test_the_evidence_saying_it_is_missing_is_not_presence(self):
        """
        `_entity_present` skips an entry whose text says the thing was not
        found. Without it the absence rule contradicts a correct answer using
        the very tool result that agrees with it.
        """
        pod = ("describe_pod", {"error": 'pods "ghost-pod" not found'})
        answer = "The pod `ghost-pod` does not exist in the demo namespace."

        assert grounding.check(answer, ev(pod))["contradictions"] == []


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


class TestAbsenceTheEvidenceConfirms:
    """
    The other half of the endpoint measurement.

    Asked about `typo-svc`, both qwen3 and gpt-4o-mini answered correctly --
    the selector matches no pods -- and both were scored
    `insufficient_evidence`, which reads as "nothing here could be checked".
    But `get_service_endpoints` had returned `ready_endpoints: []` and
    `not_ready_endpoints: []`, which is precisely the measurement that settles
    it. The contract could say an absence was CONTRADICTED and had no way to
    say it was SUPPORTED. 45 recorded runs were affected.
    """

    EMPTY = ("get_service_endpoints", {
        "service": "typo-svc", "namespace": "demo",
        "selector": {"app": "web-frontend"},
        "ready_endpoints": [], "not_ready_endpoints": []})
    UNREADY_ONLY = ("get_service_endpoints", {
        "service": "crasher-svc", "namespace": "demo",
        "selector": {"app": "crasher"},
        "ready_endpoints": [], "not_ready_endpoints": ["10.244.0.12"]})
    POPULATED = ("get_service_endpoints", {
        "service": "healthy-svc", "namespace": "demo",
        "selector": {"app": "healthy"},
        "ready_endpoints": ["10.244.0.20"], "not_ready_endpoints": []})

    def test_a_confirmed_absence_is_an_observation_not_silence(self):
        answer = ("The `typo-svc` service has no endpoints because its "
                  "selector matches no pods in that namespace.")

        verdict = grounding.check(answer, ev(self.EMPTY))

        assert verdict["confidence"] == "grounded"
        assert verdict["checked"] >= 1

    def test_the_observation_cites_the_field_that_settled_it(self):
        """
        This asserted `ready_endpoints` alone until 2026-09-02, which was the
        literal the code passed rather than the field the claim was settled
        from. "has no endpoints" names no readiness, so both lists had to be
        empty for it to hold and both are named. See
        TestACitationNamesTheFieldTheNumberCameFrom for the contradiction half,
        where citing one list was not merely partial but wrong.
        """
        answer = "The `typo-svc` service has no endpoints."

        claims = grounding.check(answer, ev(self.EMPTY))["claims"]
        absence = [c for c in claims if c.get("kind") == "absence"]

        assert absence, "the confirmed absence was not recorded as a claim"
        assert absence[0]["evidence"][0]["tool"] == "get_service_endpoints"
        assert absence[0]["evidence"][0]["field"] == (
            "ready_endpoints, not_ready_endpoints")

    def test_no_ready_endpoints_is_true_when_the_only_endpoint_is_unready(self):
        """
        The nine false positives. `crasher-svc` has one endpoint and it is not
        ready, so "has no ready endpoints" is exactly correct -- and counting
        ready and not-ready together called nine correct answers wrong.
        """
        answer = ("The crasher-svc service is unreachable because it has no "
                  "ready endpoints.")

        verdict = grounding.check(answer, ev(self.UNREADY_ONLY))

        assert verdict["contradictions"] == []
        assert verdict["confidence"] != grounding.CONTRADICTED

    def test_claiming_no_pods_at_all_is_still_contradicted_by_an_unready_one(self):
        """The protection the fix must not cost: a pod that exists, exists."""
        answer = "The crasher-svc service matches no pods."

        found = grounding.check(answer, ev(self.UNREADY_ONLY))["contradictions"]

        assert found and found[0]["rule"] == "service_has_endpoints_vs_claimed_none"

    def test_claiming_no_endpoints_of_a_populated_service_is_contradicted(self):
        answer = "The healthy-svc service has no endpoints."

        found = grounding.check(answer, ev(self.POPULATED))["contradictions"]

        assert found and found[0]["rule"] == "service_has_endpoints_vs_claimed_none"

    def test_a_heading_is_not_an_assertion(self):
        """
        Recorded in think-ON-n12.json: `- **No Endpoints**:` as a section
        label. A bare phrase with no verb asserts nothing, and scoring it as a
        claim called a correct answer contradicted.
        """
        answer = ("- **No Endpoints**: the service is unreachable.\n"
                  "The pod is not ready.")

        assert grounding.check(answer, ev(self.UNREADY_ONLY))["contradictions"] == []

    def test_a_conditional_is_not_an_assertion(self):
        """Also recorded: "If no endpoints, investigate the database"."""
        answer = "If no endpoints, investigate the database deployment."

        assert grounding.check(answer, ev(self.UNREADY_ONLY))["contradictions"] == []

    def test_an_absence_is_not_confirmed_from_silence(self):
        """
        The tool has to have been called. Confirming an absence because nothing
        was measured is the unfalsifiable tick this whole module exists to
        prevent -- and it would turn every unchecked claim into a green one.
        """
        unrelated = ("list_pods", {"some-pod": {"status": "Running"}})
        answer = "The typo-svc service has no endpoints."

        verdict = grounding.check(answer, ev(unrelated))

        assert verdict["confidence"] == grounding.INSUFFICIENT
        assert not [c for c in verdict["claims"] if c.get("kind") == "absence"]

    def test_a_confirmation_never_softens_a_contradicted_answer(self):
        """
        An answer can state one absence correctly and another wrongly. The
        verdict is CONTRADICTED either way, and a confirmed absence displayed
        beside it reads as partial support for an answer that is wrong.
        """
        answer = ("The crasher-svc service has no ready endpoints. "
                  "The container exited with an application error.")

        verdict = grounding.check(answer, ev(self.UNREADY_ONLY, OOM_POD))

        if verdict["confidence"] == grounding.CONTRADICTED:
            assert not [c for c in verdict["claims"] if c.get("kind") == "absence"]


class TestACitationNamesTheFieldTheNumberCameFrom:
    """
    A citation is an instruction to the operator. The console renders it as
    `tool.field` beside the finding (`ui._cite`), so naming a field means:
    open that result, read that field, and you will see what I saw.

    Both endpoint rules cited `ready_endpoints` unconditionally, while the
    count behind them came from `ready_endpoints` and `not_ready_endpoints`
    together whenever the claim did not name readiness. The module decided
    which measurement settled the claim, wrote it into a local, and then
    passed the literal instead -- so a finding reading "reported 1
    endpoint(s)" pointed at a field holding `[]`.
    """

    UNREADY_ONLY = {
        "service": "crasher-svc", "namespace": "demo",
        "selector": {"app": "crasher"},
        "ready_endpoints": [], "not_ready_endpoints": ["10.244.0.12"]}
    EMPTY = {
        "service": "typo-svc", "namespace": "demo",
        "selector": {"app": "web-frontend"},
        "ready_endpoints": [], "not_ready_endpoints": []}

    @staticmethod
    def _counted(cited, result):
        """
        How many endpoints the cited fields actually hold.

        Every name in the citation has to be a key of the tool result -- a
        derived counter like `endpoints_total` is a name this module made up
        and an operator cannot find, which is the same failure in a politer
        form.
        """
        names = [n.strip() for n in cited.split(",")]
        assert names, "the finding cited no field at all"
        for name in names:
            assert name in result, (
                f"cited {name!r}, which is not a field of the tool result")
        return sum(len(result[name]) for name in names)

    def test_a_general_contradiction_cites_the_fields_it_counted(self):
        """
        The failing case. "matches no pods" names no readiness, so the count
        is both lists -- one endpoint, and it is in `not_ready_endpoints`.
        Citing `ready_endpoints` alone sends the operator to an empty list.
        """
        answer = "The crasher-svc service matches no pods."

        found = grounding.check(
            answer, ev(("get_service_endpoints", self.UNREADY_ONLY))
        )["contradictions"]

        assert found and found[0]["rule"] == "service_has_endpoints_vs_claimed_none"
        assert "reported 1 endpoint(s)" in found[0]["measured"]
        counted = self._counted(found[0]["evidence"][0]["field"], self.UNREADY_ONLY)
        assert counted == 1, (
            f"the finding reports 1 endpoint; its citation accounts for {counted}")

    def test_a_general_confirmation_cites_both_lists_it_had_to_read(self):
        """
        An absence confirmed from one of two lists is confirmed from silence
        about the other, which is the tick this module exists to prevent. Both
        were read; both are named.
        """
        answer = "The `typo-svc` service has no endpoints."

        claims = grounding.check(
            answer, ev(("get_service_endpoints", self.EMPTY))
        )["claims"]
        absence = [c for c in claims if c.get("kind") == "absence"]

        assert absence, "the confirmed absence was not recorded as a claim"
        cited = absence[0]["evidence"][0]["field"]
        assert self._counted(cited, self.EMPTY) == 0
        assert "not_ready_endpoints" in cited, (
            "an absence of endpoints is settled by both lists being empty")

    def test_a_readiness_claim_still_cites_readiness_alone(self):
        """
        The other half of the contract, and the reason the literal was nearly
        right: a claim that names readiness is about `ready_endpoints`, and
        widening every citation to both lists would make this one wrong in the
        opposite direction -- `not_ready_endpoints` holds an endpoint that has
        no bearing on it.
        """
        answer = ("The crasher-svc service is unreachable because it has no "
                  "ready endpoints.")

        claims = grounding.check(
            answer, ev(("get_service_endpoints", self.UNREADY_ONLY))
        )["claims"]
        absence = [c for c in claims if c.get("kind") == "absence"]

        assert absence, "the confirmed absence was not recorded as a claim"
        assert absence[0]["evidence"][0]["field"] == "ready_endpoints"
        assert self._counted("ready_endpoints", self.UNREADY_ONLY) == 0
