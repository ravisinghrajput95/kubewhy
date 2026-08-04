"""
Runs the eval cases against a live demo cluster and a live model.

This tests the agent, not the code: whether the model actually reaches the
right root cause. It needs a cluster and Ollama running, which is why it lives
outside tests/ and never runs in CI.

    kind create cluster --name triage-demo
    kubectl apply -f demo/broken-pods.yaml
    python evals/run_eval.py
    python evals/run_eval.py --model llama3.2 --repeat 3

Because the model is non-deterministic, --repeat runs each case several times
and reports a pass rate. A single failure is noise; a low rate is a finding.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
from cases import CASES  # noqa: E402


def _satisfied(group, text):
    return any(term.lower() in text for term in group)


def grade(case, result):
    """Return (passed, [reasons it failed])."""
    answer = result["answer"].lower()
    called = [c["name"] for c in result["tool_calls"]]
    failures = []

    if "expect_any" in case and not _satisfied(case["expect_any"], answer):
        failures.append(f"none of {case['expect_any']} in answer")

    for group in case.get("expect_all", []):
        if not _satisfied(group, answer):
            failures.append(f"missing {group}")

    for term in case.get("forbid", []):
        if term.lower() in answer:
            failures.append(f"wrongly claimed {term!r}")

    for tool in case.get("expect_tools", []):
        if tool not in called:
            failures.append(f"never called {tool}")

    for tool in case.get("forbid_tools", []):
        if tool in called:
            failures.append(f"should not have called {tool}")

    return not failures, failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=agent.MODEL)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--case", help="run only this case by name")
    args = parser.parse_args()

    cases = [c for c in CASES if not args.case or c["name"] == args.case]
    if not cases:
        print(f"no case named {args.case!r}", file=sys.stderr)
        return 2

    print(f"model={args.model}  cases={len(cases)}  repeat={args.repeat}\n")

    total_passes = total_runs = 0
    ungrounded = 0
    rows = []

    for case in cases:
        passes = 0
        elapsed = 0.0
        reasons = []

        for _ in range(args.repeat):
            started = time.time()
            try:
                result = agent.ask(case["question"], model=args.model)
            except ConnectionError as exc:
                # Infrastructure, not the model. Scoring this as a failed case
                # would report a plausible-looking low score for what is
                # really "nothing ran".
                print(f"\nollama is unreachable: {exc}", file=sys.stderr)
                print("start it with `ollama serve`", file=sys.stderr)
                return 2
            except Exception as exc:
                result = {"answer": f"ERROR: {exc}", "tool_calls": [], "confidence": "ungrounded"}
            elapsed += time.time() - started

            ok, why = grade(case, result)
            passes += ok
            reasons += why
            if result.get("confidence") != "grounded":
                ungrounded += 1

        total_passes += passes
        total_runs += args.repeat
        rows.append((case["name"], passes, args.repeat, elapsed / args.repeat, reasons))

        mark = "PASS" if passes == args.repeat else ("FLAKY" if passes else "FAIL")
        print(f"{mark:6} {case['name']:32} {passes}/{args.repeat}  {elapsed/args.repeat:5.1f}s")
        for reason in dict.fromkeys(reasons):
            print(f"         - {reason}")

    rate = total_passes / total_runs * 100 if total_runs else 0
    print(f"\nscore: {total_passes}/{total_runs} ({rate:.0f}%)")
    print(f"answers with unverified claims: {ungrounded}/{total_runs}")

    # Non-zero exit on a clear regression, so this can gate a release even
    # though it is too slow and too non-deterministic for CI.
    return 0 if rate >= 70 else 1


if __name__ == "__main__":
    raise SystemExit(main())
