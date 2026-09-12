"""
Where a run's wall clock actually goes, over the recorded corpus.

Every latency figure this project publishes is a report. None of them says
what to *change*: "median 54s" does not distinguish a run that is slow because
it made eight model calls from one that made two slow ones. `tool_ms` is
already known to be ~0.5% of runtime, so the Kubernetes calls are not the
cost -- which leaves round count and per-round cost, and those are two
different levers with two different fixes.

This reads `timing.rounds`, `timing.round_ms`, `nudges` and `policies` off the
records in `results/` and reports:

  * wall clock as a function of round count -- what one more round costs
  * per-round duration by *position* -- whether later rounds cost more than
    earlier ones, which is prompt growth rather than round count
  * how often the re-ask mechanisms fire, and what they cost once round count
    is controlled for

**The arm filter is not optional, and measuring is what showed why.** The
first version of this analysis pooled every qwen3 record and produced a table
where six-round runs were *faster* than five-round ones. 42 of those 72 runs
were `think: False`, an arm measured at roughly 7x faster; pooling the two
arms had buried a 7x variable inside a 1.2x effect. `--think` defaults to
`on` and the tool refuses to pool unless asked, because a mixed arm does not
announce itself -- it just returns a plausible table.

Records written before the `think` field existed carry `None` and are
excluded from both arms rather than guessed at.

    python evals/round_budget.py                  # think=on, qwen3
    python evals/round_budget.py --think off
    python evals/round_budget.py --self-check     # prove the arm filter works
"""

import argparse
import glob
import json
import os
import statistics as st
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results", "*.json")


def load(pattern=RESULTS):
    """Every recorded run across every results file, as flat dicts."""
    runs = []
    for path in sorted(glob.glob(pattern)):
        try:
            loaded = json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        rows = loaded if isinstance(loaded, list) else (
            loaded.get("runs") or loaded.get("results") or [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    row = dict(row)
                    row["_file"] = path
                    runs.append(row)
    return runs


def wall_ms(run):
    """
    Wall clock for a run. `timing.wall_ms` where it exists, and the graded
    `seconds` otherwise -- the older records predate the timing block and
    dropping them would throw away most of the corpus.
    """
    timing = run.get("timing") or {}
    value = timing.get("wall_ms")
    if value is None and isinstance(run.get("seconds"), (int, float)):
        value = run["seconds"] * 1000.0
    return value


def usable(run):
    return (isinstance(run.get("timing"), dict)
            and run["timing"].get("rounds")
            and not run.get("void")
            and wall_ms(run) is not None)


def select(runs, model="qwen3", think=True):
    """
    The runs one arm of one model produced.

    `think=None` pools the arms and is what the docstring warns about; it
    exists so the contamination can be demonstrated rather than only
    described.
    """
    chosen = [r for r in runs if usable(r) and r.get("model") == model]
    if think is not None:
        chosen = [r for r in chosen if r.get("think") is think]
    return chosen


def quantile(values, p):
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * p), len(ordered) - 1)]


def report(runs, out=sys.stdout):
    def say(line=""):
        print(line, file=out)

    wall = [wall_ms(r) for r in runs]
    rounds = [r["timing"]["rounds"] for r in runs]

    say(f"n={len(runs)}")
    say(f"wall  : median {st.median(wall) / 1000:.1f}s  "
        f"p95 {quantile(wall, 0.95) / 1000:.1f}s")
    say(f"rounds: median {st.median(rounds)}  mean {st.mean(rounds):.2f}  "
        f"max {max(rounds)}")
    say(f"        {dict(sorted(Counter(rounds).items()))}")

    say("\nwall by round count -- what one more round costs:")
    by_rounds = defaultdict(list)
    for run in runs:
        by_rounds[run["timing"]["rounds"]].append(wall_ms(run))
    previous = None
    for count in sorted(by_rounds):
        median = st.median(by_rounds[count])
        step = f"  (+{(median - previous) / 1000:5.1f}s)" if previous else ""
        say(f"  {count} rounds: {median / 1000:7.1f}s  "
            f"n={len(by_rounds[count]):<4}{step}")
        previous = median

    say("\nper-round duration by position -- prompt growth, not round count:")
    by_position = defaultdict(list)
    for run in runs:
        for index, ms in enumerate(run["timing"].get("round_ms") or []):
            by_position[index + 1].append(ms)
    for position in sorted(by_position):
        sample = by_position[position]
        if len(sample) >= 5:
            say(f"  round {position}: {st.median(sample) / 1000:6.1f}s  "
                f"n={len(sample)}")

    say("\nre-ask mechanisms:")
    say(f"  nudges  : {dict(sorted(Counter(r.get('nudges') or 0 for r in runs).items()))}")
    say(f"  policies: {dict(sorted(Counter(r.get('policies') or 0 for r in runs).items()))}")

    def reasked(run):
        return bool((run.get("nudges") or 0) or (run.get("policies") or 0))

    say("\n  wall at matched round count -- controls for the confound that a "
        "re-ask\n  fires on runs that were already taking more rounds:")
    say(f"  {'rounds':>7} {'no re-ask':>18} {'re-ask fired':>18}")
    for count in sorted(by_rounds):
        quiet = [wall_ms(r) for r in runs
                 if r["timing"]["rounds"] == count and not reasked(r)]
        loud = [wall_ms(r) for r in runs
                if r["timing"]["rounds"] == count and reasked(r)]
        left = f"{st.median(quiet) / 1000:6.1f}s (n={len(quiet)})" if quiet else "-"
        right = f"{st.median(loud) / 1000:6.1f}s (n={len(loud)})" if loud else "-"
        say(f"  {count:>7} {left:>18} {right:>18}")

    # Upper bound, and it is an upper bound twice over: it prices every
    # re-ask at the *last* round of its run, which is the most expensive
    # position, and it credits the mechanism with the whole round rather than
    # with the difference between that round and the one it replaced.
    total = sum(wall)
    charged = 0.0
    for run in runs:
        fired = (run.get("nudges") or 0) + (run.get("policies") or 0)
        durations = run["timing"].get("round_ms") or []
        if fired and durations:
            charged += sum(durations[-min(fired, len(durations)):])
    say(f"\n  upper bound on re-ask cost: {charged / 1000:.0f}s of "
        f"{total / 1000:.0f}s = {100 * charged / total:.1f}%")

    say("\n  what the re-ask buys, since the cost is only worth paying if it "
        "buys something:")
    for label, wanted in (("re-ask fired", True), ("no re-ask", False)):
        group = [r for r in runs if reasked(r) is wanted and "passed" in r]
        if group:
            passed = sum(1 for r in group if r["passed"])
            say(f"  {label:>14}: passed {passed}/{len(group)} = "
                f"{100 * passed / len(group):.0f}%   median wall "
                f"{st.median([wall_ms(r) for r in group]) / 1000:.1f}s")


def self_check(runs):
    """
    Prove the arm filter is doing something.

    The failure this guards against is the one that actually happened: a
    pooled corpus returning a table that looked fine. So build a corpus whose
    two arms differ by a factor nothing subtle could hide, and require that
    selecting one arm excludes the other. A filter that has stopped filtering
    reports the pooled median, which is a different number -- if it is not,
    this script is not selecting anything and every table it has printed is
    over an unknown mixture.
    """
    def synthetic(think, ms, n):
        return [{"model": "probe", "think": think, "passed": True,
                 "seconds": ms / 1000.0,
                 "timing": {"rounds": 3, "wall_ms": ms,
                            "round_ms": [ms / 3] * 3}} for _ in range(n)]

    corpus = synthetic(True, 60000, 20) + synthetic(False, 6000, 20)

    on = select(corpus, model="probe", think=True)
    off = select(corpus, model="probe", think=False)
    pooled = select(corpus, model="probe", think=None)

    if len(on) != 20 or len(off) != 20:
        raise SystemExit(
            f"self-check FAILED: the arm filter returned {len(on)} on and "
            f"{len(off)} off from a corpus built with 20 of each. It is not "
            "selecting on `think`.")

    on_median = st.median([wall_ms(r) for r in on])
    pooled_median = st.median([wall_ms(r) for r in pooled])
    if on_median == pooled_median:
        raise SystemExit(
            "self-check FAILED: one arm and the pooled corpus report the same "
            f"median ({on_median:.0f}ms) against arms built 10x apart. The "
            "filter is not reaching the numbers.")

    # And the exclusion has to survive the field being absent, because 618
    # records in this corpus predate it.
    legacy = select(corpus + synthetic(None, 999, 5), model="probe", think=True)
    if len(legacy) != 20:
        raise SystemExit(
            f"self-check FAILED: {len(legacy)} runs selected after adding 5 "
            "records with no `think` field. Records that predate the field "
            "must be excluded, not guessed at.")

    print(f"self-check passed: arms separate at {on_median:.0f}ms vs pooled "
          f"{pooled_median:.0f}ms, and 5 records with no `think` field were "
          "excluded from both arms")

    live = [r for r in runs if usable(r)]
    print(f"corpus: {len(runs)} records, {len(live)} usable, "
          f"{dict(Counter(r.get('think') for r in live))} by arm")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--model", default="qwen3")
    parser.add_argument(
        "--think", default="on", choices=["on", "off", "pooled"],
        help="which arm to measure; `pooled` mixes them and is wrong for "
             "anything but demonstrating that it is wrong")
    parser.add_argument("--self-check", action="store_true",
                        help="prove the arm filter works, then exit")
    args = parser.parse_args(argv)

    runs = load()
    if args.self_check:
        return self_check(runs)

    think = {"on": True, "off": False, "pooled": None}[args.think]
    chosen = select(runs, model=args.model, think=think)
    if not chosen:
        print(f"no usable runs for model={args.model} think={args.think}",
              file=sys.stderr)
        return 2

    print(f"model={args.model}  arm={args.think}"
          + ("   POOLED -- see the module docstring" if think is None else ""))
    report(chosen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
