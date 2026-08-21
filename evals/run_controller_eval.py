"""
Eval for the controller's diagnose -> deliver path.

tests/test_controller.py proves the watch loop and the budget behave, with the
model mocked. evals/run_eval.py proves the agent reaches the right conclusion,
through agent.ask. Nothing covered the join between them: the controller asks
its own question, in its own words, about a pod it picked itself, and then a
sink turns the answer into a message. Every one of those steps can be wrong
while both existing suites pass.

It is a different question from run_eval.py's on purpose. There the input is a
human's question; here it is the sentence the controller composes, which no
human reviews and which is the actual prompt in production.

    kubectl apply -f demo/broken-pods.yaml
    python evals/run_controller_eval.py
    python evals/run_controller_eval.py --repeat 3

What is graded is the delivered text, not the raw answer, because that is what
someone reads at 3am -- including whether it survives Slack's block limit and
whether it names the workload rather than a pod hash nobody can search for.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import controller  # noqa: E402
import grounding  # noqa: E402
import sinks  # noqa: E402
import store  # noqa: E402
from routers.k8s_pods_info import _api, _pod_status, workload_of  # noqa: E402

# Each entry is a fault demo/broken-pods.yaml creates deliberately, so the
# right answer is known before the model runs.
CASES = [
    {
        "workload": "memory-hog",
        "expect_all": [["oomkill", "out of memory", "memory limit"], ["64mi", "64 mi"]],
    },
    {
        "workload": "crasher",
        # The cause is in the logs. Stopping at CrashLoopBackOff is the failure.
        "expect_all": [["db:5432", "database", "connection refused"]],
    },
    {
        "workload": "bad-image",
        "expect_all": [["image"], ["this-tag-does-not-exist", "not found", "does not exist"]],
    },
    {
        "workload": "never-ready",
        # Running and broken: nothing terminated, nothing restarted. If the
        # controller cannot explain this one it is reading only the status.
        "expect_all": [["readiness", "probe", "not ready"]],
    },
    {
        "workload": "nightly-sync",
        # A CronJob pod, which is the shape this eval had no case for and the
        # one seen failing live on GKE: the controller's finding was a list of
        # tool calls to make rather than a diagnosis. The cause is one line in
        # the container's log, and the pod has terminated rather than
        # restarting, so nothing about the status can supply it.
        "expect_all": [["503", "upstream"]],
    },
]

# The tools the model can call. Named in prose but absent from tool_calls,
# they are the signature of an answer that described the investigation instead
# of performing it.
TOOL_NAMES = (
    "scan_cluster", "list_pods", "describe_pod", "get_pod_events", "get_pod_logs",
    "list_nodes", "list_deployments", "get_service_endpoints", "scan_references",
)


def tools_named_but_not_called(finding):
    """
    Tools the answer names in prose and never called itself.

    Observed live on GKE for a failing CronJob: "To find the root cause: 1.
    Check termination reason: call describe_pod... 2. Inspect logs: use
    get_pod_logs...". Every tool was available and none were used.

    A fact, not a verdict, and the rename is the whole change. As
    planned_instead_of_looking this was read as the failure itself, which
    conflated two behaviours the signal cannot separate: *wrote a plan instead
    of diagnosing*, and *diagnosed, then suggested a next step*. Both name a
    tool they did not call. Only the first is worthless in an alert.

    grade() decides which one happened, because it is the only caller that
    knows the case's root cause and can ask whether the answer already carries
    it. A finding that says "the logs show X" having called get_pod_logs does
    not match here at all, because the tool was called.
    """
    text = (finding.get("diagnosis") or "").lower()
    called = {name.lower() for name in finding.get("tool_calls", [])}
    return sorted(tool for tool in TOOL_NAMES if tool in text and tool not in called)


class CaptureSink:
    """Stands in for Slack, and keeps what would have been sent."""

    def __init__(self):
        self.sent = []

    def send(self, finding):
        self.sent.append(finding)


def find_pod(workload):
    """A real pod of this workload, as the watch would have handed it over."""
    for pod in _api().list_namespaced_pod("demo", _request_timeout=15).items:
        if (workload_of(pod) or pod.metadata.name) == workload:
            return pod
    return None


def grade(case, finding, delivered):
    """
    Grade one delivered message. Returns (ok, failures, notes).

    Notes are things worth seeing that are not failures. They exist because
    the plan detector needed somewhere to put "suggested a next step" without
    either failing the run or throwing the observation away.
    """
    failures = []
    notes = []
    text = delivered.lower()

    missing = [
        group for group in case["expect_all"]
        if not any(term in text for term in group)
    ]
    for group in missing:
        failures.append(f"missing {group}")

    # The message has to name the workload. A pod name carries a fresh hash on
    # every rollout, so an alert naming only the pod is unsearchable minutes
    # after it arrives.
    if case["workload"] not in text:
        failures.append(f"never named the workload {case['workload']!r}")

    # Against grounding.VERDICTS, not a tuple retyped here. This check used to
    # list three of the four and failed every finding that came back
    # `insufficient_evidence` -- a real verdict, meaning the diagnosis stated
    # nothing the checker could trace, which sinks.py renders like any other.
    # Found 2026-08-21 in fresh validation: crasher and bad-image both met
    # every content expectation and were scored as failures on this line
    # alone.
    if finding.get("confidence") not in grounding.VERDICTS:
        failures.append(f"no usable confidence: {finding.get('confidence')!r}")

    # The same signal, read two ways, because on its own it cannot tell a
    # plan from a postscript. With the root cause missing, naming tools it
    # never called is the GKE failure: an alert that hands you a to-do list.
    # With the root cause present, it is a suggestion after the useful part,
    # which is not what this eval is here to punish.
    described = tools_named_but_not_called(finding)
    if described and missing:
        failures.append(
            f"wrote out {', '.join(described)} as steps for the reader to take "
            "instead of calling them"
        )
    elif described:
        notes.append(
            f"suggested {', '.join(described)} as a next step, after reporting "
            "the root cause"
        )

    # Slack drops a block over 3000 characters, which means no alert at all
    # rather than a truncated one.
    if len(delivered) > 2900:
        failures.append(f"delivered text is {len(delivered)} chars, over the block limit")

    return not failures, failures, notes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", default=None, help="defaults to TRIAGE_MODEL")
    args = parser.parse_args()

    import agent as agent_module

    model = args.model or agent_module.MODEL
    passes = total = 0
    print(f"model={model}  cases={len(CASES)}  repeat={args.repeat}\n")

    for case in CASES:
        if find_pod(case["workload"]) is None:
            print(f"SKIP   {case['workload']:<24} not in the demo namespace")
            continue

        for _ in range(args.repeat):
            sink = CaptureSink()
            # A fresh store per run: the cooldown is the controller's job and
            # would otherwise suppress the second repeat of the same workload.
            watcher = controller.Controller(
                sink=sink,
                budget=controller.Budget(state=store.MemoryStore()),
                model=model,
            )

            # Re-resolved per repeat, not once per case. A nightly-sync pod
            # lives about two minutes and a repeat takes longer than that, so
            # the pod found before the first run is already collected by the
            # second -- which made the later repeats of that case a test of
            # still_there() rather than of the diagnosis.
            pod = find_pod(case["workload"])
            if pod is None:
                print(f"SKIP   {case['workload']:<24} gone between repeats")
                continue
            status = _pod_status(pod)

            # The order production uses: capture at enqueue, hand it to the
            # diagnosis. Calling diagnose(pod, status) as this file used to
            # skipped capture_evidence entirely, so the CronJob case measured
            # the behaviour from before that fix existed and would have gone
            # on reporting 0/3 however well the fix worked.
            evidence = watcher.capture_evidence(pod)

            started = time.time()
            finding = watcher.diagnose(pod, status, evidence)
            elapsed = time.time() - started
            total += 1

            if finding is None:
                print(f"FAIL   {case['workload']:<24} diagnosis returned nothing")
                continue

            sink.send(finding)
            delivered = sinks.format_text(sink.sent[0])
            ok, why, notes = grade(case, finding, delivered)
            passes += ok

            mark = "PASS" if ok else "FAIL"
            # Whether evidence was actually captured, not just attempted.
            # capture_pod_logs returns [] on any failure -- a 404, an error
            # dict, a pod with no logs yet -- and an empty capture is exactly
            # the old behaviour wearing the new code's clothes. Without this
            # printed, a run that captured nothing is indistinguishable from
            # one that captured the line the diagnosis turns on.
            # flush, for the reason run_eval.py flushes: the moment this is
            # redirected to a file, stdout is block-buffered and a run that
            # takes half an hour shows nothing until it ends -- which is
            # indistinguishable from a hang, on a suite whose whole subject is
            # a diagnosis racing a pod's lifetime.
            print(
                f"{mark:6} {case['workload']:<24} {status:<18} "
                f"{finding['confidence']:<9} {elapsed:5.1f}s  {len(delivered)}c  "
                f"evidence={'yes' if evidence else 'NONE'}",
                flush=True,
            )
            for reason in why:
                print(f"         - {reason}", flush=True)
            # Marked differently from failures on purpose: a run can be all
            # PASS and still have these, and reading them as failures is the
            # confusion this detector had built into it.
            for note in notes:
                print(f"         ~ {note}")

    # The dedup half of the path, which costs no model time to check.
    budget = controller.Budget(cooldown=1800, state=store.MemoryStore())
    pod = find_pod("memory-hog")
    if pod is not None:
        first = controller.Controller(
            sink=CaptureSink(), budget=budget, model=model
        ).enqueue(pod, _pod_status(pod))
        second = controller.Controller(
            sink=CaptureSink(), budget=budget, model=model
        ).enqueue(pod, _pod_status(pod))
        total += 1
        passes += first and not second
        print(
            f"{'PASS' if first and not second else 'FAIL':6} "
            f"{'cooldown suppresses a repeat':<24}"
        )

    rate = passes / total * 100 if total else 0
    print(f"\nscore: {passes}/{total} ({rate:.0f}%)")
    return 0 if rate >= 70 else 1


if __name__ == "__main__":
    raise SystemExit(main())
