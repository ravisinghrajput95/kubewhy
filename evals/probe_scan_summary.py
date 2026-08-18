"""
Why does `cluster_wide_scan` drop workloads from its own summary?

`run_eval.py` records what the model answered and which tools it called, and
that is enough to score a run and not enough to explain this one. The failure
is a complete tool result being summarised incompletely: on 2026-08-17
`scan_cluster` returned eight failing workloads and the answer listed six.
Nothing was missed by the tools and nothing was invented, so the only place
the two lost entries can be studied is between the tool result and the answer
-- and the tool result is the one thing no eval record keeps.

So this keeps it. One case, many repeats, and for every run the full text of
every `scan_cluster` result beside the full text of the answer, plus which of
the returned workloads the answer named. The question it is built to answer
is whether the drops cluster by **position** in the tool's output, by how
many **entries** that output had, or by **fault type** -- three different
mechanisms wanting three different fixes, and n=1 can distinguish none of
them.

    caffeinate -is env OLLAMA_KEEP_ALIVE=24h .venv/bin/python \
        evals/probe_scan_summary.py --context kind-triage-demo --repeat 20 \
        --json results/scan-summary-probe.json

Analysis lives in `analyse_scan_summary.py`, deliberately separate: this file
costs an hour of model time to run and the questions asked of its output will
change after the first look at it.
"""

import argparse
import datetime as dt
import functools
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402
import routers.k8s_pods_info as k8s  # noqa: E402
from cases import CASES  # noqa: E402
from host_state import low_power_mode  # noqa: E402

CASE = "cluster_wide_scan"


def shuffled(seed, real=None):
    """
    Wrap `scan_cluster` so it returns the same workloads in a different order.

    Position in the tool's output and workload identity are perfectly
    confounded on the demo fixtures: `scan_cluster` sorts by blast radius and
    then by name, so `log-shipper` is at index 3 on every single run. A drop
    rate of 7/19 for that entry is therefore also a drop rate of 7/19 for that
    index, and nothing in a live run can say which of the two the model is
    responding to.

    Permuting the result per run breaks the tie by construction, and does it
    without touching a single value the model reads -- same keys, same
    statuses, same example pods, same count. If the drops follow index 3, it
    is position. If they follow `log-shipper`, it is the entry.

    `_truncated` is held at the end because it is a message about the list
    rather than a member of it, and the seed is recorded so a run can be
    replayed exactly.

    `functools.wraps` is load-bearing rather than tidiness. Ollama builds the
    tool schema by introspecting the callables in `agent.TOOLS` -- name,
    signature and docstring -- so an unwrapped closure hands the model a tool
    called `wrapper` taking `**kwargs`, with no description. Measured: 12 runs
    that called no tool at all and returned an empty answer in 4-9s. A probe
    that changes the tool definition is not measuring the same agent.
    """
    real = real or agent.TOOLS["scan_cluster"]
    rng = random.Random(seed)

    @functools.wraps(real)
    def wrapper(**kwargs):
        result = real(**kwargs)
        if not isinstance(result, dict) or "error" in result or "result" in result:
            return result
        keys = [k for k in result if k != "_truncated"]
        rng.shuffle(keys)
        out = {k: result[k] for k in keys}
        if "_truncated" in result:
            out["_truncated"] = result["_truncated"]
        return out

    return wrapper


def entries_of(result_text):
    """
    The workloads a `scan_cluster` result reported, in the order it reported
    them.

    Order matters here and is not incidental: `scan_cluster` sorts by blast
    radius before truncating, so position in the result is a property the
    model sees, and "the drops were adjacent" is one of the three hypotheses
    this probe exists to separate. dict preserves insertion order and
    json.loads preserves it from the wire, so the index below is the index the
    model read.

    Returns [] for an error or a bare {"result": ...} message -- neither is a
    list of workloads, and treating either as an empty one would silently
    score a run that never got a list as a run that dropped everything.
    """
    try:
        parsed = json.loads(result_text)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, dict) or "error" in parsed or "result" in parsed:
        return []

    entries = []
    for index, (key, value) in enumerate(parsed.items()):
        if key == "_truncated" or not isinstance(value, dict):
            continue
        namespace, _, rest = key.partition("/")
        workload, _, fault_suffix = rest.partition(":")
        entries.append({
            "key": key,
            "namespace": namespace,
            "workload": workload,
            "position": index,
            "pods": value.get("pods"),
            "status": value.get("status"),
            # `fault` is only present when it adds something over `status`;
            # the suffix is only present when one workload carries two faults.
            "fault": value.get("fault") or fault_suffix or value.get("status"),
            "example": value.get("example"),
        })
    return entries


def named_in(answer, entry):
    """
    Whether the answer mentions this workload at all, by any of the names the
    tool gave the model for it.

    Deliberately generous. The claim under investigation is that entries
    vanish from the summary entirely, so anything that shows the model carried
    the entry forward counts as carried -- including naming the example pod
    instead of its owner, which is a worse answer and not a dropped one. A
    strict check here would fold two different defects into one number.
    """
    lowered = answer.lower()
    for form in (entry["workload"], entry["key"], entry["example"]):
        if form and form.lower() in lowered:
            return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=agent.MODEL)
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--json", help="write per-run records to this file")
    parser.add_argument("--context", help="kubeconfig context to measure against")
    parser.add_argument(
        "--shuffle", action="store_true",
        help="permute scan_cluster's entry order per run, to separate position "
             "from workload identity (see shuffled())",
    )
    parser.add_argument("--seed", type=int, default=0, help="base seed for --shuffle")
    args = parser.parse_args()

    case = next(c for c in CASES if c["name"] == CASE)

    if args.context:
        k8s.use_context(args.context)

    context = k8s.active_context()
    pods = k8s.list_pods(namespace="demo")
    if isinstance(pods, dict) and pods.get("error"):
        print(f"cluster {context!r} cannot answer for 'demo': {pods['error']}",
              file=sys.stderr)
        return 2

    # What the tool says right now, before any model time is spent. The
    # 2026-08-17 failure was only interpretable because someone called
    # scan_cluster by hand against the same cluster minutes later; recording
    # it up front makes that comparison part of the measurement instead of a
    # thing to remember to do afterwards.
    baseline = k8s.scan_cluster(only_unhealthy=True)

    print(f"model={args.model}  case={CASE}  repeat={args.repeat}  "
          f"context={context}  demo pods={len(pods)}")
    print(f"scan_cluster right now: {len(entries_of(json.dumps(baseline)))} workloads\n",
          flush=True)

    real_scan = agent.TOOLS["scan_cluster"]
    records = []
    for run_index in range(args.repeat):
        seed = args.seed + run_index
        agent.TOOLS["scan_cluster"] = (
            shuffled(seed, real_scan) if args.shuffle else real_scan
        )
        began_at = dt.datetime.now().isoformat(timespec="seconds")
        started = time.time()
        calls = []
        answer_event = None

        try:
            for event in agent.stream(case["question"], model=args.model):
                if event["type"] == "tool_result":
                    calls.append(event)
                elif event["type"] == "answer":
                    answer_event = event
        except ConnectionError as exc:
            print(f"\nollama is unreachable: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 -- a failed run is data too
            answer_event = {"answer": f"ERROR: {exc}", "tool_calls": [],
                            "confidence": "ungrounded", "nudges": 0}

        answer = answer_event.get("answer", "")
        scans = [
            {
                "arguments": next(
                    (t["arguments"] for t in answer_event.get("tool_calls", [])
                     if t["name"] == "scan_cluster"), {}
                ),
                "result": call["result"],
                "entries": entries_of(call["result"]),
            }
            for call in calls if call["name"] == "scan_cluster"
        ]

        # The reference set is the last scan_cluster result that actually
        # returned workloads. A run that scans, drills in, then scans again
        # has been shown two lists, and the one it was summarising when it
        # stopped is the last one.
        listing = next((s for s in reversed(scans) if s["entries"]), None)
        returned = listing["entries"] if listing else []
        dropped = [e for e in returned if not named_in(answer, e)]

        # A run that exhausted MAX_ROUNDS never wrote a summary at all, so
        # every entry reads as dropped and the run would contribute one
        # phantom drop to every position and every fault class at once. It is
        # a real failure and a different one; recorded, flagged, and excluded
        # from the drop marginals by the analysis.
        gave_up = answer.startswith("Gave up after")

        record = {
            "run": run_index + 1,
            "case": CASE,
            "shuffled": args.shuffle,
            "seed": seed if args.shuffle else None,
            "model": args.model,
            "context": k8s.active_context(),
            "started_at": began_at,
            "seconds": round(time.time() - started, 1),
            # See host_state.low_power_mode: a throttled host halves the
            # model's speed with every timer in the loop still honest.
            "low_power_mode": low_power_mode(),
            "confidence": answer_event.get("confidence"),
            "nudges": answer_event.get("nudges", 0),
            "timing": answer_event.get("timing"),
            "tools": [t["name"] for t in answer_event.get("tool_calls", [])],
            "arguments": [t.get("arguments", {})
                          for t in answer_event.get("tool_calls", [])],
            "answer": answer,
            # Every scan_cluster result in full, not just the one analysed.
            # A second scan returning a different list would itself be the
            # explanation, and only the raw text can show that.
            "scans": scans,
            "returned": returned,
            "returned_count": len(returned),
            "dropped": dropped,
            "dropped_count": len(dropped),
            "complete": bool(returned) and not dropped,
            "gave_up": gave_up,
        }
        records.append(record)

        if args.json:
            with open(args.json, "w") as fh:
                json.dump(records, fh, indent=1)

        slept = (record["timing"] or {}).get("slept_ms") or 0.0
        mark = "GAVE" if gave_up else ("OK  " if record["complete"] else "DROP")
        print(
            f"  r{run_index + 1:<3} {mark} "
            f"{len(returned) - len(dropped)}/{len(returned)} named  "
            f"{record['seconds']:6.1f}s"
            + (f"  [host asleep {slept / 1000:.0f}s]" if slept > 1000 else ""),
            flush=True,
        )
        for entry in dropped:
            print(f"       - dropped {entry['key']} "
                  f"(position {entry['position']}, {entry['fault']})", flush=True)

    complete = sum(r["complete"] for r in records)
    print(f"\ncomplete summaries: {complete}/{len(records)}")
    if args.json:
        print(f"wrote {len(records)} runs to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
