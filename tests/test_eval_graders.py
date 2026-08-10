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
planned_instead_of_looking = controller_eval.planned_instead_of_looking


def finding(diagnosis, tool_calls):
    return {"diagnosis": diagnosis, "tool_calls": tool_calls}


class TestPlannedInsteadOfLooking:
    """
    Separates a diagnosis from a plan to produce one.

    Seen live on GKE: for a failing CronJob the controller delivered a numbered
    list of tool calls to make. Every tool was available; none were used.
    """

    def test_the_gke_failure_is_caught(self):
        """The observed text, near enough verbatim."""
        answer = (
            "To find the root cause: 1. Check termination reason: call "
            "describe_pod on the pod. 2. Inspect logs: use get_pod_logs to see "
            "why it exited."
        )

        assert planned_instead_of_looking(finding(answer, [])) == [
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

        assert planned_instead_of_looking(finding(answer, ["get_pod_logs"])) == []

    def test_a_plain_diagnosis_is_not_flagged(self):
        answer = "The container exited 1 after the upstream returned 503."

        assert planned_instead_of_looking(finding(answer, ["get_pod_logs"])) == []

    def test_it_reports_only_the_tools_that_were_skipped(self):
        """A partly-executed plan is still a plan about the missing half."""
        answer = "describe_pod shows exit code 1. Next, run get_pod_logs to see why."

        assert planned_instead_of_looking(
            finding(answer, ["describe_pod"])
        ) == ["get_pod_logs"]

    def test_an_empty_diagnosis_does_not_explode(self):
        """diagnose() can return an answer of None; the grader still has to run."""
        assert planned_instead_of_looking({"diagnosis": None, "tool_calls": []}) == []
        assert planned_instead_of_looking({}) == []
