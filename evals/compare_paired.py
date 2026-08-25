"""
Paired scenario-level comparison of two configurations on one corpus.

Paired because both configurations see the same scenarios: the right unit of
analysis is the scenario, not the run. Comparing 145 runs against 145 runs as
independent samples would overstate the evidence by treating five repeats of
one scenario as five independent facts about the model.
"""
import collections
import json
import math
import statistics
import sys
from math import comb


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 100.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - s) * 100, min(1.0, c + s) * 100


def sign_test(wins, losses):
    """Two-sided exact binomial on discordant scenarios (a sign test)."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def load(path):
    recs = json.load(open(path))
    by = collections.defaultdict(list)
    for r in recs:
        by[r["case"]].append(r)
    return recs, by


A_PATH, B_PATH = sys.argv[1], sys.argv[2]
A_NAME, B_NAME = sys.argv[3], sys.argv[4]
a_recs, A = load(A_PATH)
b_recs, B = load(B_PATH)
cases = sorted(set(A) & set(B))

print("=" * 78)
print(f"PAIRED COMPARISON   {A_NAME}  vs  {B_NAME}")
print(f"scenarios={len(cases)}  runs={len(a_recs)} vs {len(b_recs)}")
print("=" * 78)

# --- run-level, reported but not tested -------------------------------------
for name, recs in ((A_NAME, a_recs), (B_NAME, b_recs)):
    k = sum(1 for r in recs if r["passed"])
    lo, hi = wilson(k, len(recs))
    print(f"  {name:<14} {k}/{len(recs)} runs ({100*k/len(recs):.0f}%)  "
          f"Wilson 95% [{lo:.0f}-{hi:.0f}]")

# --- scenario-level pairing --------------------------------------------------
wins = losses = ties = 0
rows = []
for c in cases:
    ka, na = sum(r["passed"] for r in A[c]), len(A[c])
    kb, nb = sum(r["passed"] for r in B[c]), len(B[c])
    if ka > kb:
        wins += 1
    elif kb > ka:
        losses += 1
    else:
        ties += 1
    if ka != kb:
        rows.append((c, ka, na, kb, nb))

p = sign_test(wins, losses)
print(f"\n  SCENARIO-LEVEL PAIRING")
print(f"    {A_NAME} better on : {wins}")
print(f"    {B_NAME} better on : {losses}")
print(f"    identical          : {ties}")
print(f"    discordant         : {wins + losses}")
print(f"    two-sided sign test p = {p:.4f}")
verdict = ("DIFFERENT (p < 0.05)" if p < 0.05
           else "UNDETERMINED — the sample does not separate them")
print(f"    VERDICT: {verdict}")

if rows:
    print(f"\n  SCENARIOS THAT DIFFER")
    print(f"    {'scenario':<44} {A_NAME:>12} {B_NAME:>12}")
    for c, ka, na, kb, nb in sorted(rows, key=lambda r: (r[3]/r[4]) - (r[1]/r[2])):
        print(f"    {c:<44} {ka}/{na:<10} {kb}/{nb}")

# --- the other metrics, side by side ----------------------------------------
def metrics(recs):
    obs = con = unk = 0
    verdicts = collections.Counter()
    calls, rounds, secs = [], [], []
    re_asks = collections.Counter()
    for r in recs:
        rca = r.get("rca") or {}
        obs += len(rca.get("observations") or [])
        unk += len(rca.get("unknowns") or [])
        con += len(r.get("contradictions") or [])
        verdicts[r.get("confidence")] += 1
        calls.append(len(r.get("tools") or []))
        rounds.append((r.get("timing") or {}).get("rounds", 0))
        secs.append(r["seconds"])
        for k in ("nudges", "policies", "coverage"):
            re_asks[k] += r.get(k, 0)
    secs.sort()
    def pct(q):
        return secs[min(len(secs) - 1, int(len(secs) * q))]
    return {
        "obs": obs, "con": con, "unk": unk, "verdicts": verdicts,
        "calls": sum(calls) / len(calls), "rounds": statistics.median(rounds),
        "median": statistics.median(secs), "p95": pct(0.95), "p99": pct(0.99),
        "re_asks": re_asks,
    }

ma, mb = metrics(a_recs), metrics(b_recs)
print(f"\n  METRICS, REPORTED SEPARATELY")
print(f"    {'':<34}{A_NAME:>14}{B_NAME:>16}")
for label, key in (("evidence-supported claims", "obs"),
                   ("contradicted claims", "con"),
                   ("unsupported claims (unknowns)", "unk")):
    print(f"    {label:<34}{ma[key]:>14}{mb[key]:>16}")
print(f"    {'mean tool calls':<34}{ma['calls']:>14.1f}{mb['calls']:>16.1f}")
print(f"    {'median model rounds':<34}{ma['rounds']:>14}{mb['rounds']:>16}")
print(f"    {'median duration':<34}{ma['median']:>13.0f}s{mb['median']:>15.0f}s")
print(f"    {'p95 duration':<34}{ma['p95']:>13.0f}s{mb['p95']:>15.0f}s")
print(f"    {'p99 duration':<34}{ma['p99']:>13.0f}s{mb['p99']:>15.0f}s")
for k in ("nudges", "policies", "coverage"):
    print(f"    {'re-asks: ' + k:<34}{ma['re_asks'][k]:>14}{mb['re_asks'][k]:>16}")

print(f"\n  GROUNDING VERDICTS")
for v in sorted(set(ma["verdicts"]) | set(mb["verdicts"]), key=str):
    print(f"    {str(v):<34}{ma['verdicts'][v]:>14}{mb['verdicts'][v]:>16}")

# --- entity scoping ----------------------------------------------------------
def scoping(recs):
    extracted = onto = off = 0
    for r in recs:
        t = r.get("target")
        if not t or not t.get("name"):
            continue
        extracted += 1
        stray = [a for a in (r.get("arguments") or [])
                 if a.get("workload") and t["name"] not in str(a["workload"])]
        if stray:
            off += 1
        else:
            onto += 1
    return extracted, onto, off

for name, recs in ((A_NAME, a_recs), (B_NAME, b_recs)):
    e, on, off = scoping(recs)
    total = len(recs)
    print(f"\n  ENTITY SCOPING — {name}")
    print(f"    target extracted      : {e}/{total} ({100*e/total:.0f}%)")
    print(f"    stayed on target      : {on}/{e}" if e else "    n/a")
    print(f"    wrong-target rate     : {off}/{e} ({100*off/e:.1f}%)" if e else "")
