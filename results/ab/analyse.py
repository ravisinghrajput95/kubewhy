"""
Apply a frozen criterion file to paired A/B records and print per-arm counts.

    python results/ab/analyse.py results/ab/defect50-criterion.py results/ab/defect50-*.json
"""
import json
import math
import sys
from importlib.machinery import SourceFileLoader


def wilson(k, n, z=1.96):
    if not n:
        return 0.0, 100.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0, c - m) * 100, min(1, c + m) * 100


def main():
    crit = SourceFileLoader("crit", sys.argv[1]).load_module()
    for path in sys.argv[2:]:
        if path.endswith("criterion.py"):
            continue
        with open(path) as fh:
            records = json.load(fh)
        print(f"\n== {path}")
        for arm in ("control", "variant"):
            rows = [r for r in records if r["arm"] == arm and not r.get("void")]
            voids = sum(1 for r in records if r["arm"] == arm and r.get("void"))
            judged = [crit.judge(r) for r in rows]
            keys = [k for k in judged[0] if isinstance(judged[0][k], bool)] if judged else []
            parts = []
            for k in keys:
                hits = sum(1 for j in judged if j[k])
                lo, hi = wilson(hits, len(judged))
                parts.append(f"{k} {hits}/{len(judged)} [{lo:.0f}-{hi:.0f}]")
            print(f"  {arm:<7} n={len(rows)} void={voids}  " + "  ".join(parts))
            for r, j in zip(rows, judged, strict=True):
                print(f"      tools={r.get('tools')} grader={'pass' if r['passed'] else 'FAIL'} "
                      f"{ {k: v for k, v in j.items() if k != 'case_grader'} }")


if __name__ == "__main__":
    main()
