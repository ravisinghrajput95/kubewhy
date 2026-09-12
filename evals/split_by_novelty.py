"""
The one split this suite's headline must never hide.

Defect 44: the suite scored 88% over 34 cases, and that number was 97% on the
29 cases the prompts were written against and 40% on the five fault types they
had never seen. The headline is the average of two different products — a tool
that explains faults its author enumerated, and a tool that explains
Kubernetes. Only the second one is the claim anybody reads.

That split was computed by hand, once, which is why it is worth a script: a
figure nobody can recompute is a figure that stops being recomputed.

Membership comes from `UNCOVERED_CASES` in `cases.py` rather than a list
copied into this file, so a case that moves between the two sets moves here
too. A run whose case name is in neither set is reported rather than silently
dropped — that is how a renamed case would otherwise flatter one half.

    python evals/split_by_novelty.py results/uncovered-n3-2026-09-12.json
    python evals/split_by_novelty.py --self-check
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evals.cases import CASES, UNCOVERED_CASES  # noqa: E402

NOVEL = {c["name"] for c in UNCOVERED_CASES}
PRE_EXISTING = {c["name"] for c in CASES} - NOVEL


def wilson(passes, total, z=1.96):
    """95% confidence interval for a binomial proportion, as (low, high) %."""
    if not total:
        return 0.0, 100.0
    p = passes / total
    denom = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denom
    return max(0.0, centre - margin) * 100, min(1.0, centre + margin) * 100


def load(paths):
    runs = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        rows = loaded if isinstance(loaded, list) else (
            loaded.get("runs") or loaded.get("results") or [])
        runs += [r for r in rows if isinstance(r, dict)]
    return runs


def partition(runs):
    """Runs grouped by whether the prompts had ever seen the fault type."""
    groups = {"pre-existing": [], "never seen": [], "unclassified": []}
    for run in runs:
        if run.get("void"):
            continue
        name = run.get("case")
        if name in NOVEL:
            groups["never seen"].append(run)
        elif name in PRE_EXISTING:
            groups["pre-existing"].append(run)
        else:
            groups["unclassified"].append(run)
    return groups


# What a failure was, because "failed" is not one thing and the halves are
# not comparable without it. A run that named the right root cause and fell
# short of the grounding bar is a different product from one that named the
# wrong cause: the first is a checker that wants evidence the answer did not
# cite, the second sends an on-call reader to debug healthy code. Four
# verdicts exist for the same reason -- see the note in `grounding.py` -- and
# summing two of them into one number is the mistake this file is built
# against.
FAILURE_KINDS = (
    ("wrong answer", ("none of ", "missing ", "wrongly claimed ")),
    ("tool expectation", ("never called ", "should not have called ")),
    ("grounding verdict", ("grounding verdict ",)),
    ("unverified claim", ("unverified claims: ",)),
    ("harness", ("payload ",)),
)


def classify(failure):
    for kind, prefixes in FAILURE_KINDS:
        if failure.startswith(prefixes):
            return kind
    return "unrecognised"


def failure_breakdown(runs, out, indent="  "):
    """
    How the failures in a group divide, and how many of them left the root
    cause intact.
    """
    failed = [r for r in runs if not r.get("passed")]
    if not failed:
        return

    kinds = defaultdict(int)
    substantive = 0
    for run in failed:
        reasons = run.get("failures") or []
        seen = {classify(f) for f in reasons} or {"unrecognised"}
        for kind in seen:
            kinds[kind] += 1
        if seen & {"wrong answer", "unrecognised"}:
            substantive += 1

    print(f"{indent}{len(failed)} failed: "
          + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(),
                                                    key=lambda kv: -kv[1])),
          file=out)
    print(f"{indent}{substantive} of those named the wrong cause; "
          f"{len(failed) - substantive} named the right one and missed a bar "
          "around it", file=out)


def line(label, runs):
    passes = sum(1 for r in runs if r.get("passed"))
    total = len(runs)
    if not total:
        return f"{label:<14} {'-':>28}"
    low, high = wilson(passes, total)
    grounded = sum(1 for r in runs if r.get("confidence") == "grounded")
    return (f"{label:<14} {passes:>3}/{total:<3} {100 * passes / total:5.1f}%  "
            f"[{low:4.1f}-{high:5.1f}] 95% CI   grounded {grounded}/{total}")


def report(runs, out=sys.stdout):
    def say(text=""):
        print(text, file=out)

    groups = partition(runs)
    scored = groups["pre-existing"] + groups["never seen"] + groups["unclassified"]

    say(line("headline", scored))
    say("  -- and the headline is the average of these two, so do not quote "
        "it alone:")
    say(line("pre-existing", groups["pre-existing"]))
    failure_breakdown(groups["pre-existing"], out, indent="               ")
    say(line("never seen", groups["never seen"]))
    failure_breakdown(groups["never seen"], out, indent="               ")

    if groups["unclassified"]:
        names = sorted({r.get("case") for r in groups["unclassified"]})
        say(f"\nunclassified   {len(groups['unclassified'])} runs in neither "
            f"set: {', '.join(str(n) for n in names)}")
        say("               a case name that matches no entry in cases.py "
            "counts toward the headline\n               and neither half. "
            "Fix the name or the set before quoting anything.")

    voided = sum(1 for r in runs if r.get("void"))
    if voided:
        say(f"\nVOID           {voided} run(s) excluded -- the provider did "
            "not answer")

    say("\nper case, never-seen half:")
    for name in sorted(NOVEL):
        rows = [r for r in groups["never seen"] if r.get("case") == name]
        passes = sum(1 for r in rows if r.get("passed"))
        say(f"  {name:<34} {passes}/{len(rows)}")

    failed = defaultdict(int)
    for run in groups["pre-existing"]:
        if not run.get("passed"):
            failed[run.get("case")] += 1
    if failed:
        say("\nfailures in the pre-existing half:")
        for name, count in sorted(failed.items(), key=lambda kv: -kv[1]):
            total = sum(1 for r in groups["pre-existing"] if r.get("case") == name)
            say(f"  {name:<34} {count}/{total} failed")


def self_check():
    """
    Prove the two halves are actually being told apart.

    The failure mode is a partition that silently puts everything in one
    bucket — which reports a plausible headline and two halves that agree
    with it. So: build runs for one case from each set, at deliberately
    opposite outcomes, and require the halves to come back opposite. A
    partition that has stopped partitioning cannot produce that.
    """
    if not NOVEL or not PRE_EXISTING:
        raise SystemExit(
            f"self-check FAILED: cases.py yields {len(NOVEL)} never-seen and "
            f"{len(PRE_EXISTING)} pre-existing case names. One of the sets is "
            "empty, so every run would land in the other half.")

    novel_name = sorted(NOVEL)[0]
    old_name = sorted(PRE_EXISTING)[0]
    runs = ([{"case": novel_name, "passed": False, "confidence": "partial"}] * 5
            + [{"case": old_name, "passed": True, "confidence": "grounded"}] * 5
            + [{"case": "a_case_that_does_not_exist", "passed": True}])

    groups = partition(runs)
    if len(groups["never seen"]) != 5 or len(groups["pre-existing"]) != 5:
        raise SystemExit(
            f"self-check FAILED: 5 runs of {novel_name!r} and 5 of "
            f"{old_name!r} partitioned as {len(groups['never seen'])} never-seen "
            f"and {len(groups['pre-existing'])} pre-existing.")
    if any(r["passed"] for r in groups["never seen"]) or \
            not all(r["passed"] for r in groups["pre-existing"]):
        raise SystemExit(
            "self-check FAILED: the halves came back with each other's runs.")
    if len(groups["unclassified"]) != 1:
        raise SystemExit(
            "self-check FAILED: a run whose case name is in neither set was "
            "not reported as unclassified. It would be dropped silently.")

    print(f"self-check passed: {len(PRE_EXISTING)} pre-existing and "
          f"{len(NOVEL)} never-seen case names, told apart, and a run "
          "belonging to neither is surfaced rather than dropped")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("paths", nargs="*", help="results JSON files")
    parser.add_argument("--self-check", action="store_true",
                        help="prove the partition works, then exit")
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()
    if not args.paths:
        parser.error("give at least one results file, or --self-check")

    runs = load(args.paths)
    if not runs:
        print("no runs found", file=sys.stderr)
        return 1
    report(runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
