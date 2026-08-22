"""
How often does a diagnosis ignore the log that was already in its prompt?

`run_controller_eval.py` grades the delivered message against the case's
expectations and reports a pass rate. It cannot answer this question, for two
reasons: it mixes the workloads whose cause is in the log with the ones whose
cause is in the status, and a failure there can be any of six things -- a
missing workload name, an over-long message, an unusable confidence. This
isolates one behaviour, on the only two fixtures where it is decidable.

    caffeinate -is env OLLAMA_KEEP_ALIVE=24h .venv/bin/python \
        evals/probe_evidence_read.py --context kind-aiops-test --repeat 10 \
        --json results/evidence-read.json

**It runs the production path, not a reconstruction of it.** The failure mode
this repo keeps hitting is an eval measuring a code path production does not
take -- four times now, most recently a `draft` field whose test passed
because the mocked answer contained nothing checkable. So the probe builds a
real `Controller`, calls `capture_evidence()` at the point the watch does, and
calls `diagnose()`, which composes its own question. The only deviation is a
wrapper around `agent.ask` that forces `evidence=True`, because `diagnose()`
keeps `result["answer"]` and this needs `result["draft"]` as well -- and that
wrapper widens the return value without touching the arguments.

**It is scored against the draft.** `answer` has been through `verify()`,
which rewrites unsupported values in the prose, and `annotate()`, which
appends an audit footer quoting them back. Either can move a phrase this
instrument looks for, in either direction. Both texts are recorded and both
are scored, so the gap between them is visible rather than assumed.

**A run whose capture came back empty is void, not failed.** capture_pod_logs
returns [] on a 404, on an error dict and on a pod that has not logged yet,
and `nightly-sync` pods are collected within about two minutes. Scoring "did
not mention 5432" against a run that was never shown 5432 measures the
harness. `evidence_carries()` decides, per run, against the fixture's real log
line.
"""

import argparse
import datetime as dt
import functools
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402
import controller  # noqa: E402
import routers.k8s_pods_info as k8s  # noqa: E402
import sinks  # noqa: E402
import store  # noqa: E402

import evidence_read  # noqa: E402
from host_state import low_power_mode  # noqa: E402
from run_controller_eval import CaptureSink, find_pod  # noqa: E402
from summarise import wilson  # noqa: E402

# The two fixtures whose root cause is in the log and nowhere else. See
# evidence_read.FACTS for why the other four are not here.
WORKLOADS = tuple(evidence_read.FACTS)


def capturing_ask(sink):
    """
    `agent.ask` with `evidence=True` forced, keeping the full result.

    diagnose() builds its finding from six named keys, so the extra ones ride
    along unread. functools.wraps because agent.TOOLS is introspected by name
    elsewhere in this codebase and an unwrapped wrapper has bitten it before.
    """
    original = agent.ask

    @functools.wraps(original)
    def ask(question, **kwargs):
        kwargs["evidence"] = True
        result = original(question, **kwargs)
        sink.append(result)
        return result

    return ask, original


def score(workload, result, evidence, finding):
    """One run's record, with the void check first."""
    missing = evidence_read.evidence_carries(workload, evidence)
    draft = evidence_read.read(workload, result.get("draft") or "")
    published = evidence_read.read(workload, (finding or {}).get("diagnosis") or "")
    return {
        "workload": workload,
        # Void beats every other verdict: with the line absent from the
        # prompt, "did not carry the line" is not a finding about the model.
        "void": bool(missing) or not evidence,
        "missing_from_evidence": missing,
        "draft": draft,
        "published": published,
        # The two texts disagreeing means verify() or annotate() moved a
        # phrase the instrument reads. Recorded because it would otherwise be
        # invisible, and it decides which number is the honest one.
        "rewritten": draft["matched"] != published["matched"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", default=None, help="defaults to TRIAGE_MODEL")
    parser.add_argument("--workload", action="append", choices=WORKLOADS,
                        help="default: both")
    parser.add_argument("--json", help="write per-run records to this file")
    parser.add_argument("--context", help="kubeconfig context to measure against")
    args = parser.parse_args()

    if args.context:
        k8s.use_context(args.context)

    context = k8s.active_context()
    pods = k8s.list_pods(namespace="demo")
    if isinstance(pods, dict) and pods.get("error"):
        print(f"cluster {context!r} cannot answer for namespace 'demo': "
              f"{pods['error']}", file=sys.stderr)
        return 2

    model = args.model or agent.MODEL
    workloads = args.workload or list(WORKLOADS)
    print(f"model={model}  workloads={','.join(workloads)}  repeat={args.repeat}  "
          f"context={context}  demo pods={len(pods)}\n", flush=True)

    records = []
    for workload in workloads:
        for index in range(args.repeat):
            # Re-resolved per repeat: a nightly-sync pod lives about two
            # minutes and a repeat takes longer, so a pod found once per case
            # would make every later repeat a test of still_there().
            pod = find_pod(workload)
            if pod is None:
                print(f"SKIP   {workload:<14} no pod right now", flush=True)
                continue

            status = k8s._pod_status(pod)
            captured = []
            agent.ask, original = capturing_ask(captured)
            try:
                watcher = controller.Controller(
                    sink=CaptureSink(),
                    budget=controller.Budget(state=store.MemoryStore()),
                    model=model,
                )
                evidence = watcher.capture_evidence(pod)
                started = time.time()
                finding = watcher.diagnose(pod, status, evidence)
                elapsed = time.time() - started
            finally:
                agent.ask = original

            if finding is None or not captured:
                print(f"VOID   {workload:<14} no diagnosis "
                      f"({'pod gone' if finding is None else 'ask never ran'})",
                      flush=True)
                continue

            result = captured[-1]
            record = score(workload, result, evidence, finding)
            record.update({
                "run": index,
                "pod": pod.metadata.name,
                "status": status,
                "context": context,
                "model": model,
                "elapsed_s": round(elapsed, 1),
                "confidence": finding.get("confidence"),
                "tool_calls": finding.get("tool_calls"),
                "nudges": result.get("nudges"),
                "timing": result.get("timing"),
                "low_power_mode": low_power_mode(),
                "at": dt.datetime.now().isoformat(timespec="seconds"),
                # The texts themselves, because every question asked of this
                # set after the first look will be a question about wording.
                "evidence_text": evidence_read.evidence_text(evidence),
                "draft_text": result.get("draft"),
                "answer_text": finding.get("diagnosis"),
            })
            records.append(record)

            draft = record["draft"]
            mark = "VOID" if record["void"] else ("READ" if draft["read"] else "IGNORED")
            slept = (result.get("timing") or {}).get("slept_ms") or 0.0
            print(
                f"{mark:6} {workload:<14} {record['confidence']:<20} "
                f"{elapsed:5.1f}s  facts={','.join(draft['matched']) or '-':<28} "
                f"decoys={','.join(draft['decoys']) or '-'}"
                + (f"  [host asleep {slept / 1000:.0f}s]" if slept > 1000 else ""),
                flush=True,
            )
            if record["void"]:
                print(f"         - evidence lacks {record['missing_from_evidence']}",
                      flush=True)
            if record["rewritten"]:
                print(f"         ~ published text carries "
                      f"{record['published']['matched']} instead", flush=True)

    report(records)

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(records, handle, indent=2, default=str)
        print(f"\nwrote {len(records)} records to {args.json}")

    return 0


def report(records):
    """Rates per workload, with the void runs held out of the denominator."""
    print()
    for workload in WORKLOADS:
        rows = [r for r in records if r["workload"] == workload]
        if not rows:
            continue
        void = [r for r in rows if r["void"]]
        live = [r for r in rows if not r["void"]]
        if not live:
            print(f"{workload:<14} 0 measurable runs of {len(rows)}")
            continue

        did_read = sum(r["draft"]["read"] for r in live)
        low, high = wilson(did_read, len(live))
        complete = sum(r["draft"]["complete"] for r in live)
        status_only = sum(r["draft"]["status_only"] for r in live)
        print(f"{workload:<14} read {did_read}/{len(live)} "
              f"({did_read / len(live) * 100:.0f}%, 95% CI [{low:.0f}-{high:.0f}])  "
              f"complete {complete}/{len(live)}  "
              f"status-only {status_only}/{len(live)}  void {len(void)}")

        for fact in evidence_read.FACTS[workload]:
            hit = sum(bool(r["draft"]["facts"].get(fact.name)) for r in live)
            print(f"    {fact.name:<12} {hit}/{len(live)}")


if __name__ == "__main__":
    raise SystemExit(main())
