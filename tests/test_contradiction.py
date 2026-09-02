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


class TestWhichThingTheAbsenceIsAbout:
    """
    `_absence_is_about` decides whether an absence phrase is talking about a
    given entity, and it is the guard that stopped the stuck-volume answer
    being called a contradiction. Its docstring says it was fixed by testing
    it directly rather than through a run; nothing in the suite did that, and
    six of the module's mutation survivors sat in its five lines.

    The cases below are its boundaries: the phrase missing, the name missing
    before it, and the name at index 0 of the clause -- where the search that
    finds it starts.
    """

    PHRASE = "does not exist"

    def test_a_clause_without_the_phrase_is_about_nothing(self):
        assert not contradiction._absence_is_about(
            "The pod `nginx-conf` is running.", self.PHRASE, "nginx-conf")

    def test_a_name_that_never_precedes_the_phrase_is_not_its_subject(self):
        """
        The name is in the clause, after the phrase. An absence attaches to
        the nearest named thing *before* it, so this is not its subject.
        """
        assert not contradiction._absence_is_about(
            "The ConfigMap does not exist, unlike `nginx-conf`.",
            self.PHRASE, "nginx-conf")

    def test_a_name_at_the_very_start_of_the_clause_is_still_its_subject(self):
        """
        The boundary three mutants live on. `rfind(name, 0, at)` searches from
        index 0, and a clause that opens with the entity puts it exactly
        there -- the model writes this shape often, because the sentence
        before it named the pod.
        """
        assert contradiction._absence_is_about(
            "nginx-conf does not exist in the config-faults namespace.",
            self.PHRASE, "nginx-conf")

    def test_a_marked_up_name_in_between_takes_the_absence_away(self):
        """The rule the function exists for, at the same starting index."""
        assert not contradiction._absence_is_about(
            "nginx-conf mounts `other-map`, which does not exist.",
            self.PHRASE, "nginx-conf")


def entries(*bodies):
    """What scan() hands the fact walk: scoped evidence, text and source."""
    return [{"text": json.dumps(b), "source": {"id": f"tool-{i}", "tool": "t"}}
            for i, b in enumerate(bodies, 1)]


class TestTheFactsTheEvidenceEstablishes:
    """
    `facts()` is a dispatch chain: each branch is a key test AND a type test,
    and every one of those guards was a mutation survivor. The chain reads
    every (key, value) pair in the document depth first and takes the first
    match with `setdefault`, so a guard that lets one pair too many through
    does not add a wrong fact beside the right one -- it wins the race and
    the right one is dropped.

    The document below is built to lose that race in every branch: a string
    before `status`, an integer before `restarts`, a dict that is not
    `last_termination`, a dict that is not `limits`, and a list that is not
    an endpoint list.
    """

    NOISY = {
        "pod": "crasher-x",
        "generation": 7,
        "namespace": "demo",
        "conditions": {"reason": "ContainersNotReady"},
        "requests": {"memory": "32Mi"},
        "tolerations": ["node.kubernetes.io/not-ready", "node.kubernetes.io/x"],
        "status": "CrashLoopBackOff",
        "containers": {
            "crasher": {
                "restarts": 3,
                "limits": {"memory": "64Mi"},
                "last_termination": {"reason": "OOMKilled", "exit_code": 137},
            }
        },
    }

    def test_only_a_status_key_becomes_the_status(self):
        found = contradiction.facts(entries(self.NOISY))

        assert found["status"] == "crashloopbackoff"

    def test_only_a_termination_becomes_the_termination_reason(self):
        """
        `conditions.reason` is `ContainersNotReady`, which is a true fact
        about the pod and not a reason anything terminated. It is walked
        first, so a guard that accepts any dict reports it as the cause of
        death and the rule that reads OOMKilled never sees it.
        """
        found = contradiction.facts(entries(self.NOISY))

        assert found["termination_reason"] == "oomkilled"
        assert found["exit_code"] == 137

    def test_only_a_limits_key_becomes_a_limit(self):
        found = contradiction.facts(entries(self.NOISY))

        assert found["limit_memory"] == "64Mi"
        assert [k for k in found if k.startswith("limit_")] == ["limit_memory"]

    def test_only_a_restart_count_becomes_the_restart_count(self):
        found = contradiction.facts(entries(self.NOISY))

        assert found["restarts"] == 3

    def test_a_list_that_is_not_an_endpoint_list_is_not_counted(self):
        """
        Two tolerations are not two endpoints. `endpoints_total` absent and
        `endpoints_total == 0` are different states -- absent means no service
        was read, and confirming an absence from a tool nobody called is the
        tick this module exists to prevent.
        """
        found = contradiction.facts(entries(self.NOISY))

        assert "endpoints_total" not in found

    def test_endpoints_are_counted_up(self):
        """
        Both counters accumulate. A subtraction here reads as a negative
        count, which is neither `> 0` nor `== 0`, so both endpoint rules go
        silent and no finding is produced at all.
        """
        found = contradiction.facts(entries({
            "service": "healthy-svc",
            "ready_endpoints": ["10.244.0.20"],
            "not_ready_endpoints": ["10.244.0.21", "10.244.0.22"]}))

        assert found["ready_endpoints_total"] == 1
        assert found["endpoints_total"] == 3


class TestReadinessInEveryShapeATtoolReportsIt:
    def test_no_containers_at_all_is_not_ready(self):
        """
        `0/0` is what a Deployment scaled to zero reports. Nothing is ready,
        because nothing is running -- and reading it as ready would say a
        workload with no pods is serving.
        """
        assert contradiction._ready_fraction("0/0") is False

    def test_every_container_ready_is_ready(self):
        assert contradiction._ready_fraction("2/2") is True

    def test_some_containers_ready_is_not(self):
        assert contradiction._ready_fraction("1/2") is False


class TestWhatCountsAsTheToolsReportingAnEntityExists:
    """
    `_entity_present` is what stops "the pod nginx-conf does not exist" being
    scored against a pod the tools never saw. It recognises three shapes, and
    two of them -- keyed by name, and named as a workload's example pod --
    had no test: their `return True` could be flipped to `return False` and
    the suite stayed green.

    Both shapes are what a real scan looks like. `scan_cluster` keys by
    `namespace/workload` and hangs the pod name off `example`, so a question
    about a pod reaches the second shape and a question about a workload the
    first.
    """

    def test_a_name_that_keys_a_document_is_present(self):
        found = entries({"crasher-1": {"status": "CrashLoopBackOff",
                                       "restarts": 7}})

        assert contradiction._entity_present(found, "crasher-1") is True

    def test_a_workloads_example_pod_is_present(self):
        found = entries({"demo/crasher": {"status": "CrashLoopBackOff",
                                          "pods": 1, "example": "crasher-1"}})

        assert contradiction._entity_present(found, "crasher-1") is True

    def test_a_document_about_a_pod_reports_it_present(self):
        found = entries({"pod": "crasher-1", "namespace": "demo"})

        assert contradiction._entity_present(found, "crasher-1") is True

    def test_the_evidence_saying_it_is_missing_is_not_presence(self):
        """The reason this is not a substring search."""
        found = entries({"pod": "volume-stuck", "events": [
            {"message": 'configmap "nginx-conf" not found'}]})

        assert contradiction._entity_present(found, "nginx-conf") is False

    def test_a_name_nothing_reported_is_not_present(self):
        found = entries({"demo/crasher": {"status": "CrashLoopBackOff"}})

        assert contradiction._entity_present(found, "payments-api") is False


READY_POD = ("describe_pod", {
    "pod": "healthy-web-abc123", "namespace": "demo", "status": "Running",
    "containers": {"web": {"ready": True}}})


class TestTwoRulesNothingDrove:
    """
    `ready_vs_claimed_not_ready` was named once in the suite, in another
    test's docstring, and `running_vs_claimed_failing` not at all. Neither had
    a case that made it fire, so `if known.get("ready") is True` could be
    inverted and both of the phrase lookups under it disabled, with the suite
    green.

    Each rule gets both halves, because the halves fail in opposite
    directions. The firing case proves the rule works; the denial case proves
    it is reading an assertion rather than a phrase -- the failure this whole
    module was rewritten for, where "no OOMKilled reported" was scored as a
    claim that the container was OOM-killed.
    """

    def test_a_pod_the_tools_report_ready_is_not_unready(self):
        answer = "The healthy-web pod is not ready, so the Service has no backend."

        found = grounding.check(answer, ev(READY_POD))["contradictions"]

        assert found and found[0]["rule"] == "ready_vs_claimed_not_ready"
        assert found[0]["measured"] == "ready reported true by the tools"

    def test_denying_unreadiness_is_not_claiming_it(self):
        """`nothing` sits inside the 40-character window before the phrase."""
        answer = "Nothing shows the pod is not ready; the failure is elsewhere."

        found = grounding.check(answer, ev(READY_POD))["contradictions"]

        assert [f for f in found if f["rule"] == "ready_vs_claimed_not_ready"] == []

    def test_a_running_ready_pod_is_not_failing(self):
        answer = "The healthy-web pod is failing and needs to be restarted."

        found = grounding.check(answer, ev(READY_POD))["contradictions"]

        assert found and found[0]["rule"] == "running_vs_claimed_failing"
        assert "status = running" in found[0]["measured"]

    def test_denying_failure_is_not_claiming_it(self):
        answer = "There is no sign the pod is failing; it is serving normally."

        found = grounding.check(answer, ev(READY_POD))["contradictions"]

        assert [f for f in found if f["rule"] == "running_vs_claimed_failing"] == []

    def test_denying_an_application_cause_is_not_claiming_one(self):
        """
        The rule the module was rewritten for, from the other side. The
        recorded sentence is "This is not a resource exhaustion issue (no
        OOMKilled or memory limits reported)": the phrase is present and is
        being denied. Its mirror -- an OOMKilled pod where the answer rules
        out an application error -- had no case.
        """
        answer = ("The container was OOMKilled. There was no application "
                  "error involved.")

        found = grounding.check(answer, ev(OOM_POD))["contradictions"]

        assert [f for f in found
                if f["rule"] == "imposed_termination_vs_application_cause"] == []

    def test_denying_an_absence_is_not_claiming_one(self):
        """
        The same half, for the existence rule. This one had firing cases and
        no denial case, so the assertion check in its phrase lookup could be
        dropped without the suite noticing.
        """
        answer = ("No evidence the pod `crasher-abc123` does not exist; it "
                  "is running and crashing.")
        pod = ("describe_pod", {"pod": "crasher-abc123", "namespace": "demo",
                                "status": "CrashLoopBackOff"})

        found = grounding.check(answer, ev(pod))["contradictions"]

        assert [f for f in found
                if f["rule"] == "claimed_absent_but_measured_present"] == []


class TestAPhraseThatIsNotThereIsNotAsserted:
    def test_a_phrase_absent_from_the_clause_is_not_asserted(self):
        assert contradiction._asserted("the pod is running", "is not ready") is False

    def test_a_phrase_present_and_undenied_is_asserted(self):
        assert contradiction._asserted("the pod is not ready", "is not ready") is True


class TestTheTwoHalvesAreNotInterchangeable:
    """
    `check()` and `confirmations()` are this module's public API and neither
    has a caller in the repository -- grounding.py calls `scan()` and takes
    both halves itself. Untested and uncalled, `check()` could return the
    confirmations and `confirmations()` could raise IndexError, and nothing
    anywhere would fail. An API nobody exercises is a claim, not a function.
    """

    ENDPOINTS = ("get_service_endpoints", {
        "service": "typo-svc", "namespace": "demo",
        "selector": {"app": "web-frontend"},
        "ready_endpoints": [], "not_ready_endpoints": []})

    def test_check_returns_the_contradictions(self):
        answer = ("The typo-svc service has no endpoints. The container "
                  "exited with an application error.")
        oom = ev(OOM_POD, self.ENDPOINTS)

        found = contradiction.check(answer, oom)

        assert found and all(f["rule"].startswith("imposed_termination")
                             for f in found)

    def test_confirmations_returns_the_other_half(self):
        answer = "The typo-svc service has no endpoints."

        confirmed = contradiction.confirmations(answer, ev(self.ENDPOINTS))

        assert confirmed
        assert confirmed[0]["rule"] == "service_endpoints_confirmed_empty"

    def test_neither_half_is_the_other(self):
        answer = ("The typo-svc service has no endpoints. The container "
                  "exited with an application error.")
        both = ev(OOM_POD, self.ENDPOINTS)

        assert (contradiction.check(answer, both)
                != contradiction.confirmations(answer, both))


class TestWhatAFindingSaysAboutWhereItCameFrom:
    def test_a_finding_names_the_tool_call_it_read(self):
        """
        The id is how an operator gets from the finding back to the call. The
        `or {}` guarding it had no test: a finding could report `id: None`
        for every rule and the suite stayed green.
        """
        answer = ("The pod is in CrashLoopBackOff, which means the container "
                  "exited with an application error.")

        found = grounding.check(answer, ev(OOM_POD))["contradictions"]

        assert found
        assert found[0]["evidence"][0]["id"] == "tool-1"

    def test_a_record_with_no_source_does_not_break_the_finding(self):
        """What the `or {}` is for -- a record the caller assembled by hand."""
        answer = ("The pod is in CrashLoopBackOff, which means the container "
                  "exited with an application error.")
        raw = [{"text": json.dumps(OOM_POD[1]), "source": None}]

        found, _ = contradiction.scan(answer, [
            {"id": None, "tool": OOM_POD[0], "result": raw[0]["text"]}])

        assert found
        assert found[0]["evidence"][0]["id"] is None


class TestScanRefusesToWorkWithNothing:
    def test_no_evidence_is_not_an_answer_with_no_contradictions(self):
        """
        `not text or not tool_outputs` returns early. With `and` in its place
        a question with no evidence reaches `tool_outputs[0]` and raises
        IndexError, which is a crash in the middle of an investigation rather
        than a verdict.
        """
        assert contradiction.scan("The pod is not ready.", []) == ([], [])

    def test_no_answer_is_not_an_answer(self):
        assert contradiction.scan("", ev(OOM_POD)) == ([], [])

    def test_raw_tool_outputs_are_accepted_as_well_as_records(self):
        """
        Two evidence shapes, and the branch that tells them apart had no test
        driving the second. A record is a dict carrying `result`; the raw
        shape is a plain list of JSON strings, which is what every caller
        older than grounding.records() passes.
        """
        answer = ("The pod is in CrashLoopBackOff, which means the container "
                  "exited with an application error.")

        found, _ = contradiction.scan(answer, [json.dumps(OOM_POD[1])])

        assert found and found[0]["rule"] == "imposed_termination_vs_application_cause"

    def test_evidence_of_neither_shape_produces_no_findings_rather_than_a_crash(self):
        """
        A dict with no `result` is neither shape. It reaches this module from
        a caller that assembled evidence by hand, and the honest answer is
        that nothing could be checked -- not an exception thrown out of the
        middle of an investigation.
        """
        assert contradiction.scan("The pod is not ready.", [{"tool": "x"}]) == ([], [])


class TestAResourceLimitTheAnswerGotWrong:
    """
    `resource_limit_disagrees` had no test anywhere in the repository -- not
    in this file, not in the eval harness. It is the rule that catches an
    answer quoting a limit the cluster does not have, and its two phrasings,
    its guard against firing with nothing measured, and the regex group it
    reads the value from were all unexercised.

    Both phrasings are here because they are two alternatives in one pattern
    and only the second was reachable through any existing test: reading the
    wrong group returns None for the first, an empty value, and no finding.
    """

    HOG = ("describe_pod", {
        "pod": "memory-hog-x", "namespace": "demo", "status": "CrashLoopBackOff",
        "containers": {"hog": {"limits": {"memory": "64Mi", "cpu": "100m"}}}})

    def test_a_limit_stated_after_the_unit_is_checked(self):
        """`memory limit of 512Mi` -- the first alternative, group 1."""
        answer = "The pod has a memory limit of 512Mi, which it exceeded."

        found = grounding.check(answer, ev(self.HOG))["contradictions"]

        limit = next((f for f in found
                      if f["rule"] == "resource_limit_disagrees"), None)
        assert limit, "the stated limit was not checked against the measured one"
        assert limit["claim"] == "512Mi"
        assert limit["measured"] == "limits.memory = 64Mi"

    def test_a_limit_stated_before_the_unit_is_checked(self):
        """`512Mi memory limit` -- the second alternative, group 2."""
        answer = "The container was killed against its 512Mi memory limit."

        found = grounding.check(answer, ev(self.HOG))["contradictions"]

        assert [f for f in found if f["rule"] == "resource_limit_disagrees"]

    def test_the_measured_limit_is_not_contradicted_by_itself(self):
        answer = "The pod has a memory limit of 64Mi and was OOMKilled against it."

        found = grounding.check(answer, ev(self.HOG))["contradictions"]

        assert [f for f in found if f["rule"] == "resource_limit_disagrees"] == []

    def test_a_limit_nothing_measured_is_not_contradicted(self):
        """
        The guard. With no limit in the evidence there is nothing to disagree
        with, and a rule that fires here would be inventing the measurement it
        claims to have made.
        """
        no_limits = ("describe_pod", {
            "pod": "crasher-x", "namespace": "demo", "status": "Running",
            "containers": {"crasher": {}}})
        answer = "The pod has a memory limit of 512Mi."

        found = grounding.check(answer, ev(no_limits))["contradictions"]

        assert [f for f in found if f["rule"] == "resource_limit_disagrees"] == []

    def test_a_bare_number_beside_the_unit_is_not_a_stated_limit(self):
        """
        Six false positives, zero true ones, from the `stress` fixture's log
        line. The number has to be presented as a limit or a request.
        """
        answer = ("The logs read `dispatching hogs: 0 cpu, 0 io, 1 vm, 0 hdd`, "
                  "which is the workload starting.")

        found = grounding.check(answer, ev(self.HOG))["contradictions"]

        assert [f for f in found if f["rule"] == "resource_limit_disagrees"] == []


class TestWhichNamesTheAbsenceRuleWillConsider:
    def test_a_bare_word_is_not_a_generated_object_name(self):
        """
        "The pod demo does not exist" -- `demo` is the next English word, not
        an object. Requiring a digit or a hyphen is what keeps the rule off
        it, and dropping that requirement fires the rule on prose.
        """
        pod = ("list_pods", {"demo": {"status": "Running", "ready": "1/1"}})
        answer = "The pod demo does not exist in this cluster."

        found = grounding.check(answer, ev(pod))["contradictions"]

        assert [f for f in found
                if f["rule"] == "claimed_absent_but_measured_present"] == []

    def test_a_generated_name_is_considered(self):
        """The behaviour the filter must not cost."""
        pod = ("list_pods", {"demo-1": {"status": "Running", "ready": "1/1"}})
        answer = "The pod demo-1 does not exist in this cluster."

        found = grounding.check(answer, ev(pod))["contradictions"]

        assert [f for f in found
                if f["rule"] == "claimed_absent_but_measured_present"]
