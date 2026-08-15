"""
Tests for the eval graders.

The graders decide what counts as a pass, so a wrong one does not fail loudly
-- it publishes a plausible number that means nothing. `validate.py` already
treats the suite as code; this treats the scoring the same way.

No cluster and no model: the graders are pure functions over a finding dict.
"""

import importlib.util
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
