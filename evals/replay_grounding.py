"""
Replay the recorded corpus through the current grounding checker.

The rule this project holds is: **replay before believing any change to
grounding, contradiction detection or the grader.** It has earned that rule
twice -- a first draft of the contradiction rules produced six false positives
and zero true ones, and a first draft of the SUPPORTED-absence rule called
nine correct answers contradicted. Both were caught here rather than shipped.

Until now the tool that does it was not in the repository. VALIDATION.md
claimed "Grounding replay: PROVEN, 907 recorded runs" while nothing committed
could reproduce it, which is the same criticism FUTURE.md makes of the
mutation harness that was never checked in. A result nobody else can re-derive
is a claim, not evidence.

    python evals/replay_grounding.py                    # every replayable record
    python evals/replay_grounding.py results/final-*.json
    python evals/replay_grounding.py --fail-on-change   # CI gate
    python evals/replay_grounding.py --self-check       # prove the harness works

## What is replayed, and what is not

`check()` is re-run over the record's **draft** and **evidence** -- the two
inputs the checker was originally handed. Never `answer`: that has already
been through verify(), which rewrites unsupported figures in place, and
annotate(), which appends the audit. Scoring the annotated text reproduces a
different verdict and the difference is the tooling, not the change under test.

Records without both fields are skipped and counted. Older runs did not retain
them; that is a property of the record, not a failure of the replay, and the
summary says how many were skipped so a shrinking corpus is visible.

## Three ways a replay has lied here, and what stops each

1. **Stale `__pycache__`.** A "zero changes" result that compared new code
   against a cached old copy. Every run prints the resolved path and a hash of
   the modules it actually loaded.
2. **Module shadowing.** Copies of `grounding.py` and `contradiction.py` sat
   beside the replay script, and Python puts the script's own directory ahead
   of `PYTHONPATH` -- so the replay compared the new code against itself.
   `_assert_not_shadowed()` refuses to run if the loaded module is not the one
   at the repository root.
3. **A harness that is not wired to anything.** The deepest of the three:
   "no regressions" from a replay that never called the checker looks exactly
   like "no regressions" from one that did. `--self-check` perturbs the
   verdict deliberately and fails if the replay does not notice, which is the
   only way this script can demonstrate it is exercising the code it claims to.
"""

import argparse
import collections
import glob
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The repository root, appended rather than inserted at position 0. sys.path[0]
# is already this script's directory, and prepending here would do nothing
# about that -- see _assert_not_shadowed for the half that actually helps.
if ROOT not in sys.path:
    sys.path.append(ROOT)

import grounding          # noqa: E402
import contradiction      # noqa: E402


def _fingerprint(module):
    """Where a module came from and what it contains, as loaded."""
    path = getattr(module, "__file__", "") or ""
    try:
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]
    except OSError:
        digest = "unreadable"
    return path, digest


def _assert_not_shadowed():
    """
    Refuse to run against a copy of the checker sitting next to this script.

    Not hypothetical. It happened, and the replay reported "zero changes"
    because it had compared the new code against itself.
    """
    problems = []
    for module in (grounding, contradiction):
        path, _ = _fingerprint(module)
        expected = os.path.join(ROOT, os.path.basename(path))
        if os.path.abspath(path) != os.path.abspath(expected):
            problems.append(f"{module.__name__} loaded from {path}, not {expected}")
    if problems:
        raise SystemExit(
            "refusing to replay: the checker is being shadowed.\n  "
            + "\n  ".join(problems)
            + "\n\nPython puts this script's directory ahead of PYTHONPATH, so a "
              "copy of grounding.py beside it silently wins. Remove the copy."
        )


def replayable(path):
    """Records from one file that retain both of the checker's inputs."""
    try:
        loaded = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return [], 0

    rows = loaded if isinstance(loaded, list) else (
        loaded.get("runs") or loaded.get("results") or [])
    if not isinstance(rows, list):
        return [], 0

    keep, skipped = [], 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("draft") and row.get("evidence") and row.get("confidence"):
            keep.append(row)
        else:
            skipped += 1
    return keep, skipped


def verdict_of(record, check=None):
    """The current checker's verdict for one recorded run."""
    checker = check or grounding.check
    return checker(record["draft"], record["evidence"])["confidence"]


def replay(paths, check=None):
    """
    Every replayable record, scored again. Returns the transitions and totals.

    Nothing is written back. A replay that edits the corpus it is measuring
    against cannot be run twice and cannot be checked by anyone else.
    """
    moved, same, skipped, errors = [], 0, 0, []
    for path in paths:
        records, without_inputs = replayable(path)
        skipped += without_inputs
        for record in records:
            try:
                now = verdict_of(record, check)
            except Exception as exc:                      # noqa: BLE001
                errors.append({"file": path, "case": record.get("case"),
                               "error": f"{type(exc).__name__}: {exc}"})
                continue
            if now == record["confidence"]:
                same += 1
            else:
                moved.append({
                    "file": os.path.basename(path),
                    "case": record.get("case"),
                    "model": record.get("model"),
                    "was": record["confidence"],
                    "now": now,
                })
    return {"moved": moved, "unchanged": same,
            "skipped_no_inputs": skipped, "errors": errors}


def summarise(result):
    total = result["unchanged"] + len(result["moved"])
    lines = [f"replayed {total} records — {result['unchanged']} unchanged, "
             f"{len(result['moved'])} moved, "
             f"{result['skipped_no_inputs']} skipped (no draft/evidence)"]

    if result["errors"]:
        lines.append(f"  {len(result['errors'])} raised — the checker must not "
                     "raise on a recorded input:")
        for err in result["errors"][:5]:
            lines.append(f"    {err['case']}: {err['error']}")

    if result["moved"]:
        transitions = collections.Counter(
            (m["was"], m["now"]) for m in result["moved"])
        lines.append("  transitions:")
        for (was, now), count in transitions.most_common():
            lines.append(f"    {was:22s} -> {now:22s} {count}")
        lines.append("  examples:")
        for move in result["moved"][:8]:
            lines.append(f"    {move['case']} ({move['model']}): "
                         f"{move['was']} -> {move['now']}  [{move['file']}]")
    return "\n".join(lines)


def self_check(paths):
    """
    Prove this script is exercising the checker it claims to.

    A replay that never reaches the checker reports "no changes" and looks
    exactly like a clean run. So: score the corpus with a deliberately
    perturbed checker, and require that the replay notices. If it does not,
    the harness is measuring nothing and every result it has produced is void.

    This is the guard the recorded harness failures argue for -- a "no
    regressions" result that has not proved it exercised two different
    versions is not a result.
    """
    def perturbed(answer, evidence):
        real = grounding.check(answer, evidence)
        return {**real, "confidence": "__perturbed__"}

    result = replay(paths, check=perturbed)
    scored = result["unchanged"] + len(result["moved"])

    if scored == 0:
        raise SystemExit(
            "self-check FAILED: no records were scored at all. The corpus is "
            "empty, or every record lacks draft/evidence.")
    if not result["moved"]:
        raise SystemExit(
            f"self-check FAILED: {scored} records scored and the replay "
            "reported no change, against a checker rigged to change every "
            "verdict. This harness is not exercising grounding.check().")

    print(f"self-check passed: {len(result['moved'])} of {scored} records "
          "detected as moved against a deliberately perturbed checker")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("paths", nargs="*",
                        help="result files (default: every results/*.json)")
    parser.add_argument("--fail-on-change", action="store_true",
                        help="exit 1 if any verdict moved — the CI gate")
    parser.add_argument("--self-check", action="store_true",
                        help="prove the replay reaches the checker, then exit")
    parser.add_argument("--json", dest="out",
                        help="write the full result here")
    args = parser.parse_args(argv)

    _assert_not_shadowed()

    paths = args.paths or sorted(glob.glob(os.path.join(ROOT, "results", "*.json")))
    if not paths:
        raise SystemExit("no result files found")

    # Printed every run, not behind a flag. Both recorded harness failures
    # would have been visible in one line of this.
    for module in (grounding, contradiction):
        path, digest = _fingerprint(module)
        print(f"using {module.__name__:15s} {digest}  {path}")

    if args.self_check:
        return self_check(paths)

    result = replay(paths)
    print(summarise(result))

    if args.out:
        json.dump(result, open(args.out, "w", encoding="utf-8"), indent=2)
        print(f"wrote {args.out}")

    if result["errors"]:
        return 1
    if args.fail_on_change and result["moved"]:
        print("\nverdicts moved. If that is the intended effect of a change, "
              "read the transitions above and confirm each class is one you "
              "meant -- the first two drafts of the absence rule looked like "
              "this and were wrong.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
