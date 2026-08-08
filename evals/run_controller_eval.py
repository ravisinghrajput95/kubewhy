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
]


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
    failures = []
    text = delivered.lower()

    for group in case["expect_all"]:
        if not any(term in text for term in group):
            failures.append(f"missing {group}")

    # The message has to name the workload. A pod name carries a fresh hash on
    # every rollout, so an alert naming only the pod is unsearchable minutes
    # after it arrives.
    if case["workload"] not in text:
        failures.append(f"never named the workload {case['workload']!r}")

    if finding.get("confidence") not in ("grounded", "partial", "ungrounded"):
        failures.append(f"no usable confidence: {finding.get('confidence')!r}")

    # Slack drops a block over 3000 characters, which means no alert at all
    # rather than a truncated one.
    if len(delivered) > 2900:
        failures.append(f"delivered text is {len(delivered)} chars, over the block limit")

    return not failures, failures


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
        pod = find_pod(case["workload"])
        if pod is None:
            print(f"SKIP   {case['workload']:<24} not in the demo namespace")
            continue

        status = _pod_status(pod)
        for _ in range(args.repeat):
            sink = CaptureSink()
            # A fresh store per run: the cooldown is the controller's job and
            # would otherwise suppress the second repeat of the same workload.
            watcher = controller.Controller(
                sink=sink,
                budget=controller.Budget(state=store.MemoryStore()),
                model=model,
            )

            started = time.time()
            finding = watcher.diagnose(pod, status)
            elapsed = time.time() - started
            total += 1

            if finding is None:
                print(f"FAIL   {case['workload']:<24} diagnosis returned nothing")
                continue

            sink.send(finding)
            delivered = sinks.format_text(sink.sent[0])
            ok, why = grade(case, finding, delivered)
            passes += ok

            mark = "PASS" if ok else "FAIL"
            print(
                f"{mark:6} {case['workload']:<24} {status:<18} "
                f"{finding['confidence']:<9} {elapsed:5.1f}s  {len(delivered)}c"
            )
            for reason in why:
                print(f"         - {reason}")

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
