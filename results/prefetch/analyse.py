"""
Read the prefetch A/B: latency off the loop's clocks, accuracy off the frozen
criterion, both per case and pooled.

    python results/prefetch/analyse.py results/prefetch/*-2026-09-16.json

Records are paired by adjacency: ab_prompt.py writes both arms of one repeat
back to back, alternating which leads. The switch is checked before anything is
counted -- a control run carrying a prefetched call, or a variant run carrying
none on a case whose target has an example pod, is a leak or a no-op and is
reported, never scored.
"""
import json
import math
import random
import statistics
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

PREFETCH_IN_CONTROL = False

crit = SourceFileLoader("crit", str(Path(__file__).with_name("criterion.py"))).load_module()


def fisher_two_sided(a, b, c, d):
    """Exact two-sided Fisher p for [[a, b], [c, d]]."""
    n1, n2, k = a + b, c + d, a + c
    n = n1 + n2

    def p(x):
        return math.comb(n1, x) * math.comb(n2, k - x) / math.comb(n, k)

    observed = p(a)
    lo, hi = max(0, k - n2), min(k, n1)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= observed * (1 + 1e-9)))


def sign_test(diffs):
    """Exact two-sided sign test on non-zero paired differences."""
    nonzero = [d for d in diffs if d]
    n, k = len(nonzero), sum(1 for d in nonzero if d > 0)
    if not n:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(k, n - k) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def permutation(diffs, trials=200_000, seed=16):
    """Two-sided sign-flip permutation p on the mean paired difference."""
    if not diffs:
        return 1.0
    rng = random.Random(seed)
    observed = abs(sum(diffs))
    hits = 0
    for _ in range(trials):
        if abs(sum(d if rng.random() < 0.5 else -d for d in diffs)) >= observed - 1e-9:
            hits += 1
    return (hits + 1) / (trials + 1)


def pairs(records):
    out = []
    for i in range(0, len(records) - 1, 2):
        one, two = records[i], records[i + 1]
        if {one["arm"], two["arm"]} != {"control", "variant"} or one["case"] != two["case"]:
            raise SystemExit(f"records {i},{i + 1} are not one pair")
        by = {one["arm"]: one, two["arm"]: two}
        if PREFETCH_IN_CONTROL:
            out.append((by["variant"], by["control"]))
        else:
            out.append((by["control"], by["variant"]))
    return out


def main(paths):
    pooled = {"off": [], "on": []}
    all_pairs = []
    print(f"{'case':<30} {'arm':<7} {'right':>5} {'looked':>6} {'grader':>6} "
          f"{'rounds med':>10} {'wall med s':>10} {'model med s':>11} {'tool med ms':>11}")
    for path in paths:
        with open(path) as fh:
            records = json.load(fh)
        case_pairs = pairs(records)
        leaks = [(c, v) for c, v in case_pairs if c.get("prefetched") or not v.get("prefetched")]
        voids = [(c, v) for c, v in case_pairs if c.get("void") or v.get("void")]
        usable = [(c, v) for c, v in case_pairs if (c, v) not in leaks and (c, v) not in voids]
        name = records[0]["case"]
        for arm, index in (("off", 0), ("on", 1)):
            rows = [p[index] for p in usable]
            judged = [crit.judge(r) for r in rows]
            for r, j in zip(rows, judged, strict=True):
                pooled[arm].append((r, j))
            looked = [j["looked"] for j in judged if j["looked"] is not None]
            timing = [r.get("timing") or {} for r in rows]
            print(f"{name[:30]:<30} {arm:<7} "
                  f"{sum(j['right'] for j in judged)}/{len(rows):<3} "
                  f"{(str(sum(looked)) + '/' + str(len(looked))) if looked else '-':>6} "
                  f"{sum(bool(r['passed']) for r in rows)}/{len(rows):<4} "
                  f"{statistics.median(t.get('rounds', 0) for t in timing) if timing else 0:>10} "
                  f"{statistics.median(t.get('wall_ms', 0) for t in timing) / 1000 if timing else 0:>10.1f} "
                  f"{statistics.median(t.get('model_ms', 0) for t in timing) / 1000 if timing else 0:>11.1f} "
                  f"{statistics.median(t.get('tool_ms', 0) for t in timing) if timing else 0:>11.0f}")
        if leaks or voids:
            print(f"  !! {len(leaks)} leaked or no-op pairs, {len(voids)} void pairs -- not scored")
        all_pairs += usable

    print(f"\n{len(all_pairs)} usable pairs")
    for key, label in (("wall_ms", "wall"), ("model_ms", "model"), ("rounds", "rounds")):
        diffs = [(v["timing"][key] - c["timing"][key]) for c, v in all_pairs]
        scale = 1000 if key.endswith("_ms") else 1
        print(f"  {label:<6} on - off: median {statistics.median(diffs) / scale:+.1f}, "
              f"mean {statistics.mean(diffs) / scale:+.1f}; "
              f"on lower in {sum(d < 0 for d in diffs)}/{len(diffs)}, higher in "
              f"{sum(d > 0 for d in diffs)}; sign p={sign_test(diffs):.4g}, "
              f"permutation p={permutation(diffs):.4g}")

    right = {arm: sum(j["right"] for _, j in pooled[arm]) for arm in pooled}
    n = {arm: len(pooled[arm]) for arm in pooled}
    p = fisher_two_sided(right["off"], n["off"] - right["off"],
                         right["on"], n["on"] - right["on"])
    print(f"\n  right: prefetch off {right['off']}/{n['off']}, "
          f"on {right['on']}/{n['on']}, Fisher p={p:.4g}")
    discordant = [(crit.judge(c)["right"], crit.judge(v)["right"]) for c, v in all_pairs]
    only_c = sum(1 for a, b in discordant if a and not b)
    only_v = sum(1 for a, b in discordant if b and not a)
    exact = sign_test([1] * only_v + [-1] * only_c)
    print(f"  paired: right with prefetch off only {only_c}, on only {only_v}, "
          f"exact McNemar p={exact:.4g}")


if __name__ == "__main__":
    # Since the prefetch went on by default, an A/B varies
    # TRIAGE_PREFETCH_TARGET=off in the variant arm and the control carries the
    # prefetch. --prefetch-arm control swaps the arms before anything is read,
    # so every table below still reads "without" against "with".
    args = sys.argv[1:]
    if args[:2] == ["--prefetch-arm", "control"]:
        PREFETCH_IN_CONTROL = True
        args = args[2:]
    main(args)
