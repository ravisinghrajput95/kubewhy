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



class TestTheNegationWindowHasTwoBounds:
    """
    _NEGATION_WINDOW is 78, and both ends of that are measured rather than
    chosen. Every clause below is real, taken from `results/` in 2026-09.

    The lower bound: at 40 this rule fired on the exact sentence the system
    prompt asks for. The prompt tells the model that exit 137 names the signal
    and not the sender, and that a `last_termination.reason` of Error means
    something other than the OOM killer did it. Six recorded clauses say so,
    and all six were scored as *asserting* the OOM kill they were denying --
    the checker penalising the behaviour the prompt teaches.

    The upper bound is why this is not "look anywhere in the clause". One
    recorded clause carries a negator governing a different part of the
    sentence, and a whole-clause window silences a genuine contradiction.
    """

    # (distance to the governing negator, clause, the phrase under test)
    DENIALS = [
        (42, "this means the kubelet did **not** attribute the crash to the "
             "kernel's oom killer.", "oom kill"),
        (43, "thus, while we see evidence of it crashing because of resource "
             "limits, we can't definitively confirm the presence of a bug in "
             "the application code.", "bug in the application"),
        (46, "- the container has **no memory or cpu limits** defined, ruling "
             "out oomkilled or resource exhaustion as the cause.", "oomkilled"),
        (48, 'however, the `last_termination.reason` field shows `"error"`, '
             'not `"oomkilled"`, meaning the kubelet did not explicitly '
             'attribute the termination to the oom killer.', "oom kill"),
        (63, "- the kubelet did **not** report the termination as being "
             "caused by the **kernel's oom killer**.", "oom kill"),
        (69, 'however, the kubelet\'s `last_termination.reason` field shows '
             '**"error"**, not "oomkilled" (which the kubelet explicitly sets '
             'when the kernel\'s oom killer terminates a container).',
             "oom kill"),
    ]

    # The negator here is about the *node*; the clause still claims the OOM
    # killer ran, against a measured reason of Error.
    GOVERNING_SOMETHING_ELSE = (
        87,
        "the node is not under memory pressure, but the container's lack of "
        "limits allows it to trigger the oom killer independently.",
        "oom kill",
    )

    def _distance(self, clause, phrase):
        """Characters from the nearest preceding negator to the phrase."""
        start = clause.find(phrase)
        assert start >= 0, f"{phrase!r} is not in the clause"
        hits = list(contradiction._NEGATORS.finditer(clause[:start]))
        assert hits, "this clause has no negator, so it tests nothing"
        return start - hits[-1].start()

    @pytest.mark.parametrize("distance,clause,phrase", DENIALS)
    def test_a_denial_is_not_read_as_a_claim(self, distance, clause, phrase):
        # The distance is asserted too. Without it a reworded clause could
        # drift inside 40 and the case would pass while testing nothing.
        assert self._distance(clause, phrase) == distance
        assert not contradiction._asserted(clause, phrase)

    def test_a_negator_governing_another_clause_does_not_deny(self):
        distance, clause, phrase = self.GOVERNING_SOMETHING_ELSE
        assert self._distance(clause, phrase) == distance
        assert contradiction._asserted(clause, phrase)

    def test_the_window_sits_inside_the_band_those_two_bounds_define(self):
        """
        The bounds, as a range rather than as a number. A future retune that
        keeps the tests above passing individually can still land on an edge;
        this says where the room is.
        """
        furthest_denial = max(d for d, _, _ in self.DENIALS)
        nearest_false_negator = self.GOVERNING_SOMETHING_ELSE[0]

        assert furthest_denial <= contradiction._NEGATION_WINDOW
        assert nearest_false_negator > contradiction._NEGATION_WINDOW

    def test_an_ordinary_assertion_is_still_an_assertion(self):
        # The counter. Every test above passes on an _asserted() that returns
        # False for everything.
        assert contradiction._asserted(
            "the container was oomkilled after exceeding its memory limit",
            "oomkilled")

class TestDenialsTheBackwardWindowCannotSee:
    """
    Defect 52. Every clause below is recorded: the first eleven from `results/`,
    classified by hand among the 66 distinct clauses this rule flagged over the
    corpus on 2026-09-15; the last three from the defect 46 A/B, whose records
    keep the flagged clause but not the draft.

    None of them claims an OOM kill. They deny one with the negator *after* the
    phrase, deny it with a noun directly before it, concede it and set it
    aside, restate the rule the system prompt teaches, or ask a question.
    Replayed, fixing them moved 11 verdicts out of `contradicted`, none in, and
    removed no finding from any other clause.
    """

    KILLED = ("describe_pod", {
        "pod": "slow-starter-1", "namespace": "demo",
        "status": "CrashLoopBackOff",
        "containers": {"web": {"ready": False, "restarts": 5, "limits": {},
                               "last_termination": {"reason": "Error",
                                                    "exit_code": 137}}}})

    NOT_CLAIMS = [
        # negator after the phrase
        "The OOM killer is **not** responsible here.",
        "The **OOM killer** (out-of-memory killer) is **not confirmed** as the "
        "cause, since the kubelet only sets `OOMKilled` as the reason when the "
        "kernel\u2019s OOM killer terminates a container.",
        "The OOM killer is not the cause, as the termination reason is not "
        '`"OOMKilled"`.',
        # a negating noun directly before it
        "However, the absence of OOMKilled in the logs suggests this is not the "
        "direct cause.",
        "Error` instead of `OOMKilled`.",
        "The `Error` termination reason and lack of OOMKilled confirmation rule "
        "out memory pressure as the direct cause.",
        # a concession, set aside
        "While exit code 137 strongly suggests a SIGKILL (commonly from the OOM "
        "killer), the kubelet\u2019s logs do not explicitly confirm this.",
        "While exit code 137 is often associated with OOM killers, the "
        '`"Error"` reason suggests the termination was not explicitly caused by '
        "the OOM killer.",
        # the rule, restated
        "The kubelet only sets `OOMKilled` if the kernel's OOM killer terminated "
        "the container.",
        "The kubelet explicitly sets `OOMKilled` as the reason **only when the "
        "kernel\u2019s OOM killer terminates a container**.",
        # a question
        "### **Why the Confusion About OOMKilled?**",
        # from the defect 46 A/B
        "This means the **OOM killer was not the cause** (the kubelet explicitly "
        "sets `OOMKilled` when the OOM killer terminates a container).",
        '"OOMKilled"` only when the kernel\'s OOM killer terminates a container.',
        "The kubelet sets `OOMKilled` explicitly for OOM kills.",
        # defect 52's remainder, 2026-09-15: a contrast with the measured
        # reason, an assumption being overturned, and a thing to look for
        "The **slow-starter deployment** in the `demo` namespace is restarting "
        "due to a **container exit code 137**, which typically indicates "
        "**OOMKilled** (Out-Of-Memory), but the `last_termination.reason` field "
        'explicitly states **"Error"**.',
        "- This contradicts the earlier assumption of OOM termination.",
        "- Inspect `demo/nightly-sync` and `demo/memory-hog` for OOMKilled or "
        "exit code 1.",
    ]

    CLAIMS = [
        # The counter: without these, every test above passes on a rule that
        # has stopped firing at all.
        "The container was killed by the OOM killer.",
        # A conjunction between the phrase and a later "was not" -- the
        # after-negator must not reach across it. Exactly one word sits
        # between, because the pattern admits one: the first version of this
        # counter read "OOM-killed because it is not limited", two words, and
        # kept passing with the conjunction guard deleted.
        "The container was OOM-killed and was not restarted.",
        # A comma ends the subject the after-negator is allowed to govern.
        "The OOM killer terminated the container, which is not unusual.",
        # Defect 45's true positive: "lack" 40 characters back is about limits.
        "The node is not under memory pressure, but the container's lack of "
        "limits allows it to trigger the OOM killer independently.",
        # A concession whose phrase comes after the comma is still a claim.
        "While the node had free memory, the container was OOMKilled.",
        # "sets" without the OOM killer as the condition is not the rule.
        "The kubelet sets the reason, and this container was OOMKilled.",
        # A "but" that introduces something other than the reason field.
        "The container was OOMKilled, but the logs show no error at all.",
        # "check" earlier in the clause, not directly before the phrase. One
        # clause: the first version read "Check the logs: the container was
        # OOMKilled.", which the splitter cuts at the colon, so "check" never
        # shared a clause with the phrase and the anchor went untested.
        "Check memory limits since the container was OOMKilled.",
    ]

    def test_a_clause_about_one_of_two_pods_is_not_checked_against_either(self):
        """
        Defect 52's remainder. A cluster-scan answer named `demo/memory-hog`,
        then a bare "OOMKilled." under it, and the fragment scoped to a
        different pod's describe_pod -- whose reason was Error. With two pods'
        details in scope the rule cannot know whose reason the clause is about.
        """
        other = ("describe_pod", {
            "pod": "nightly-sync-1", "namespace": "demo", "status": "Error",
            "containers": {"sync": {"last_termination": {"reason": "Error",
                                                         "exit_code": 1}}}})
        found = grounding.check("The container was OOM-killed.",
                                ev(self.KILLED, other))["contradictions"]
        assert found == []

    def test_a_workload_and_its_pod_are_one_subject_not_two(self):
        # A tool called with the workload name reports that name; describe_pod
        # reports the pod's. Counting them as two pods silenced the re-ask.
        logs = ("get_pod_logs", {"pod": "slow-starter", "logs": []})
        found = grounding.check("The container was OOM-killed.",
                                ev(self.KILLED, logs))["contradictions"]
        assert found and found[0]["rule"] == "termination_reason_vs_memory_cause"

    def test_the_same_clause_about_one_pod_is_still_checked(self):
        # The counter: without it the test above passes on a rule that has
        # stopped firing for multi-document evidence of any kind.
        events = ("get_pod_events", {"pod": "slow-starter-1", "events": []})
        found = grounding.check("The container was OOM-killed.",
                                ev(self.KILLED, events))["contradictions"]
        assert found and found[0]["rule"] == "termination_reason_vs_memory_cause"

    @pytest.mark.parametrize("clause", NOT_CLAIMS)
    def test_a_recorded_non_claim_is_not_a_contradiction(self, clause):
        assert grounding.check(clause, ev(self.KILLED))["contradictions"] == []

    @pytest.mark.parametrize("clause", CLAIMS)
    def test_a_claim_beside_those_shapes_is_still_caught(self, clause):
        found = grounding.check(clause, ev(self.KILLED))["contradictions"]
        assert found and found[0]["rule"] == "termination_reason_vs_memory_cause"


class TestFalsePositivesTheRecheckRead(TestDenialsTheBackwardWindowCannotSee):
    """
    Defect 56. Four more shapes, every clause recorded, found by reading the
    answers of the 2026-09-16/17 sets rather than by counting them. Replayed
    over 2089 records: 6 findings removed, 0 added, 5 verdicts out of
    `contradicted` and none into it.

    Inherits the defect 52 CLAIMS, so a fix here that stops the rule firing at
    all fails those too.
    """

    RUNNING = ("scan_cluster", {"demo/slow-starter": {
        "status": "Running", "ready": "1/1", "pods": 1,
        "example": "slow-starter-1"}})

    NOT_CLAIMS = [
        # A counterfactual: the phrase is what would have been seen.
        "- **SIGKILL** from the kernel (if memory is exhausted, but "
        "`OOMKilled` would be the `reason`).",
        # A denial that says so with an adjective rather than "not".
        "- The **OOMKilled** claim was incorrect:",
        # The rule again, with the acronym spelled out and markup between the
        # conditional and its subject.
        "The kubelet sets the `last_termination.reason` field to "
        '**"OOMKilled"** *only if* the kernel\'s out-of-memory (OOM) killer '
        "terminated the container.",
        "The kubelet sets `OOMKilled` as the reason **only if** the "
        "kernel\u2019s out-of-memory killer terminated the container.",
    ]

    ABOUT_THE_PROBE = [
        "The liveness probe is failing repeatedly, causing the container to "
        "restart.",
        'The pod is not "crashing" in the traditional sense\u2014it is '
        "**Running** and **Ready**, but the probe is failing to detect it as "
        "healthy, leading to termination.",
        "The startup check is failing, so the kubelet restarts it.",
    ]

    ABOUT_THE_POD = [
        # The counter: the same rule, on the pod itself, still fires.
        "The pod is failing and has not come back.",
        "The deployment is failing to stay up.",
    ]

    @pytest.mark.parametrize("clause", NOT_CLAIMS)
    def test_a_recorded_non_claim_is_not_a_contradiction(self, clause):
        assert grounding.check(clause, ev(self.KILLED))["contradictions"] == []

    @pytest.mark.parametrize("clause", ABOUT_THE_PROBE)
    def test_a_failing_probe_is_not_a_claim_that_the_pod_is_not_running(self, clause):
        """
        A Running, Ready pod whose liveness probe fails is exactly what this
        fixture is: the probe failing is why it restarts. Both recorded
        clauses came from `scoping_quiet_workload_beside_loud_one`, and the
        scan row they were measured against was a snapshot taken between
        restarts -- which the prefetch makes the first evidence in every run.
        """
        assert grounding.check(clause, ev(self.RUNNING))["contradictions"] == []

    @pytest.mark.parametrize("clause", ABOUT_THE_POD)
    def test_the_same_claim_about_the_pod_is_still_caught(self, clause):
        found = grounding.check(clause, ev(self.RUNNING))["contradictions"]
        assert found and found[0]["rule"] == "running_vs_claimed_failing"

    def test_a_readiness_probe_failure_still_contradicts_ready(self):
        """
        `_NOT_READY` carries "readiness probe is failing" on purpose: a
        failing readiness probe does contradict ready = true, and the guard
        above must not reach it.
        """
        found = grounding.check("The readiness probe is failing.",
                                ev(self.RUNNING))["contradictions"]
        assert found and found[0]["rule"] == "ready_vs_claimed_not_ready"


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


class TestADeterminerDoesNotDefeatTheAbsenceGuard:
    """
    Defect 58. `_DENIED_BEFORE` has recognised "the absence of X" since defect
    52, and one article walked straight past it: the guard required the
    negating noun to be adjacent, and "the absence of **an** `OOMKilled`
    reason" is not adjacent. That clause was scored `contradicted` on
    2026-09-18 while agreeing with the measurement it was checked against.
    """

    CLAUSE = ("However, the absence of an `OOMKilled` reason suggests the "
              "kill was not triggered by the kernel's OOM killer.")

    def test_the_denial_is_not_read_as_a_claim(self):
        assert not contradiction.asserted(self.CLAUSE, "oomkilled")

    @pytest.mark.parametrize("determiner", ["a", "an", "the", "any", "some"])
    def test_any_determiner(self, determiner):
        clause = f"there was an absence of {determiner} OOMKilled reason"
        assert not contradiction.asserted(clause, "oomkilled")

    def test_the_true_positive_the_adjacency_rule_exists_for_still_asserts(self):
        """
        The counter. `_DENIED_BEFORE`'s own comment says adjacency is what
        keeps this asserted, and defect 45 recorded it as a real contradiction.
        A determiner slot must not widen into a general "a negator appears
        somewhere before" rule, which would silence it.
        """
        clause = ("The node is not under memory pressure, but the container's "
                  "lack of limits allows it to trigger the OOM killer "
                  "independently.")
        assert contradiction.asserted(clause, "oom killer")


class TestDeniedIsNarrowerThanNotAsserted:
    """
    Defect 59's second attempt, and the reason there are two functions.

    `_asserted` carries a 78-character backward window tuned to one rule.
    Handed a general status token it reads the wrong "not": "is **not**
    starting due to a CreateContainerConfigError" asserts the status. Reusing
    it in grounding.check() moved 7 corpus records from `grounded` to
    `insufficient_evidence` before the replay caught it.
    """

    ASSERTS_THE_STATUS = ('The pod "missing-configmap-key" in the '
                          '"config-faults" namespace is not starting due to a '
                          'CreateContainerConfigError.')

    def test_the_wide_window_gets_this_wrong(self):
        """Not a wish -- a record of why `denied` exists. If this ever starts
        passing, `denied` can collapse back into `not asserted`."""
        assert not contradiction.asserted(
            self.ASSERTS_THE_STATUS, "createcontainerconfigerror")

    def test_denied_gets_it_right(self):
        assert not contradiction.denied(
            self.ASSERTS_THE_STATUS, "createcontainerconfigerror")

    @pytest.mark.parametrize("clause", [
        "The reason was not `OOMKilled`.",
        "Look for `OOMKilled` in the termination reason or node pressure metrics.",
        "not the OOM killer, as `last_termination.reason` is `Error` "
        "instead of `OOMKilled`",
        "However, the absence of an `OOMKilled` reason suggests otherwise.",
        "This is not a resource exhaustion issue (no OOMKilled or memory "
        "limits reported).",
    ])
    def test_the_plain_denials(self, clause):
        assert contradiction.denied(clause, "oomkilled")

    @pytest.mark.parametrize("clause", [
        "The container was `OOMKilled` after exceeding its memory limit.",
        "The pod was OOMKilled by the kernel.",
    ])
    def test_the_counter_an_assertion_is_not_denied(self, clause):
        """Without this, a `denied` that always returned True would pass every
        test above and blind the unverified path completely."""
        assert not contradiction.denied(clause, "oomkilled")

    def test_a_phrase_that_is_absent_is_not_denied(self):
        assert not contradiction.denied("nothing relevant here", "oomkilled")


class TestDeniedAlsoCoversTheRuleStatement:
    """
    Defect 59 was ported narrower than the problem. It carried the positional
    denials to `denied()` and left behind defect 56's rule-statement guard and
    defect 52's reason-contrast, so `stuck_terminating_finalizer` went on
    failing 2 of 3 runs on `oomkilled` -- twice on the sentence
    SYSTEM_PROMPT teaches. Found 2026-09-21 by reading its failures.
    """

    @pytest.mark.parametrize("clause", [
        "the kubelet only sets `OOMKilled` when the kernel's OOM killer "
        "terminates a container",
        'The kubelet sets the reason to `"OOMKilled"` **only if** the '
        "kernel's OOM killer terminated the container.",
        "note that `last_termination.reason = Error` does not confirm OOMKilled.",
    ])
    def test_the_three_shapes_its_own_failures_carried(self, clause):
        assert contradiction.denied(clause, "oomkilled")

    def test_one_word_may_sit_between_the_negator_and_the_phrase(self):
        """"does not confirm X" and "did not report X" -- one verb, no more."""
        assert contradiction.denied("the kubelet did not report OOMKilled", "oomkilled")

    def test_the_counter_four_words_may_not(self):
        """
        The regression defect 59 was written against. Widening the adjacency
        without a bound puts "is not starting due to a
        CreateContainerConfigError" back to reading as a denial of the status,
        which moved 7 corpus records from grounded to insufficient_evidence.
        """
        clause = ('The pod "missing-configmap-key" is not starting due to a '
                  "CreateContainerConfigError.")
        assert not contradiction.denied(clause, "createcontainerconfigerror")

    def test_the_counter_an_assertion_is_still_not_denied(self):
        assert not contradiction.denied(
            "The container was `OOMKilled` after exceeding its memory limit.",
            "oomkilled")


class TestAConditionalWithAModalClaimsNothing:
    """
    Defect 67, found 2026-09-21 by reading the interleaved run's one
    `contradicted` verdict. The clause reasons about what WOULD have been seen:

        "If the container's memory usage exceeded these defaults, the OOM
         killer would trigger, but the kubelet would log the reason as
         **OOMKilled**."

    which is the model getting it right -- the reason is Error, so this did not
    happen. `_COUNTERFACTUAL` covers the same idea only *after* the phrase and
    only for five verbs, and "would log" before it fell through.

    Structural rather than a verb list, because a verb list is what defect 45
    already learned not to build here.
    """

    CLAUSE = ("If the container's memory usage exceeded these defaults, the "
              "OOM killer would trigger, but the kubelet would log the reason "
              "as **OOMKilled**.")

    @pytest.mark.parametrize("phrase", ["oomkilled", "oom kill", "oom killer"])
    def test_every_phrase_the_rule_tries_is_guarded(self, phrase):
        """
        The rule takes the first phrase that asserts, so guarding one and not
        its siblings changes which phrase is reported and nothing else. The
        first fix here guarded `oomkilled` and left `oom kill` firing.
        """
        assert not contradiction.asserted(self.CLAUSE, phrase)

    def test_the_modal_may_fall_either_side_of_the_phrase(self):
        """"the OOM killer **would** trigger" puts it after; "**would** log
        the reason as OOMKilled" puts it before. One sentence, both sides."""
        assert not contradiction.asserted(self.CLAUSE, "oom killer")
        assert not contradiction.asserted(self.CLAUSE, "oomkilled")

    def test_the_counter_a_conditional_without_a_modal_still_asserts(self):
        """Both halves are required. Otherwise every sentence opening with
        "If" stops being checkable."""
        assert contradiction.asserted(
            "If you look at the logs, the container was OOMKilled.", "oomkilled")

    def test_the_counter_a_plain_assertion_still_asserts(self):
        assert contradiction.asserted(
            "The container was OOMKilled after exceeding its memory limit.",
            "oomkilled")
