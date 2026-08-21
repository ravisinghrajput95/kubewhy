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
# This directory too, so `from cases import CASES` resolves when something
# other than the shell imports this file -- the grader has tests now, and they
# do not run from evals/.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402
import routers.k8s_pods_info as k8s  # noqa: E402
from cases import CASES  # noqa: E402
from host_state import low_power_mode  # noqa: E402
from ollama_state import resident  # noqa: E402


def _satisfied(group, text):
    return any(term.lower() in text for term in group)


def grade(case, result):
    """
    Return (passed, [reasons it failed], [notes]).

    `forbid` is read against whether the question was answered, for the same
    reason `tools_named_but_not_called` is in the controller eval: one string
    match cannot tell *answered about the wrong thing* from *answered, then
    mentioned another thing*, and only the case knows which of those it is
    looking at.

    Measured on `healthy_workload_not_substituted`, the case that motivated
    `forbid` in the first place. Four failures were recorded with their answer
    text -- 2 of 30 in a replay probe, 2 of 20 live -- and all four read like
    this:

        The healthy-web deployment in demo is running normally with 2 ready
        replicas. No issues detected. Other deployments in demo (like
        bad-image, memory-hog) are unhealthy and may require investigation.

    That is the correct answer with a true aside, and it was being scored as
    the substitution the case exists to catch. Nothing in the run was wrong;
    the grader was. A substitution failing to deliver the verdict still fails,
    because `answered` is false there.

    The 2026-08-17 baseline's 2 failures on this case are unverified: they
    carry the same two reasons, `memory-hog` and `bad-image`, and predate the
    run that keeps answer text, so there is nothing left to read.

    Notes are printed and recorded, never scored -- an aside is worth seeing,
    since one that grew into a diagnosis of the neighbour would be a real
    failure and this is the only place it would show up first.
    """
    answer = result["answer"].lower()
    called = [c["name"] for c in result["tool_calls"]]
    failures = []
    notes = []

    wanted = "expect_any" in case or "expect_all" in case
    missing = []

    if "expect_any" in case and not _satisfied(case["expect_any"], answer):
        missing.append(f"none of {case['expect_any']} in answer")

    for group in case.get("expect_all", []):
        if not _satisfied(group, answer):
            missing.append(f"missing {group}")

    failures += missing

    # Answered means the case's own positive expectations were all met. A case
    # that declares none has nothing to condition on, so its forbid list stays
    # unconditional rather than silently becoming advisory.
    answered = wanted and not missing

    for term in case.get("forbid", []):
        if term.lower() not in answer:
            continue
        if answered:
            notes.append(f"named {term!r} alongside the answer")
        else:
            failures.append(f"wrongly claimed {term!r}")

    for tool in case.get("expect_tools", []):
        if tool not in called:
            failures.append(f"never called {tool}")

    for tool in case.get("forbid_tools", []):
        if tool in called:
            failures.append(f"should not have called {tool}")

    # A correct root cause wrapped around an invented figure passed every case
    # in this suite until 2026-08-19, because nothing here read the grounding
    # verdict. Observed live the day before, all three inside otherwise
    # correct diagnoses: 512Mi for a container measured at 64Mi, "503 Service
    # Unavailable" for a connection that was refused, and "exit 137 means the
    # OOM killer" for a pod with no memory limit at all.
    #
    # Opt-in per case rather than suite-wide, so the ten original cases keep
    # measuring what they measured and stay comparable to every published
    # number. The unverified claims are recorded as a note either way, because
    # a fabrication next to a right answer is worth seeing even where it is
    # not being scored.
    unverified = result.get("unverified") or []
    if unverified:
        if case.get("require_grounded"):
            failures.append(f"unverified claims: {unverified}")
        else:
            notes.append(f"stated {unverified} with no measurement behind it")

    return not failures, failures, notes


def preflight(namespace="demo"):
    """
    Which cluster this is about to measure, and whether the fixtures are on it.

    Both halves were learned the same way. A run started against the kind
    cluster on 2026-08-17 and, one case in, `current-context` moved to a GKE
    cluster somebody else had just created; every tool call after that
    answered honestly about an empty cluster, and the model was left saying
    "the namespace demo does not exist". Unattended, that is 100 runs and 90
    minutes producing a score of zero that looks exactly like the agent having
    catastrophically regressed.

    The context is ambient state that anything on the machine can change --
    `kind create`, `gcloud container clusters get-credentials`, another
    terminal -- so an eval that reads it without recording it cannot say what
    it measured. Returns the context name to store beside the results.
    """
    context = k8s.active_context()
    pods = k8s.list_pods(namespace=namespace)

    if isinstance(pods, dict) and pods.get("error"):
        print(
            f"cluster {context!r} cannot answer for namespace {namespace!r}: "
            f"{pods['error']}\n"
            "Point --context at the cluster holding demo/broken-pods.yaml.",
            file=sys.stderr,
        )
        return None, context

    return len(pods), context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=agent.MODEL)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--case", help="run only this case by name")
    parser.add_argument("--json", help="write per-run results to this file")
    parser.add_argument(
        "--context",
        help="kubeconfig context to measure against; defaults to current-context, "
             "which anything on the machine is free to change mid-run",
    )
    args = parser.parse_args()

    cases = [c for c in CASES if not args.case or c["name"] == args.case]
    if not cases:
        print(f"no case named {args.case!r}", file=sys.stderr)
        return 2

    if args.context:
        k8s.use_context(args.context)

    pods, context = preflight()
    if pods is None:
        return 2

    print(
        f"model={args.model}  cases={len(cases)}  repeat={args.repeat}  "
        f"context={context}  demo pods={pods}\n"
    )

    total_passes = total_runs = 0
    ungrounded = 0
    # The metric that matters: a right answer resting entirely on measurement.
    # `score` alone cannot separate that from a right answer carrying an
    # invented figure, and those are different products.
    clean = 0
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
                result = agent.ask(case["question"], model=args.model,
                                   evidence=True)
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

            ok, why, notes = grade(case, result)
            passes += ok
            reasons += why
            if result.get("confidence") != "grounded":
                ungrounded += 1
            if ok and not (result.get("unverified") or []):
                clean += 1

            records.append({
                "case": case["name"],
                "model": args.model,
                # Which cluster this run asked about -- the context the client
                # is bound to, not whatever current-context says now, since
                # the two stop agreeing the moment anything else touches the
                # kubeconfig. On the record rather than in a header because
                # the record is the unit anyone reads a year later, and it is
                # a cached lookup.
                "context": k8s.active_context(),
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
                # Whether macOS was throttling the machine. Measured
                # 2026-08-18: a set's median run time doubled mid-run with
                # every second attributed to model_ms, tools at 0.03s,
                # slept_ms zero and load at 0.63 -- the battery had drained
                # and Low Power Mode had switched itself on. Low Power Mode
                # throttles the GPU, so a local model halves in speed while
                # every timer stays honest and the host looks idle. See
                # host_state.low_power_mode.
                "low_power_mode": low_power_mode(),
                # What was flagged, not just that something was: without this
                # a rise in the unverified count cannot be told apart from the
                # checker getting stricter.
                "unverified": result.get("unverified", []),
                "tools": [c["name"] for c in result.get("tool_calls", [])],
                # The arguments too, because for several tools the name is not
                # the behaviour. scan_cluster() and scan_cluster(workload='x')
                # return different things -- the second reports one workload
                # whether or not it is broken -- and the 2026-08-17 baseline
                # recorded only the name, so the runs that answered about the
                # right workload could not be told from the ones that asked
                # the right question. Same for namespace on every pod tool.
                "arguments": [
                    c.get("arguments", {}) for c in result.get("tool_calls", [])
                ],
                # What the tools returned, not only what they were asked. This
                # is the record's only irreplaceable field: everything else
                # here can be recomputed from a re-run, and this cannot,
                # because the cluster has moved on and the model answers
                # differently the second time.
                #
                # It exists because of a question that could not be answered
                # on 2026-08-21. grounding.py was changed to stop treating a
                # hedged status as a fabrication, and the obvious check --
                # re-score the existing sets under both versions and compare
                # -- was impossible, since no set held the tool output the
                # checker reads. The comparison had to be run against
                # probe_scan_summary.py's records instead, which cover one
                # case. With this field any past set can be re-scored offline,
                # for free, against any future checker.
                #
                # grounding.records() shape, so a replay is
                # grounding.check(record["answer"], record["evidence"])
                # verbatim -- same ids, same ordering, prefetched entries in
                # the same places the live check saw them.
                "evidence": result.get("evidence", []),
                # The answer as the checker read it. `answer` below is the
                # published text, after unsupported values were rewritten and
                # the audit markers added, so it is the right field to read
                # and the wrong one to re-check: replaying check() against it
                # scored 3 of 5 live runs differently on 2026-08-21, since the
                # audit footer contributes digits of its own.
                "draft": result.get("draft"),
                # The answer itself, not just the strings it was missing. A
                # failure recorded as ["missing ['memory-hog']"] cannot say
                # whether the model looked and found nothing, described a
                # different workload, or wrote a plan -- and those want three
                # different fixes. Re-running to find out costs another hour
                # and gets a different answer, this being a model.
                "answer": result.get("answer", ""),
                # How many times the run was sent back for naming a tool it
                # never called. A run that reached get_pod_logs on its own and
                # one that had to be told are the same trace without this.
                "nudges": result.get("nudges", 0),
                # How many times a deterministic policy sent the run back for
                # evidence the status block provably does not contain. Without
                # it, a run that collected events on its own cannot be told
                # from one that had to be made to -- and the first 32-run set
                # after the policy shipped could not say whether it had ever
                # fired.
                "policies": result.get("policies", 0),
                # How many times the run was sent back for leaving workloads
                # out of its own summary. Beside nudges and policies for the
                # same reason they are there: a run that listed everything
                # first time reads identically to one that was asked twice.
                "coverage": result.get("coverage", 0),
                "failures": why,
                # Not failures, and not noise either: an answer that names a
                # broken neighbour beside a correct verdict is one edit away
                # from the substitution this suite is watching for, and a run
                # that only prints its failures cannot show that drift.
                "notes": notes,
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
            # flush, because stdout is block-buffered the moment this is
            # redirected to a file -- which is how a long run is always
            # started. The per-run line exists so an hour of silence can be
            # told from the hang this suite measures, and a 4KB buffer gives
            # exactly that silence back.
            print(
                f"  r{round_index + 1:<3} {case['name']:32} "
                f"{'PASS' if passes else 'FAIL':5} {elapsed:6.1f}s"
                + (f"  [host asleep {slept / 1000:.0f}s]" if slept > 1000 else ""),
                flush=True,
            )
            for note in notes:
                # `~` rather than `-`, matching the controller eval: a reader
                # scanning a hundred lines needs to see at a glance that this
                # one did not count against the score.
                print(f"       ~ {note}", flush=True)

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
    print(
        f"fully grounded and correct: {clean}/{total_runs} "
        f"({clean / total_runs * 100:.0f}%)" if total_runs else ""
    )
    print(f"answers not fully grounded: {ungrounded}/{total_runs}")

    # Non-zero exit on a clear regression, so this can gate a release even
    # though it is too slow and too non-deterministic for CI.
    return 0 if rate >= 70 else 1


if __name__ == "__main__":
    raise SystemExit(main())
