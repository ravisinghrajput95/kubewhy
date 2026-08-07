"""
Tests for the claim checker.

The cases below are drawn from answers this agent actually produced, so the
checker is pinned against real behaviour rather than invented examples.
"""

import json

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

    def test_a_measurement_before_the_fix_is_still_checked(self):
        """The exemption starts at the recommendation, not at the line."""
        tools = [json.dumps({"limits": {"memory": "64Mi"}})]
        answer = "The pod has restarted 47 times. Raise the limit to 128Mi."

        result = grounding.check(answer, tools)

        # 47 was never measured and must still be caught; 128 is a proposal.
        assert result["unverified"] == ["47"]


class TestKnownWeakness:
    """
    Documented limits, pinned so they are visible rather than surprising.

    These assert what the checker currently does, not what it should do. If a
    change makes one fail, the checker got stronger -- update the test and the
    docstring in grounding.py together.
    """

    def test_status_measured_for_one_workload_launders_another(self):
        # Observed on a live cluster: scan_cluster returned ErrImagePull for
        # staging/payments-api and ImagePullBackOff for demo/bad-image, and the
        # model reported ErrImagePull for demo/bad-image. Claims are checked
        # against every tool result at once, so the misattribution passes.
        tools = [
            json.dumps(
                {
                    "staging/payments-api": {"status": "ErrImagePull", "pods": 3},
                    "demo/bad-image": {"status": "ImagePullBackOff", "pods": 1},
                }
            )
        ]

        assert (
            grounding.check("demo/bad-image is in ErrImagePull.", tools)["confidence"]
            == "grounded"
        )

    def test_but_a_status_no_workload_had_is_still_caught(self):
        """The check is weakened by a wide result, not defeated by it."""
        tools = [
            json.dumps(
                {
                    "staging/payments-api": {"status": "ErrImagePull", "pods": 3},
                    "demo/bad-image": {"status": "ImagePullBackOff", "pods": 1},
                }
            )
        ]

        result = grounding.check("demo/bad-image is Evicted.", tools)

        assert result["confidence"] == "partial"
        assert "evicted" in result["unverified"]


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

    def test_answer_with_no_figures_is_grounded(self):
        tools = [json.dumps({"status": "Running"})]
        assert grounding.check("Everything looks fine.", tools)["confidence"] == "grounded"

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


class TestNoToolsCalled:
    def test_figures_without_any_tool_call_are_ungrounded(self):
        result = grounding.check("CPU is at 42%.", [])

        assert result["confidence"] == "ungrounded"
        assert result["unverified"] == ["42"]

    def test_refusal_without_tools_is_not_penalised(self):
        assert grounding.check("I could not determine that.", [])["confidence"] == "grounded"
