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
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
from cases import CASES  # noqa: E402
from ollama_state import resident  # noqa: E402


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
    parser.add_argument("--json", help="write per-run results to this file")
    args = parser.parse_args()

    cases = [c for c in CASES if not args.case or c["name"] == args.case]
    if not cases:
        print(f"no case named {args.case!r}", file=sys.stderr)
        return 2

    print(f"model={args.model}  cases={len(cases)}  repeat={args.repeat}\n")

    total_passes = total_runs = 0
    ungrounded = 0
    records = []
    # Accumulated across rounds rather than printed as we go: repeat-major
    # ordering means no case is finished until the final round.
    stats = {
        c["name"]: {"passes": 0, "runs": 0, "elapsed": 0.0, "reasons": []}
        for c in cases
    }

    # Repeat-major, not case-major, and the ordering is the whole point.
    # An interrupted run is the normal outcome here rather than the
    # exception: a full set takes over an hour, and the defect this file
    # exists to measure is one that hangs for thousands of seconds. What an
    # interruption leaves behind is therefore a design decision.
    #
    # Case-major leaves a few cases complete and the rest never run, which is
    # not a sample of the suite -- it is a complete measurement of whichever
    # cases happened to be first. Measured on 2026-08-15: the machine shut
    # down 61 runs in and four of the ten cases had never run once, so the
    # suite-wide number was unusable while the early cases were oversampled.
    # Repeat-major would have left six rounds of all ten.
    for round_index in range(args.repeat):
        for case in cases:
            passes = 0
            elapsed = 0.0
            reasons = []
            was_resident = resident(args.model)
            # Wall clock and machine load at the moment the run began. Without
            # these a stall can only be described, never attributed: the first
            # set of numbers showed slow runs arriving in adjacent pairs on a
            # model that was resident throughout, which rules out the loader
            # and leaves contention as the obvious next suspect -- and nothing
            # recorded said what else the machine was doing at the time.
            began_at = dt.datetime.now().isoformat(timespec="seconds")
            load_before = os.getloadavg()[0]
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

            records.append({
                "case": case["name"],
                "model": args.model,
                "passed": bool(ok),
                "seconds": round(time.time() - started, 1),
                "confidence": result.get("confidence"),
                # Whether the weights were already loaded when this run began.
                # A stall on a run that had to load them is a loader problem,
                # not a model one, and the two have been indistinguishable in
                # every latency figure this project has published so far.
                "model_resident": was_resident,
                "started_at": began_at,
                # 1-minute load average, sampled before and after. A run that
                # is slow because sixteen other things were running is not
                # telling you anything about the agent.
                "load_before": round(load_before, 2),
                "load_after": round(os.getloadavg()[0], 2),
                # What was flagged, not just that something was: without this
                # a rise in the unverified count cannot be told apart from the
                # checker getting stricter.
                "unverified": result.get("unverified", []),
                "tools": [c["name"] for c in result.get("tool_calls", [])],
                "failures": why,
                # Where the wall clock went, split model against tools, with
                # the per-round breakdown. `seconds` above can only say a run
                # took 2217s; this says whether one round hung or every round
                # was slow, which is the distinction that killed the last two
                # hypotheses for lack of evidence.
                "timing": result.get("timing"),
            })

            # Written after every run, not at the end. A full set is over an
            # hour, and the defect this file exists to measure is a run that
            # hangs for 1013s -- so the interrupted run is the likely one, and
            # holding sixty results in memory until then loses exactly the
            # evidence that was worth collecting. ab_prompt.py already does
            # this; run_eval.py should not be the one that throws it away.
            if args.json:
                with open(args.json, "w") as fh:
                    json.dump(records, fh, indent=1)

            total_passes += passes
            total_runs += 1
            entry = stats[case["name"]]
            entry["passes"] += passes
            entry["runs"] += 1
            entry["elapsed"] += elapsed
            entry["reasons"] += reasons

            # One line per run rather than one per case: with repeat-major
            # there is nothing to summarise until the end, and an hour of
            # silence is indistinguishable from a hang -- which is the exact
            # failure this suite is trying to characterise.
            # A nap is printed next to the run it happened in, because the
            # alternative is what happened for two months: a run reported as
            # 725s against a 62s median, with nothing to say the host had been
            # suspended for 548s of it. See agent._timing.
            slept = (result.get("timing") or {}).get("slept_ms") or 0.0
            print(
                f"  r{round_index + 1:<3} {case['name']:32} "
                f"{'PASS' if passes else 'FAIL':5} {elapsed:6.1f}s"
                + (f"  [host asleep {slept / 1000:.0f}s]" if slept > 1000 else "")
            )

    print()
    for case in cases:
        entry = stats[case["name"]]
        if not entry["runs"]:
            continue
        mark = (
            "PASS" if entry["passes"] == entry["runs"]
            else ("FLAKY" if entry["passes"] else "FAIL")
        )
        print(
            f"{mark:6} {case['name']:32} {entry['passes']}/{entry['runs']}  "
            f"{entry['elapsed'] / entry['runs']:5.1f}s"
        )
        for reason in dict.fromkeys(entry["reasons"]):
            print(f"         - {reason}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(records, fh, indent=1)
        print(f"\nwrote {len(records)} runs to {args.json}")

    rate = total_passes / total_runs * 100 if total_runs else 0
    print(f"\nscore: {total_passes}/{total_runs} ({rate:.0f}%)")
    print(f"answers with unverified claims: {ungrounded}/{total_runs}")

    # Non-zero exit on a clear regression, so this can gate a release even
    # though it is too slow and too non-deterministic for CI.
    return 0 if rate >= 70 else 1


if __name__ == "__main__":
    raise SystemExit(main())
