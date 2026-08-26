"""
Numbers in the documentation must still be derivable from the data they came
from.

This project's documents quote measurements rather than impressions, which is
worth something only while the measurement and the document agree. `results/`
is regenerated as evaluations are re-run; a figure in RUNBOOK.md that quietly
stopped matching it would be indistinguishable from one that was never
measured at all.

So this recomputes from the corpus and compares. It fails loudly and tells you
both numbers, because the correct response is usually to update the document,
not the threshold.
"""

import glob
import json
import os
import re
import statistics

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNBOOK = os.path.join(ROOT, "docs", "RUNBOOK.md")
RESULTS = os.path.join(ROOT, "results", "*.json")


def corpus():
    """Every recorded run across every results file, as flat dicts."""
    runs = []
    for path in sorted(glob.glob(RESULTS)):
        try:
            loaded = json.load(open(path))
        except (ValueError, OSError):
            continue
        rows = loaded if isinstance(loaded, list) else (
            loaded.get("runs") or loaded.get("results") or [])
        if isinstance(rows, list):
            runs += [r for r in rows if isinstance(r, dict)]
    return runs


@pytest.fixture(scope="module")
def runs():
    found = corpus()
    if not found:
        pytest.skip("results/ is empty or absent in this checkout")
    return found


@pytest.fixture(scope="module")
def runbook():
    return open(RUNBOOK, encoding="utf-8").read()


def documented(runbook, label):
    """The count and percentage RUNBOOK.md's verdict table gives for a verdict."""
    row = re.search(
        rf"^\|\s*`{label}`\s*\|\s*([\d,]+)\s*\(([\d.]+)%\)", runbook, re.M)
    assert row, f"no verdict row for {label} in RUNBOOK.md"
    return int(row.group(1).replace(",", "")), float(row.group(2))


class TestTheVerdictTableMatchesTheCorpus:
    @pytest.mark.parametrize(
        "verdict",
        ["grounded", "partial", "insufficient_evidence", "contradicted", "ungrounded"],
    )
    def test_each_verdict_count(self, runs, runbook, verdict):
        actual = sum(1 for r in runs if r.get("confidence") == verdict)
        stated, _ = documented(runbook, verdict)

        assert actual == stated, (
            f"RUNBOOK.md says {stated} {verdict} runs; results/ now has {actual}")

    def test_the_total_is_the_one_quoted(self, runs, runbook):
        stated = int(re.search(r"(\d[\d,]*) recorded runs in `results/`",
                               runbook).group(1).replace(",", ""))
        assert len(runs) == stated

    def test_the_percentages_are_of_that_total(self, runs, runbook):
        """
        A count that drifted and a percentage that did not is the shape of an
        edited document, and the percentage is the half people quote.
        """
        for verdict in ("grounded", "contradicted"):
            count, percent = documented(runbook, verdict)
            assert round(100 * count / len(runs), 1) == percent


class TestTheLatencyFiguresMatchTheCorpus:
    def durations(self, runs):
        return sorted(r["seconds"] for r in runs
                      if isinstance(r.get("seconds"), (int, float)))

    def test_the_median(self, runs, runbook):
        stated = float(re.search(r"median \*\*([\d.]+)s\*\*", runbook).group(1))
        assert round(statistics.median(self.durations(runs)), 1) == stated

    def test_the_p95_and_p99(self, runs, runbook):
        values = self.durations(runs)
        quantile = lambda q: values[min(int(len(values) * q), len(values) - 1)]

        p95 = float(re.search(r"p95\s+\*\*([\d.]+)s\*\*", runbook).group(1))
        p99 = float(re.search(r"p99\s+\*\*([\d.]+)s\*\*", runbook).group(1))

        assert round(quantile(0.95), 1) == p95
        assert round(quantile(0.99), 1) == p99

    def test_the_budget_multiple(self, runs, runbook):
        """
        "roughly 2.2x the p99" is the sentence that makes 600s look chosen
        rather than round. It stops being true when the p99 moves.
        """
        values = self.durations(runs)
        p99 = values[min(int(len(values) * 0.99), len(values) - 1)]
        stated = float(re.search(r"([\d.]+)× the p99", runbook).group(1))

        assert abs(600 / p99 - stated) < 0.1

    def test_the_count_of_runs_over_budget(self, runs, runbook):
        over = [d for d in self.durations(runs) if d > 600]
        stated = re.search(r"(\w+) runs exceeded 600s of wall clock", runbook).group(1)
        words = {"Zero": 0, "One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}

        assert len(over) == words[stated.capitalize()]


class TestTheContradictionCaveatIsStillTrue:
    def test_the_known_defect_still_dominates_contradictions(self, runs, runbook):
        """
        The runbook tells an operator to suspect one known bug first. That
        advice is only good while the bug is actually the common cause.
        """
        contradicted = [r for r in runs if r.get("confidence") == "contradicted"]
        scoping = [r for r in contradicted
                   if r.get("case") == "scoping_quiet_workload_beside_loud_one"]

        stated_count = int(re.search(r"(\d+) of those \d+", runbook).group(1))
        stated_share = int(re.search(r"\*\*(\d+)%\*\*", runbook).group(1))

        assert len(scoping) == stated_count
        assert round(100 * len(scoping) / len(contradicted)) == stated_share
