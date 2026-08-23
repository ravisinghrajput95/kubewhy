"""
Tests for the claim checker.

The cases below are drawn from answers this agent actually produced, so the
checker is pinned against real behaviour rather than invented examples.
"""

import json

import pytest

import grounding


class TestRealHallucination:
    """The case this module exists for."""

    def test_flags_uptime_never_measured(self):
        # qwen3 reported "18 days" for a host up four hours, having called
        # only the memory tool.
        tools = [json.dumps({"13991": {"name": "llama-server", "memory_percent": 19.66}})]
        answer = "llama-server uses 19.66% of memory. The host has been up for 18 days."

        result = grounding.check(answer, tools)

        assert result["confidence"] == "partial"
        assert "18" in result["unverified"]

    def test_same_answer_passes_once_uptime_is_measured(self):
        tools = [
            json.dumps({"13991": {"memory_percent": 19.66}}),
            json.dumps({"Uptime": "4:36:25"}),
        ]
        answer = "llama-server uses 19.66% of memory. The host has been up 4:36:25."

        assert grounding.check(answer, tools)["confidence"] == "grounded"


class TestRecommendationsAreNotClaims:
    """
    Observed in the browser UI against a live cluster: a correct OOM diagnosis
    came back `partial`, flagging 128 and 256 -- both of which appeared only in
    the suggested fix. The exemption for recommendations existed already, but
    the clause splitter breaks on ":", so `limits.memory: 256Mi` tore the
    numbers away from the verb proposing them.
    """

    def test_kubernetes_field_syntax_in_a_fix_is_exempt(self):
        tools = [
            json.dumps(
                {
                    "limits": {"memory": "64Mi"},
                    "requests": {"memory": "32Mi"},
                    "last_termination": {"reason": "OOMKilled", "exit_code": 137},
                }
            )
        ]
        answer = (
            "The memory-hog pod is OOMKilled for exceeding its memory limit of "
            "64Mi, and its request is 32Mi.\n"
            "Fix: Increase the limits (e.g., limits.memory: 256Mi and "
            "requests.memory: 128Mi) to match the workload."
        )

        result = grounding.check(answer, tools)

        assert result["unverified"] == []
        assert result["confidence"] == "grounded"

    def test_a_proposed_yaml_block_is_not_a_claim(self):
        """
        Models answer with corrected YAML, and its lines carry no verb of their
        own -- so the values being proposed were checked as measurements and
        flagged. Seen in 2 of 3 eval runs of the OOM case.
        """
        tools = [json.dumps({"limits": {"memory": "64Mi"}})]
        answer = (
            "The pod exceeds its memory limit of 64Mi.\n"
            "Fix: raise the limit.\n"
            "```yaml\n"
            "resources:\n"
            "  limits:\n"
            "    memory: 256Mi\n"
            "```"
        )

        assert grounding.check(answer, tools)["unverified"] == []

    def test_a_quoted_block_of_evidence_is_still_checked(self):
        """The exemption follows intent, not fences: a block introduced by
        ordinary prose is evidence, and a fabricated figure in it must flag."""
        tools = [json.dumps({"limits": {"memory": "64Mi"}})]
        answer = "The container logged this before dying:\n```\nOOM killed after 9999 seconds\n```"

        assert "9999" in grounding.check(answer, tools)["unverified"]

    def test_a_measurement_before_the_fix_is_still_checked(self):
        """The exemption starts at the recommendation, not at the line."""
        tools = [json.dumps({"limits": {"memory": "64Mi"}})]
        answer = "The pod has restarted 47 times. Raise the limit to 128Mi."

        result = grounding.check(answer, tools)

        # 47 was never measured and must still be caught; 128 is a proposal.
        assert result["unverified"] == ["47"]


SCAN = [
    json.dumps(
        {
            "staging/payments-api": {"status": "ErrImagePull", "pods": 3},
            "demo/bad-image": {"status": "ImagePullBackOff", "pods": 1},
        }
    )
]


class TestClaimsAreScopedToTheirEntity:
    """
    Observed on a live cluster: scan_cluster reported ErrImagePull for
    staging/payments-api and ImagePullBackOff for demo/bad-image, and the model
    then attributed ErrImagePull to demo/bad-image. Checking every claim
    against every measurement at once let that pass as grounded, and the wider
    the tool result the more it covered.
    """

    def test_a_status_belonging_to_another_workload_does_not_support_this_one(self):
        result = grounding.check("demo/bad-image is in ErrImagePull.", SCAN)

        assert result["confidence"] == "partial"
        assert "errimagepull" in result["unverified"]

    def test_the_status_that_workload_really_had_still_passes(self):
        """Scoping must not make every status look fabricated."""
        assert (
            grounding.check("demo/bad-image is ImagePullBackOff.", SCAN)["confidence"]
            == "grounded"
        )

    def test_each_workload_is_checked_against_its_own_measurement(self):
        answer = (
            "staging/payments-api is in ErrImagePull.\n"
            "demo/bad-image is ImagePullBackOff."
        )

        assert grounding.check(answer, SCAN)["confidence"] == "grounded"

    def test_a_figure_from_another_workload_does_not_support_this_one(self):
        # payments-api has 3 pods; bad-image has 1.
        result = grounding.check("demo/bad-image has 3 pods affected.", SCAN)

        assert "3" in result["unverified"]

    def test_a_status_no_workload_had_is_still_caught(self):
        result = grounding.check("demo/bad-image is Evicted.", SCAN)

        assert result["confidence"] == "partial"
        assert "evicted" in result["unverified"]

    def test_a_clause_naming_nothing_falls_back_to_every_measurement(self):
        """
        A summary sentence names no entity. Scoping it to nothing would flag
        every such sentence, which is the fastest way to make this ignorable.
        """
        assert (
            grounding.check("One workload is in ImagePullBackOff.", SCAN)["confidence"]
            == "grounded"
        )

    def test_a_workload_name_reaches_its_pods_measurements(self):
        """
        Answers say "bad-image"; describe_pod files its result under
        "bad-image-647c5576d5-pxmvr". Without an alias for the trimmed
        workload name, scoping invents failures instead of catching them.
        """
        tools = [
            json.dumps(
                {
                    "demo/bad-image": {
                        "status": "ImagePullBackOff",
                        "pods": 1,
                        "example": "bad-image-647c5576d5-pxmvr",
                    }
                }
            ),
            json.dumps(
                {
                    "pod": "bad-image-647c5576d5-pxmvr",
                    "containers": {"app": {"restarts": 7}},
                }
            ),
        ]

        assert grounding.check("bad-image has restarted 7 times.", tools)["unverified"] == []

    def test_scoping_works_for_a_single_subject_document(self):
        """describe_pod names its subject rather than mapping many."""
        tools = [
            json.dumps({"pod": "memory-hog", "status": "OOMKilled", "restarts": 4}),
            json.dumps({"pod": "crasher", "status": "CrashLoopBackOff", "restarts": 9}),
        ]

        assert grounding.check("memory-hog is OOMKilled.", tools)["confidence"] == "grounded"
        # crasher's restart count is not memory-hog's.
        assert "9" in grounding.check("memory-hog restarted 9 times.", tools)["unverified"]


class TestFalsePositives:
    """A noisy checker gets ignored, so these matter as much as detection."""

    def test_markdown_ordinals_are_not_claims(self):
        tools = [json.dumps({"a": 19.66, "b": 19.54})]
        answer = "1. first uses 19.66%\n2. second uses 19.54%"

        assert grounding.check(answer, tools)["unverified"] == []

    def test_rounding_is_allowed(self):
        # "about 20%" off a measured 19.66 is summarising, not inventing.
        tools = [json.dumps({"memory_percent": 19.66})]
        assert grounding.check("about 20% of memory", tools)["confidence"] == "grounded"

    def test_proposed_values_are_not_treated_as_claims(self):
        """
        Real case: the model diagnosed a 64Mi limit correctly and suggested
        raising it to 128Mi. Flagging the 128 would train the reader to
        ignore this signal entirely.
        """
        tools = [json.dumps({"limits": {"memory": "64Mi"}, "reason": "OOMKilled"})]
        answer = (
            "The pod was OOMKilled; its memory limit of 64Mi is too low. "
            "Fix: increase the memory limit to 128Mi."
        )

        assert grounding.check(answer, tools)["unverified"] == []

    def test_a_fabrication_beside_a_suggestion_still_flags(self):
        tools = [json.dumps({"limits": {"memory": "64Mi"}})]
        answer = (
            "The pod has restarted 47 times. Consider raising the limit to 128Mi."
        )

        assert grounding.check(answer, tools)["unverified"] == ["47"]

    def test_an_answer_with_nothing_falsifiable_is_not_grounded(self):
        """
        Corrected 2026-08-19. "Everything looks fine." asserts nothing this
        module can trace, and calling that `grounded` puts the same badge on
        an unfalsifiable sentence as on a quoted kubelet message. It is not a
        failure either -- nothing was contradicted -- so it gets its own state.
        """
        tools = [json.dumps({"status": "Running"})]
        result = grounding.check("Everything looks fine.", tools)

        assert result["confidence"] == grounding.INSUFFICIENT
        assert result["checked"] == 0
        assert result["unverified"] == []

    def test_ordinary_english_status_words_are_not_claims(self):
        """
        Found in production output: a correct diagnosis saying "ensure the
        database service is running" was flagged, because "running" is both an
        English word and a Kubernetes status.
        """
        tools = [json.dumps({"logs": "connection refused"})]
        answer = "Ensure the database service is running and reachable."

        assert grounding.check(answer, tools)["unverified"] == []

    def test_values_inside_strings_count_as_measured(self):
        # Ports and exit codes appear inside log text, not as JSON numbers.
        tools = [json.dumps({"logs": "could not connect to db:5432: refused"})]
        assert grounding.check("it cannot reach port 5432", tools)["unverified"] == []


class TestStatusClaims:
    def test_flags_status_no_tool_reported(self):
        tools = [json.dumps({"pod": "p", "status": "Running"})]
        result = grounding.check("The pod is OOMKilled.", tools)

        assert "oomkilled" in result["unverified"]

    def test_accepts_status_a_tool_reported(self):
        tools = [json.dumps({"pod": "p", "status": "OOMKilled"})]
        assert grounding.check("The pod was **OOMKilled**.", tools)["unverified"] == []

    def test_matches_regardless_of_markdown_styling(self):
        tools = [json.dumps({"status": "CrashLoopBackOff"})]
        assert grounding.check("`CrashLoopBackOff`", tools)["unverified"] == []


class TestEnumeratedHeadingsAreNotMeasurements:
    """
    "### **11. demo/crasher (1 pod)**" is the eleventh item in a list, not a
    measurement of eleven of anything.

    Latent until the coverage policy made cluster summaries complete: before
    that a run listed six or eight workloads and its ordinals stayed in the
    range of numbers nobody looks twice at. A complete summary of twenty
    workloads writes ordinals up to twenty, and one live run was flagged for
    11, 12, 13, 14, 17, 18, 19, 20 and 21 out of a single answer -- burying
    the one figure in it that was worth reading.
    """

    TOOLS = [json.dumps({"demo/crasher": {"status": "Error", "pods": 1}})]

    def test_an_enumerated_heading_is_not_a_claim(self):
        answer = ("### **11. demo/crasher (1 pod)**\n"
                  "The container exits with an error.")

        assert grounding.check(answer, self.TOOLS)["unverified"] == []

    @pytest.mark.parametrize("line", [
        "11. demo/crasher",
        "- 11. demo/crasher",
        "  * 11) demo/crasher",
        "### 11. demo/crasher",
        "#### **11.** demo/crasher",
        "> 11. demo/crasher",
    ])
    def test_every_marker_the_model_uses_is_stripped(self, line):
        assert grounding._ORDINAL.sub(" ", line).lstrip()[0] not in "0123456789"

    def test_a_number_in_prose_is_still_a_claim(self):
        """The exemption is the position, not the digit. Only a number that
        opens a line after list punctuation is enumeration."""
        answer = "pod crasher-1 restarted 11 times."

        assert "11" in grounding.check(answer, self.TOOLS)["unverified"]


class TestEveryVerdictIsNamed:
    """
    A caller that retypes check()'s return values goes stale when a fourth is
    added, and one did: evals/run_controller_eval.py listed grounded, partial
    and ungrounded, and failed any finding carrying insufficient_evidence as
    "no usable confidence". Fresh controller validation on 2026-08-21 scored
    correct diagnoses of crasher and bad-image as failures on that line alone.
    """

    def test_every_verdict_check_can_return_is_in_verdicts(self):
        tools = [json.dumps({"pod": "p-1", "status": "OOMKilled"})]
        produced = {
            # grounded: a claim that traces
            grounding.check("pod p-1 was OOMKilled.", tools)["confidence"],
            # partial: a claim that does not
            grounding.check("pod p-1 was OOMKilled after 512 restarts.",
                            tools)["confidence"],
            # ungrounded: claims with no tool call at all
            grounding.check("CPU is at 42%.", [])["confidence"],
            # insufficient: nothing checkable
            grounding.check("The pod looks fine.", tools)["confidence"],
        }
        assert produced <= grounding.VERDICTS
        assert len(produced) == 4, f"expected all four, got {produced}"

    def test_insufficient_evidence_is_a_member(self):
        """The one that was missing, pinned by name."""
        assert grounding.INSUFFICIENT in grounding.VERDICTS


class TestExampleClausesAreNotClaims:
    """
    "check the logs (e.g., OOMKilled)" names a status as an example of what to
    look for. It is not an assertion that anything was OOMKilled, and the
    prescriptive exemption has always meant to cover it.

    It did not. Inside the alternation the abbreviation carried the group's
    trailing `\b`, which cannot match before the comma or space that always
    follows "e.g.", so the branch was unreachable from the day it was written.
    """

    SCAN = json.dumps({"demo/crasher": {"status": "CrashLoopBackOff",
                                        "pods": 1}})

    def test_the_abbreviation_is_recognised_at_all(self):
        assert grounding._PRESCRIPTIVE.search("(e.g., OOMKilled)")
        assert grounding._PRESCRIPTIVE.search("(e.g. OOMKilled)")

    def test_a_status_named_as_an_example_is_not_a_claim(self):
        answer = ("demo/crasher is in CrashLoopBackOff. Use describe_pod to "
                  "identify crash reasons (e.g., OOMKilled, startup failures).")
        result = grounding.check(answer, [self.SCAN])

        assert result["unverified"] == []
        assert result["confidence"] == "grounded"

    def test_the_same_status_asserted_is_still_a_claim(self):
        """The exemption is the word, not the status: drop the "e.g." and the
        sentence becomes an assertion again."""
        answer = ("demo/crasher is in CrashLoopBackOff. The container was "
                  "OOMKilled.")
        result = grounding.check(answer, [self.SCAN])

        assert "oomkilled" in result["unverified"]

    def test_the_ordinary_verbs_still_fire(self):
        for phrase in ("Increase the limit", "consider raising it",
                       "recommend 256Mi", "the value should be higher"):
            assert grounding._PRESCRIPTIVE.search(phrase), phrase


class TestAToolsOwnSpellingCounts:
    """
    scan_cluster labels a Running-but-unready workload `fault: not-ready`.
    The model reports it as `NotReady`, which is how KNOWN_STATUSES spells it,
    and "notready" is not a substring of "not-ready" -- so the checker flagged
    a status its own tool had reported in the same run.
    """

    SCAN = json.dumps({"demo/never-ready": {"status": "Running",
                                            "fault": "not-ready", "pods": 1}})

    def test_the_hyphenated_label_grounds_the_one_word_claim(self):
        answer = "deployment never-ready is Running but NotReady."
        result = grounding.check(answer, [self.SCAN])

        assert result["unverified"] == []
        assert result["confidence"] == "grounded"

    def test_the_citation_names_the_field_the_tool_wrote(self):
        """supported=True with an empty citation would be worse than the flag:
        it asserts evidence exists and cannot say where."""
        answer = "deployment never-ready is Running but NotReady."
        verdict = grounding.check(answer, [self.SCAN])

        cited = {c["value"]: c for c in verdict["claims"]}
        assert cited["notready"]["status"] == "observed"
        assert cited["notready"]["evidence"][0]["field"].endswith("fault")

    def test_a_status_no_tool_spelled_any_way_is_still_flagged(self):
        answer = "deployment never-ready is NotReady."
        scan = json.dumps({"demo/never-ready": {"status": "Running", "pods": 1}})

        assert "notready" in grounding.check(answer, [scan])["unverified"]

    def test_an_alias_does_not_ground_a_different_status(self):
        """The table maps one status to its own spellings, and must not become
        a general loosening of the substring test."""
        answer = "deployment never-ready was OOMKilled."
        result = grounding.check(answer, [self.SCAN])

        assert "oomkilled" in result["unverified"]


class TestAHedgedStatusIsInferenceNotFabrication:
    """
    `scan_cluster` reports a workload's phase, not its termination reason, so
    "CrashLoopBackOff (likely OOMKilled)" is an inference no tool in that run
    could settle. Holding it to the evidence scored a correctly labelled
    answer `partial` -- the opposite of what `inference_is_marked` grades for.

    A hedged cause was already exempt. These pin the same treatment for a
    status, and pin that the exemption does not extend to a flat assertion.
    """

    SCAN = json.dumps({"demo/memory-hog": {"status": "CrashLoopBackOff",
                                           "pods": 1}})

    def test_a_hedged_status_does_not_count_against_grounding(self):
        answer = "demo/memory-hog is in CrashLoopBackOff, likely OOMKilled."
        result = grounding.check(answer, [self.SCAN])

        assert result["unverified"] == []
        assert result["confidence"] == "grounded"

    def test_the_same_status_asserted_flatly_is_still_caught(self):
        answer = "demo/memory-hog is in CrashLoopBackOff and was OOMKilled."
        result = grounding.check(answer, [self.SCAN])

        assert "oomkilled" in result["unverified"]
        assert result["confidence"] == "partial"

    def test_a_hedged_status_is_recorded_as_an_inference(self):
        """Exempt from the evidence test, not dropped from the record: the
        reader has to be able to see what the answer guessed at."""
        answer = "demo/memory-hog is in CrashLoopBackOff, likely OOMKilled."
        verdict = grounding.check(answer, [self.SCAN])

        inferred = [c for c in verdict["claims"] if c["status"] == "inferred"]
        assert [c["value"] for c in inferred] == ["oomkilled"]
        assert inferred[0]["evidence"] == []

        contract = grounding.contract(verdict)
        assert {"claim": "oomkilled", "kind": "status"} in contract["inferences"]
        assert "oomkilled" not in [o["claim"] for o in contract["observations"]]

    def test_a_hedged_status_a_tool_did_report_is_still_an_observation(self):
        """Hedging cannot demote a measurement. The model may write "appears
        to be OOMKilled" about a pod describe_pod says was OOMKilled, and that
        claim is evidenced whatever voice it is in."""
        tools = [json.dumps({"pod": "memory-hog-x", "status": "OOMKilled"})]
        verdict = grounding.check("pod memory-hog-x appears OOMKilled.", tools)

        observed = {c["value"]: c for c in verdict["claims"]}
        assert observed["oomkilled"]["status"] == "observed"
        assert observed["oomkilled"]["evidence"]

    def test_an_answer_of_only_hedged_statuses_is_not_grounded(self):
        """The exemption must not manufacture confidence. With nothing else
        checkable, a wholly speculative answer has no verified claim, and
        `grounded` requires one."""
        result = grounding.check("The pod may be OOMKilled.", [self.SCAN])

        assert result["checked"] == 0
        assert result["confidence"] == grounding.INSUFFICIENT

    def test_a_hedged_cause_is_recorded_too(self):
        """It used to be skipped outright, so a hedged cause left no trace at
        all. Same exemption, same record."""
        verdict = grounding.check("Possibly a memory leak in pod memory-hog-x.",
                                  [json.dumps({"pod": "memory-hog-x"})])

        inferred = [c for c in verdict["claims"] if c["status"] == "inferred"]
        assert [c["value"] for c in inferred] == ["memory leak"]


class TestNoToolsCalled:
    def test_figures_without_any_tool_call_are_ungrounded(self):
        result = grounding.check("CPU is at 42%.", [])

        assert result["confidence"] == "ungrounded"
        assert result["unverified"] == ["42"]

    def test_refusal_without_tools_is_insufficient_not_confirmed(self):
        """
        Corrected 2026-08-19. An honest refusal is still not a grounded
        finding: nothing was measured. `insufficient_evidence` says that
        without penalising the refusal as a wrong answer, which is what
        `partial` or `ungrounded` would imply.
        """
        result = grounding.check("I could not determine that.", [])

        assert result["confidence"] == grounding.INSUFFICIENT
        assert result["unverified"] == []


class TestCheckedCount:
    """
    "Nothing contradicted this" and "nothing was claimed" both come back
    grounded, and a caller badging the first as confirmation will badge the
    second the same way. The count is what separates them.
    """

    def test_answer_with_no_measurable_claim_checks_nothing(self):
        tools = [json.dumps({"pods": []})]
        result = grounding.check("I cannot identify any failing pods.", tools)

        # Corrected 2026-08-19: zero claims checked is its own verdict now.
        assert result["confidence"] == grounding.INSUFFICIENT
        assert result["checked"] == 0

    def test_traceable_claims_are_counted(self):
        tools = [json.dumps({"reason": "OOMKilled", "limits": {"memory": "64Mi"}})]
        result = grounding.check("memory-hog was OOMKilled at its 64Mi limit.", tools)

        assert result["confidence"] == "grounded"
        assert result["checked"] > 0

    def test_untraceable_claims_are_counted_too(self):
        # Counted, not just flagged: checked is how many were examined.
        tools = [json.dumps({"pod": "p", "status": "Running"})]
        result = grounding.check("The pod restarted 9 times.", tools)

        assert result["confidence"] == "partial"
        assert result["checked"] == 1

    def test_no_tools_and_no_claims_checks_nothing(self):
        assert grounding.check("I could not determine that.", [])["checked"] == 0


class TestObservedFailuresFrom20260818:
    """
    One test per failure seen against a live cluster on 2026-08-18. Each of
    these was a real answer from qwen3 that the old classifier accepted.
    """

    POD = json.dumps({
        "pod": "memory-hog-bc76968c6-87fbc",
        "namespace": "demo",
        "status": "OOMKilled",
        "containers": {"hog": {"limits": {"memory": "64Mi"}}},
    })

    def test_a_the_empty_answer_is_not_grounded(self):
        """
        Observed: a run whose only tool call 404'd returned "" and the
        classifier called it grounded, because an empty string contradicts
        nothing.
        """
        tools = [json.dumps({"error": 'kubernetes API error 404: services "x" not found'})]

        assert grounding.check("", tools)["confidence"] == "ungrounded"
        assert grounding.check("   \n  ", tools)["confidence"] == "ungrounded"

    def test_b_a_fabricated_number_beside_a_correct_rca(self):
        """Observed: "Memory limit: 512Mi" when the tool reported 64Mi."""
        answer = "memory-hog was OOMKilled. Memory limit: 512Mi."
        result = grounding.check(answer, [self.POD])

        assert result["confidence"] == "partial"
        assert "512" in result["unverified"]

    def test_c_a_fabricated_status_beside_a_correct_rca(self):
        """
        Observed: "readiness probe fails with 503 Service Unavailable" when
        the kubelet reported connection refused.
        """
        pod = json.dumps({
            "pod": "never-ready-7d86d8c5f7-6rw9t",
            "status": "Running",
            "events": "Readiness probe failed: connect: connection refused",
        })
        answer = "never-ready-7d86d8c5f7-6rw9t is in CreateContainerConfigError."

        assert grounding.check(answer, [pod])["confidence"] == "partial"

    def test_d_evidence_from_one_workload_cannot_support_another(self):
        scan = json.dumps({
            "demo/memory-hog": {"status": "OOMKilled", "pods": 1},
            "demo/healthy-web": {"status": "Running", "pods": 2},
        })
        result = grounding.check("healthy-web is OOMKilled.", [scan])

        assert result["confidence"] == "partial"
        assert "oomkilled" in result["unverified"]

    def test_e_claims_with_no_tool_call_are_ungrounded(self):
        result = grounding.check("The pod restarted 5 times.", [])

        assert result["confidence"] == "ungrounded"

    def test_f_a_claim_matching_an_exact_field_is_grounded_and_cited(self):
        result = grounding.check(
            "memory-hog-bc76968c6-87fbc was OOMKilled at its 64Mi limit.",
            grounding.records([self.POD], names=["describe_pod"]),
        )

        assert result["confidence"] == "grounded"
        cited = {c["value"]: c for c in result["claims"]}
        assert cited["oomkilled"]["evidence"][0]["tool"] == "describe_pod"
        assert cited["oomkilled"]["evidence"][0]["field"] == "status"
        assert cited["64"]["evidence"][0]["field"] == "containers.hog.limits.memory"

    def test_g_an_answer_stating_nothing_checkable_is_insufficient(self):
        result = grounding.check("The workload appears to be having trouble.", [self.POD])

        assert result["confidence"] == grounding.INSUFFICIENT
        assert result["checked"] == 0

    def test_the_exit_137_misattribution_is_caught(self):
        """
        Observed: "exit code 137 indicates the container was killed by the OOM
        killer" for a pod whose termination reason was Error and which had no
        memory limit at all.
        """
        pod = json.dumps({
            "pod": "liveness-flapper-5bd4f768c5-jq5k7",
            "status": "CrashLoopBackOff",
            "last_termination": {"reason": "Error", "exit_code": 137},
            "containers": {"app": {"limits": {}}},
        })
        answer = "liveness-flapper-5bd4f768c5-jq5k7 was OOMKilled after exit 137."

        verdict = grounding.check(answer, [pod])

        # Was `partial` until 2026-08-23, which was the strongest thing the
        # checker could say: `oomkilled` traced to nothing, so it was flagged
        # as unsupported. It is worse than unsupported -- the evidence names a
        # different termination reason -- and contradiction.py can now say so.
        # The assertion is strengthened, not relaxed: this misattribution is
        # still caught, and now with the reason it is wrong attached.
        assert verdict["confidence"] == grounding.CONTRADICTED
        assert verdict["contradictions"][0]["rule"] == \
            "termination_reason_vs_memory_cause"
        assert verdict["contradictions"][0]["measured"] == \
            "last_termination.reason = error"


class TestEvidenceAudit:
    """
    The dangerous failure is a correct RCA carrying a fabricated figure: the
    headline is right, so a reader has no way to tell which half to trust.
    """

    POD = json.dumps({"pod": "memory-hog", "status": "OOMKilled",
                      "containers": {"hog": {"limits": {"memory": "64Mi"}}}})

    def test_an_unsupported_figure_is_named_as_inference(self):
        answer = "memory-hog was OOMKilled. Memory limit: 512Mi."
        out = grounding.annotate(answer, grounding.check(answer, [self.POD]))

        assert "512" in out
        assert "inference, not measurement" in out
        # The model's own prose is preserved, not rewritten.
        assert out.startswith(answer)

    def test_a_grounded_answer_carries_no_boilerplate(self):
        answer = "memory-hog was OOMKilled at its 64Mi limit."

        assert grounding.annotate(answer, grounding.check(answer, [self.POD])) == answer

    def test_a_correct_short_answer_is_not_annotated(self):
        """
        The first cut put "Root cause: UNKNOWN" under "It is healthy." --
        a false alarm on a complete answer, which is how a signal becomes
        something readers skip.
        """
        answer = "It is healthy."
        verdict = grounding.check(answer, [self.POD])

        assert verdict["confidence"] == grounding.INSUFFICIENT
        assert grounding.annotate(answer, verdict) == answer

    def test_an_answer_with_no_tools_says_nothing_was_measured(self):
        answer = "The pod restarted 9 times."
        out = grounding.annotate(answer, grounding.check(answer, []))

        assert "Nothing here was measured" in out

    def test_annotating_twice_does_not_stack(self):
        answer = "memory-hog was OOMKilled. Memory limit: 512Mi."
        once = grounding.annotate(answer, grounding.check(answer, [self.POD]))
        twice = grounding.annotate(once, grounding.check(answer, [self.POD]))

        assert once == twice

    def test_an_empty_answer_is_left_empty(self):
        assert grounding.annotate("", grounding.check("", [])) == ""


class TestVerifyThenRewrite:
    """
    Detection was not prevention. The audit named the fabricated value and left
    it standing in the prose, so "the pod has a 512Mi memory limit" still read
    as a measurement and a reader skimming for the number found the wrong one.
    These pin that no unsupported specific survives as a statement of fact.
    """

    POD = json.dumps({
        "pod": "memory-hog-bc76968c6-87fbc",
        "namespace": "demo",
        "status": "OOMKilled",
        "containers": {"hog": {"limits": {"memory": "64Mi"}}},
    })

    def rewrite(self, answer, tools=None):
        tools = tools or [self.POD]
        return grounding.verify(answer, grounding.check(answer, tools), tools)

    # 1. correct RCA + wrong number
    def test_a_wrong_number_does_not_survive(self):
        out, edits = self.rewrite(
            "memory-hog-bc76968c6-87fbc has a 512Mi memory limit and was OOMKilled."
        )

        assert "512Mi" not in out
        assert edits[0]["action"] == "corrected"

    # 4. contradictory evidence -> observed wins
    def test_the_observed_value_replaces_it(self):
        out, _ = self.rewrite(
            "memory-hog-bc76968c6-87fbc has a 512Mi memory limit and was OOMKilled."
        )

        assert "64Mi (observed)" in out
        assert "OOMKilled" in out          # the correct half is untouched

    # 2. correct RCA + wrong status code
    def test_a_fabricated_status_code_does_not_survive(self):
        out, _ = self.rewrite(
            "never-ready is not ready. The probe returned HTTP 503.",
            [json.dumps({"pod": "never-ready", "status": "Running",
                         "events": "probe failed: connection refused"})],
        )

        assert "HTTP 503." not in out
        assert "[unverified: 503]" in out

    # 3. unsupported causal explanation
    def test_an_unsupported_cause_is_marked(self):
        out, _ = self.rewrite("The application has a memory leak.")

        assert "[unverified: memory leak]" in out

    def test_a_hedged_cause_is_left_alone(self):
        """The prompt asks the model to mark inference; punishing it would be
        the wrong lesson."""
        answer = "The pod was OOMKilled, likely a memory leak."
        out, edits = self.rewrite(answer)

        assert out == answer
        assert edits == []

    # 5. workload A's evidence cannot carry workload B's claim
    def test_a_claim_cannot_borrow_another_workloads_evidence(self):
        scan = json.dumps({
            "demo/memory-hog": {"status": "OOMKilled", "pods": 1},
            "demo/healthy-web": {"status": "Running", "pods": 2},
        })
        out, _ = self.rewrite("healthy-web was OOMKilled.", [scan])

        assert "[unverified: OOMKilled]" in out

    # 6 and 7
    def test_an_empty_answer_is_never_grounded(self):
        assert grounding.check("", [self.POD])["confidence"] == "ungrounded"
        assert grounding.check("   ", [])["confidence"] == "ungrounded"

    def test_an_answer_of_only_unsupported_claims_is_never_grounded(self):
        answer = "The pod restarted 9 times and used 4096Mi."

        assert grounding.check(answer, [self.POD])["confidence"] == "partial"
        assert grounding.check(answer, [])["confidence"] == "ungrounded"

    def test_a_measured_answer_is_returned_byte_identical(self):
        answer = "memory-hog-bc76968c6-87fbc was OOMKilled at its 64Mi limit."
        out, edits = self.rewrite(answer)

        assert out == answer
        assert edits == []

    def test_a_recommendation_is_not_rewritten(self):
        """Proposing 256Mi is not asserting it, and mangling advice is worse
        than the fabrication this removes."""
        out, _ = self.rewrite(
            "The limit is 512Mi. Fix: raise it to 256Mi."
        )

        assert "raise it to 256Mi" in out

    def test_a_pod_hash_is_never_corrupted(self):
        """
        A flagged "3" must not rewrite the 3 inside a ReplicaSet hash. A
        redaction pass that corrupts pod names is worse than the fabrication.
        """
        answer = "memory-hog-bc76968c6-87fbc restarted 3 times."
        out, _ = self.rewrite(answer)

        assert "memory-hog-bc76968c6-87fbc" in out
        assert "[unverified: 3]" in out

    def test_a_decimal_is_not_split_by_its_integer_part(self):
        out, _ = self.rewrite("CPU is at 3.5 percent.")

        assert "[unverified: 3.5]" in out
        assert "[unverified: 3].5" not in out

    def test_rewriting_is_idempotent(self):
        answer = "The limit is 512Mi."
        once, _ = self.rewrite(answer)
        twice, _ = self.rewrite(once)

        assert once == twice


class TestFactContract:
    def test_observations_carry_their_evidence(self):
        pod = json.dumps({"pod": "memory-hog", "status": "OOMKilled",
                          "containers": {"hog": {"limits": {"memory": "64Mi"}}}})
        verdict = grounding.check(
            "memory-hog was OOMKilled at its 64Mi limit.",
            grounding.records([pod], names=["describe_pod"]),
        )
        rca = grounding.contract(verdict)

        assert {o["claim"] for o in rca["observations"]} == {"64", "oomkilled"}
        assert all(o["evidence"][0]["tool"] == "describe_pod"
                   for o in rca["observations"])

    def test_unknowns_are_what_was_stated_and_could_not_be_supported(self):
        pod = json.dumps({"pod": "p", "status": "OOMKilled"})
        answer = "p was OOMKilled and restarted 9 times."
        verdict = grounding.check(answer, [pod])
        _, edits = grounding.verify(answer, verdict, [pod])

        assert grounding.contract(verdict, edits)["unknowns"] == ["9"]


class TestEntityIdentityIsChecked:
    """
    The last grounding hole: values were checked, identity was not. Observed
    live 2026-08-19 -- "The crasher pod log-shipper-8gnqk is in Error with 7
    restarts". Error and 7 were both measured, for log-shipper, so every value
    checked out and the answer scored grounded while naming the wrong workload
    in the same breath.
    """

    POD = json.dumps({
        "pod": "crasher-abc123", "namespace": "demo",
        "status": "CrashLoopBackOff", "restarts": 4,
    })

    def test_a_pod_no_tool_returned_fails_even_with_correct_values(self):
        result = grounding.check(
            "The pod log-shipper-xyz has 4 restarts and is in CrashLoopBackOff.",
            [self.POD],
        )

        assert result["confidence"] == "partial"
        assert "pod log-shipper-xyz" in result["unverified"]

    def test_the_pod_the_tool_did_return_passes(self):
        result = grounding.check(
            "The pod crasher-abc123 has 4 restarts.", [self.POD]
        )

        assert result["confidence"] == "grounded"

    def test_the_workload_name_still_reaches_its_pod(self):
        """An answer says `crasher`; the tool keyed `crasher-abc123`."""
        assert grounding.check(
            "The pod crasher has 4 restarts.", [self.POD]
        )["confidence"] == "grounded"

    @pytest.mark.parametrize("kind", [
        "pod", "deployment", "service", "node", "container", "daemonset",
    ])
    def test_every_labelled_kind_is_checked(self, kind):
        result = grounding.check(f"The {kind} ghost-xyz-99 is failing.", [self.POD])

        assert f"{kind} ghost-xyz-99" in result["unverified"]

    def test_a_fabricated_entity_is_rewritten_out(self):
        answer = "The pod log-shipper-xyz has 4 restarts."
        out, edits = grounding.verify(
            answer, grounding.check(answer, [self.POD]), [self.POD]
        )

        # The labelled phrase is marked, not the bare name: "the [unverified:
        # pod log-shipper-xyz]" reads correctly, where marking only the name
        # would leave a bare "pod" doing the asserting.
        assert "[unverified: pod log-shipper-xyz]" in out
        assert any(e["action"] == "marked" for e in edits)

    def test_an_english_word_after_a_kind_is_not_an_entity(self):
        """
        "The pod restarted 9 times" captured `restarted` as a pod name in the
        first cut and reported a nonexistent pod. A grounding check that flags
        English is worse than no check.
        """
        result = grounding.check("The pod restarted 9 times.", [self.POD])

        assert not any("restarted" in u for u in result["unverified"])

    def test_a_namespace_that_is_only_a_field_value_is_known(self):
        """
        `demo` is a field in the result, not a document subject, so the index
        does not key it. Flagging it would fire on almost every correct answer.
        """
        result = grounding.check(
            "The pod crasher-abc123 in namespace demo has 4 restarts.", [self.POD]
        )

        assert result["confidence"] == "grounded"

    def test_cross_contamination_between_two_real_workloads(self):
        """Both pods exist; the claim attaches one's status to the other."""
        scan = json.dumps({
            "demo/memory-hog": {"status": "OOMKilled", "pods": 1},
            "demo/healthy-web": {"status": "Running", "pods": 2},
        })
        result = grounding.check("The deployment healthy-web is OOMKilled.", [scan])

        assert result["confidence"] == "partial"
        assert "oomkilled" in result["unverified"]
