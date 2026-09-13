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
import subprocess
import sys

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


# --- the suite count itself is a documented measurement -------------------
#
# Added 2026-09-12. Three documents state how many tests this repo has, and on
# 2026-09-10 rewriting one of them left it claiming 1711 and 1699 about the
# same tree in adjacent paragraphs. Re-reading caught that; nothing automatic
# did. Measured today: README.md and VALIDATION.md both say 1696 passing and 0
# skipped, the handoff says 1711 passed and 34 skipped, and the tree collects
# 1745 -- so two of the three were stale by 49 tests and neither was flagged.
#
# `passed + skipped` rather than `passed`, because the skip count is exactly
# what a claim like this has to carry: 34 shared-state cases skip silently
# with no Postgres, so "1711 passed" and "1745 passed" are the same tree in
# two environments and only their sum is invariant.

SUITE_CLAIM = re.compile(
    # "1711 passed, 34 skipped", bolded or not.
    r"(\d[\d,]*)\s*\**\s*pass(?:ed|ing)\**,?\s*\**\s*(\d+)\s*\**\s*skipped")

# Where each document makes its *whole-suite* claim, and nothing else.
#
# Adjacency of the two halves is not enough on its own to tell a whole-suite
# claim from a subset one: the handoff also says "512 passed, 0 skipped" about
# the six files that drive `agent.py`, which is true and has nothing to do
# with the size of the suite. So each document names the one place it speaks
# for the whole tree, and this reads only there. If a document stops carrying
# that anchor the vacuity guard below fails rather than passing quietly.
WHOLE_SUITE_CLAIM = [
    (os.path.join(ROOT, "README.md"), r"^\|\s*Automated tests\s*\|[^\n]*"),
    (os.path.join(ROOT, "docs", "VALIDATION.md"),
     r"^\|\s*Automated test suite\s*\|[^\n]*"),
    (os.path.join(ROOT, "NEXT-SESSION.md"), r"\*\*State:.{0,400}"),
]


def whole_suite_claims(path, anchor):
    """Every (passed, skipped) pair the document states about the whole tree."""
    text = open(path, encoding="utf-8").read()
    region = re.search(anchor, text, re.M | re.S)
    assert region, (
        f"{os.path.basename(path)} no longer carries the anchor this test "
        f"reads its suite count from ({anchor!r}). Either the document was "
        "restructured or the claim was removed; this test cannot tell the "
        "difference, so check by hand.")
    return SUITE_CLAIM.findall(region.group(0))


def collected_tests():
    """
    How many tests this tree actually has, by asking pytest to collect them.

    A subprocess rather than this session's own `request.session.items`,
    because that number is whatever the invocation collected -- running one
    file would make this test pass against a count of eight.
    """
    # No `-q` here. pytest.ini already carries `addopts = -q`, and a second
    # one makes it `-qq`, which suppresses the "N tests collected" line
    # entirely -- the first version of this asked for `-q` and every one of
    # these tests skipped, silently, exactly as if the documents agreed.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)

    found = re.search(r"(\d+) tests? collected", proc.stdout)
    if found:
        return int(found.group(1))

    # Fallback: `-qq` prints one `path: N` line per file and no summary.
    per_file = re.findall(r"^\S+\.py: (\d+)$", proc.stdout, re.M)
    if per_file:
        return sum(int(n) for n in per_file)

    # Not a skip. A test that cannot measure has to say so loudly: a skip
    # here reads as a green suite whose documents were never checked, which
    # is the failure this whole file exists to catch.
    raise AssertionError(
        f"could not read a collected count from pytest (exit "
        f"{proc.returncode}). stdout tail: {proc.stdout[-400:]!r}")


class TestTheDocumentedSuiteCountMatchesTheTree:
    @pytest.fixture(scope="class")
    @classmethod
    def collected(cls):
        return collected_tests()

    @pytest.mark.parametrize("path,anchor", WHOLE_SUITE_CLAIM,
                             ids=lambda v: os.path.basename(v)
                             if isinstance(v, str) and v.endswith(".md") else "")
    def test_every_claim_sums_to_the_collected_count(self, path, anchor,
                                                     collected):
        claims = whole_suite_claims(path, anchor)

        # The vacuity guard, and it is not decoration: a regex that stops
        # matching reports every document as consistent. Two of these three
        # documents were wrong the day this test was written, so "no claims
        # found" means the pattern broke, not that the docs got tidy.
        assert claims, (
            f"{os.path.basename(path)} states no 'N passed, M skipped' figure "
            "where it speaks for the whole tree.")

        for passed, skipped in claims:
            total = int(passed.replace(",", "")) + int(skipped)
            assert total == collected, (
                f"{os.path.basename(path)} says {passed} passed and {skipped} "
                f"skipped ({total}); the tree collects {collected}. The skip "
                "count moves with whether Postgres is up, so the sum is what "
                "has to match.")

    def test_the_documents_agree_with_each_other(self, collected):
        """
        The adjacent-paragraph failure, stated directly. Two documents can
        each be internally consistent and still disagree, and a reader who
        opens one of them has no way to know which is the stale one.
        """
        sums = {}
        for path, anchor in WHOLE_SUITE_CLAIM:
            for passed, skipped in whole_suite_claims(path, anchor):
                sums.setdefault(os.path.basename(path), set()).add(
                    int(passed.replace(",", "")) + int(skipped))

        distinct = {total for totals in sums.values() for total in totals}
        assert len(distinct) <= 1, (
            f"the documents disagree about the size of the suite: {sums}")
