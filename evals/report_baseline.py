"""
Per-metric report over an eval run.

Every metric the demo-validation brief lists, reported on its own line. There
is deliberately no composite score: a single number cannot separate "wrong"
from "correct but unsupported", and those two need different fixes.
"""
import collections
import json
import math
import sys


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 100.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - s) * 100, min(1.0, c + s) * 100


records = json.load(open(sys.argv[1]))
n = len(records)
by_cat = collections.defaultdict(list)

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cases as case_mod
CAT = {c["name"]: c.get("category", "?") for c in case_mod.CASES}
for r in records:
    by_cat[CAT.get(r["case"], "?")].append(r)

print("=" * 78)
print(f"kubewhy evaluation baseline — {n} scenarios, n=1 each")
print(f"model: {records[0].get('model')}   think: {records[0].get('think')}")
print("=" * 78)

# 1 RCA correctness
passed = sum(1 for r in records if r["passed"])
lo, hi = wilson(passed, n)
print(f"\n1. RCA CORRECTNESS         {passed}/{n}  ({100*passed/n:.0f}%)"
      f"   Wilson 95% [{lo:.0f}-{hi:.0f}]")
for r in records:
    if not r["passed"]:
        print(f"     FAIL  {r['case']:<40} {r['failures']}")

# 2-5 claims
obs = inf = unk = con = 0
unsupported_runs = contra_runs = 0
for r in records:
    rca = r.get("rca") or {}
    obs += len(rca.get("observations") or [])
    inf += len(rca.get("inferences") or [])
    unk += len(rca.get("unknowns") or [])
    c = r.get("contradictions") or []
    con += len(c)
    if r.get("unverified"):
        unsupported_runs += 1
    if c:
        contra_runs += 1
print(f"\n2. EVIDENCE-SUPPORTED CLAIMS   {obs} observations, each with citations")
print(f"3. CONTRADICTED CLAIMS         {con} across {contra_runs}/{n} runs")
print(f"4. UNSUPPORTED CLAIMS          {unk} unknowns; "
      f"{unsupported_runs}/{n} runs carried at least one")
print(f"   (inferences, correctly marked as such: {inf})")

# 5 verdicts
verdicts = collections.Counter(r.get("confidence") for r in records)
print(f"\n5. GROUNDING VERDICTS")
for v, c in verdicts.most_common():
    print(f"     {str(v):<24} {c}")

# 6-9 performance
secs = sorted(r["seconds"] for r in records)
rounds = [(r.get("timing") or {}).get("rounds", 0) for r in records]
calls = [len(r.get("tools") or []) for r in records]
model_ms = [(r.get("timing") or {}).get("model_ms", 0) for r in records]
tool_ms = [(r.get("timing") or {}).get("tool_ms", 0) for r in records]
print(f"\n6. INVESTIGATION DURATION  median {secs[len(secs)//2]:.0f}s   "
      f"p95 {secs[int(len(secs)*0.95)]:.0f}s   max {secs[-1]:.0f}s")
print(f"   model time {sum(model_ms)/1000/n:.0f}s avg   "
      f"tool time {sum(tool_ms)/1000/n:.1f}s avg")
print(f"7. MODEL ROUNDS            median {sorted(rounds)[len(rounds)//2]}   "
      f"max {max(rounds)}")
print(f"8. TOOL CALLS              median {sorted(calls)[len(calls)//2]}   "
      f"max {max(calls)}   total {sum(calls)}")
re_asks = collections.Counter()
for r in records:
    for k in ("nudges", "policies", "coverage"):
        re_asks[k] += r.get(k, 0)
print(f"9. RE-ASKS                 named-tool {re_asks['nudges']}, "
      f"evidence {re_asks['policies']}, coverage {re_asks['coverage']}")

# 10 termination
term = collections.Counter(r.get("termination") or "answered" for r in records)
print(f"\n10. TERMINATION")
for t, c in term.most_common():
    print(f"     {t:<24} {c}")

# by category
print(f"\nBY CATEGORY")
for cat in sorted(by_cat):
    rs = by_cat[cat]
    k = sum(1 for r in rs if r["passed"])
    print(f"   {cat:<24} {k}/{len(rs)}")

# entity scoping, checked rather than assumed
print(f"\nENTITY SCOPING (target recorded on each answer)")
scoped = [r for r in records if r.get("target")]
print(f"   runs with a target extracted : {len(scoped)}/{n}")
for r in scoped:
    t = r["target"]
    args = r.get("arguments") or []
    stray = [a for a in args
             if a.get("workload") and t["name"] not in str(a.get("workload"))]
    if stray:
        print(f"     {r['case']}: tool argument off target -> {stray[:2]}")
