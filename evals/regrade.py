"""
Re-grade a recorded run against the current corpus.

The answers are the model's, unchanged, from the run named on the command line.
Only the expectations move. This exists because an expectation edited after a
run started would otherwise need 50 minutes of model time to apply, and because
re-running would also change the answers -- which makes it impossible to tell
an expectation fix from a different sample.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cases as case_mod
import run_eval

CASES = {c["name"]: c for c in case_mod.CASES}
records = json.load(open(sys.argv[1]))
out = []
changed = []

for r in records:
    case = CASES.get(r["case"])
    if not case:
        out.append(r)
        continue
    result = {
        "answer": r.get("answer", ""),
        "tool_calls": [{"name": n, "arguments": {}}
                       for n in (r.get("tools") or [])],
        "confidence": r.get("confidence"),
        "unverified": r.get("unverified", []),
        "evidence": r.get("evidence", []),
        "contradictions": r.get("contradictions", []),
    }
    ok, why, notes = run_eval.grade(case, result)
    if bool(ok) != bool(r["passed"]):
        changed.append((r["case"], r["passed"], bool(ok), why))
    r = dict(r)
    r["passed"], r["failures"], r["notes"] = bool(ok), why, notes
    out.append(r)

dest = sys.argv[2]
json.dump(out, open(dest, "w"), indent=1)
before = sum(1 for r in records if r["passed"])
after = sum(1 for r in out if r["passed"])
print(f"  re-graded {len(out)} runs: {before}/{len(records)} -> {after}/{len(out)}")
for name, was, now, why in changed:
    print(f"    {name:<44} {'PASS' if was else 'FAIL'} -> {'PASS' if now else 'FAIL'}")
    if not now:
        print(f"      {why}")
print(f"  wrote {dest}")
