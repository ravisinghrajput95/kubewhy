"""
Aggregates eval runs into the table published in the README.

Reports a Wilson score interval rather than a bare percentage: at n=70 a point
estimate implies precision the sample does not support, and the whole reason
this harness exists is to stop overstating what was measured.

    python evals/summarise.py results/*.json
"""

import json
import math
import statistics
import sys
from collections import defaultdict


def wilson(passes, total, z=1.96):
    """95% confidence interval for a binomial proportion, as (low, high) %."""
    if not total:
        return 0.0, 0.0

    p = passes / total
    denom = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denom
    return max(0.0, (centre - margin)) * 100, min(1.0, (centre + margin)) * 100


def main(paths):
    runs = []
    for path in paths:
        with open(path) as fh:
            runs += json.load(fh)

    if not runs:
        print("no runs found", file=sys.stderr)
        return 1

    by_model = defaultdict(list)
    for run in runs:
        by_model[run["model"]].append(run)

    print(f"{'model':<12} {'pass rate':<22} {'n':<5} {'median':<8} {'p95':<8} grounded")
    print("-" * 74)

    for model, model_runs in sorted(by_model.items(), key=lambda kv: -len(kv[1])):
        passes = sum(r["passed"] for r in model_runs)
        total = len(model_runs)
        low, high = wilson(passes, total)
        times = sorted(r["seconds"] for r in model_runs)
        p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
        grounded = sum(r["confidence"] == "grounded" for r in model_runs)

        rate = passes / total * 100
        print(
            f"{model:<12} {rate:5.0f}%  [{low:4.0f}-{high:4.0f}] 95% CI  "
            f"{total:<5} {statistics.median(times):<8.1f} {p95:<8.1f} {grounded}/{total}"
        )

    print("\nper case:")
    by_case = defaultdict(lambda: defaultdict(list))
    for run in runs:
        by_case[run["case"]][run["model"]].append(run["passed"])

    models = sorted(by_model)
    print(f"{'case':<32} " + "  ".join(f"{m:>12}" for m in models))
    for case in sorted(by_case):
        cells = []
        for model in models:
            results = by_case[case].get(model, [])
            cells.append(
                f"{sum(results)}/{len(results):<10}" if results else f"{'-':>12}"
            )
        print(f"{case:<32} " + "  ".join(f"{c:>12}" for c in cells))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
