"""
Do ten replicas failing the same way collapse into one finding?

And -- the half that matters more -- does collapsing them still leave an
unrelated incident visible? A reducer that turns eleven alerts into one has
not reduced noise, it has hidden an outage.

    kubectl apply -f demo/noise.yaml
    .venv/bin/python evals/gke_noise_check.py --repeat 3 --json results/gke-noise.json

**No model is involved and that is not a shortcut.** The collapse happens in
Controller.enqueue, which asks the budget whether this workload+fault has
already been spent on; the diagnosis only ever runs for what survives that.
So the thing being measured is a deterministic decision, and putting a model
behind it would add variance to a question that has none.

Each repeat gets a fresh MemoryStore, because the cooldown is the mechanism
under test and a shared store would make repeats 2 and 3 trivially suppress
everything.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import controller  # noqa: E402
import store  # noqa: E402
from routers.k8s_pods_info import _api, _pod_status, workload_of  # noqa: E402


class CaptureSink:
    def __init__(self):
        self.sent = []

    def send(self, finding):
        self.sent.append(finding)


def one_round(namespace):
    """Enqueue every failing pod once, and report what got through."""
    watcher = controller.Controller(
        sink=CaptureSink(), budget=controller.Budget(state=store.MemoryStore())
    )
    pods = _api().list_namespaced_pod(namespace, _request_timeout=30).items

    seen, accepted = [], []
    for pod in pods:
        status = _pod_status(pod)
        # Healthy pods are not events the watch would deliver.
        if status in ("Running", "Completed", "Succeeded"):
            continue
        seen.append((workload_of(pod) or pod.metadata.name, status))
        if watcher.enqueue(pod, status):
            accepted.append((workload_of(pod) or pod.metadata.name, status))

    return seen, accepted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="noise-test")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json")
    args = parser.parse_args()

    records = []
    for index in range(args.repeat):
        seen, accepted = one_round(args.namespace)
        workloads = sorted({name for name, _ in seen})
        by_workload = {}
        for name, status in accepted:
            by_workload.setdefault(name, []).append(status)

        # The unrelated incident has to survive the collapse.
        unrelated_visible = "lonely-worker" in by_workload
        collapsed = len([s for n, s in accepted if n == "flapping-api"])
        noisy_total = len([s for n, s in seen if n == "flapping-api"])

        ok = unrelated_visible and collapsed >= 1
        print(f"{'PASS' if ok else 'FAIL':5} round {index + 1}: "
              f"{len(seen)} failing pods over {len(workloads)} workloads -> "
              f"{len(accepted)} findings")
        for name, statuses in sorted(by_workload.items()):
            print(f"        {name}: {len(statuses)} finding(s) {sorted(set(statuses))}")
        print(f"        flapping-api {noisy_total} pods -> {collapsed} finding(s); "
              f"unrelated lonely-worker visible: {unrelated_visible}")

        records.append({
            "round": index + 1,
            "failing_pods": len(seen),
            "workloads": workloads,
            "findings": len(accepted),
            "by_workload": {k: sorted(set(v)) for k, v in by_workload.items()},
            "noisy_pods": noisy_total,
            "noisy_findings": collapsed,
            "unrelated_visible": unrelated_visible,
            "passed": ok,
        })

    passed = sum(r["passed"] for r in records)
    print(f"\nscore: {passed}/{len(records)}")
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(records, handle, indent=2)
        print(f"wrote {args.json}")
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
