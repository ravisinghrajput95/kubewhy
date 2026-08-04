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
