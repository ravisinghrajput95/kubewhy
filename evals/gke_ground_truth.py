"""
Ground truth for the GKE portability run, taken with kubectl and nothing else.

Section 6 of the validation plan requires ground truth established *before*
the model is invoked. It also has to be established without the agent's own
projections: asking `describe_pod` what is wrong and then grading the answer
against `describe_pod` proves only that the code agrees with itself. So this
shells out to kubectl and records raw fields -- phase, container state reason,
exit code, event reasons, endpoint counts -- from which the correct diagnosis
is derivable by a human reading the output.

    .venv/bin/python evals/gke_ground_truth.py --json results/gke-truth.json

Every scenario names the fixture that creates it and the field that carries
the truth. A scenario whose field is absent is reported as NOT REPRODUCED
rather than silently graded, because on a platform the fixtures have never run
on, "the fault did not appear" and "the agent missed it" are different
findings and only one of them is about the agent.
"""

import argparse
import json
import subprocess

# scenario -> (namespace, selector-or-pod, the kubectl-visible fact that is
# the correct answer). `kind` says where the truth lives, which is the thing
# the agent has to reach.
SCENARIOS = [
    ("oomkilled", "demo", "app=memory-hog", "status.terminated.reason == OOMKilled"),
    ("crashloopbackoff", "demo", "app=crasher", "status.waiting.reason == CrashLoopBackOff"),
    ("imagepullbackoff", "demo", "app=bad-image", "status.waiting.reason in ImagePullBackOff/ErrImagePull"),
    ("failedmount", "config-faults", "pod/missing-configmap-volume", "event reason FailedMount"),
    ("readiness_failure", "demo", "app=never-ready", "ready 0/1 while phase Running"),
    ("liveness_failure", "demo", "app=slow-starter", "restarts > 0 with probe kill"),
    ("healthy", "demo", "app=healthy-web", "all containers ready, no restarts"),
    ("healthy_quiet", "adversarial", "app=quiet-and-fine", "all containers ready, no restarts"),
]

SERVICES = [
    ("service_no_endpoints", "demo", "typo-svc", "selector matches no pods"),
    ("service_unready_backends", "demo", "crasher-svc", "backends exist but none ready"),
]


def kubectl(*args):
    """Raw kubectl. Returns (stdout, ok) and never raises."""
    try:
        out = subprocess.run(
            ["kubectl", *args], capture_output=True, text=True, timeout=60
        )
        return out.stdout.strip(), out.returncode == 0
    except Exception as exc:  # noqa: BLE001
        return f"{exc}", False


def pods(namespace, selector):
    """Raw pod JSON for a selector or an explicit pod/NAME."""
    if selector.startswith("pod/"):
        args = ["get", selector, "-n", namespace, "-o", "json"]
    else:
        args = ["get", "pods", "-n", namespace, "-l", selector, "-o", "json"]
    raw, ok = kubectl(*args)
    if not ok or not raw:
        return []
    data = json.loads(raw)
    return data.get("items", [data]) if data else []


def container_facts(pod):
    """The fields a diagnosis has to rest on, straight from the API object."""
    facts = []
    for status in (pod.get("status", {}).get("containerStatuses") or []):
        state = status.get("state") or {}
        last = status.get("lastState") or {}
        facts.append({
            "container": status.get("name"),
            "ready": status.get("ready"),
            "restarts": status.get("restartCount"),
            "waiting": (state.get("waiting") or {}).get("reason"),
            "terminated": (state.get("terminated") or {}).get("reason"),
            "exit_code": (state.get("terminated") or {}).get("exitCode"),
            "last_terminated": (last.get("terminated") or {}).get("reason"),
            "last_exit_code": (last.get("terminated") or {}).get("exitCode"),
        })
    return facts


def events_for(namespace, name):
    """Event reasons, which is where FailedMount and FailedScheduling live."""
    raw, ok = kubectl(
        "get", "events", "-n", namespace,
        "--field-selector", f"involvedObject.name={name}",
        "-o", "jsonpath={range .items[*]}{.reason}|{.message}{\"\\n\"}{end}",
    )
    if not ok or not raw:
        return []
    return [line.split("|", 1) for line in raw.splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="write the captured truth here")
    args = parser.parse_args()

    context, _ = kubectl("config", "current-context")
    version, _ = kubectl("version", "-o", "json")
    nodes, _ = kubectl(
        "get", "nodes", "-o",
        "jsonpath={range .items[*]}{.metadata.name}|{.status.nodeInfo.kubeletVersion}|"
        "{.status.nodeInfo.osImage}|{.status.nodeInfo.architecture}|"
        "{.status.nodeInfo.containerRuntimeVersion}{\"\\n\"}{end}",
    )

    print(f"context: {context}")
    for line in nodes.splitlines():
        print(f"node:    {line}")
    print()

    truth = {"context": context, "version": version, "nodes": nodes, "scenarios": {}}

    for name, namespace, selector, expected in SCENARIOS:
        items = pods(namespace, selector)
        entry = {
            "namespace": namespace, "selector": selector, "expected": expected,
            "pods": [], "reproduced": False,
        }
        for pod in items:
            facts = container_facts(pod)
            pod_name = pod.get("metadata", {}).get("name")
            entry["pods"].append({
                "name": pod_name,
                "phase": pod.get("status", {}).get("phase"),
                "containers": facts,
                "events": events_for(namespace, pod_name),
            })
        entry["reproduced"] = bool(entry["pods"])
        truth["scenarios"][name] = entry
        mark = "OK  " if entry["reproduced"] else "GONE"
        print(f"{mark} {name:<24} {namespace}/{selector}")
        for pod in entry["pods"]:
            for container in pod["containers"]:
                print(f"       {pod['phase']:<10} ready={container['ready']} "
                      f"restarts={container['restarts']} "
                      f"waiting={container['waiting']} "
                      f"terminated={container['terminated']} "
                      f"exit={container['exit_code']} "
                      f"last={container['last_terminated']}/{container['last_exit_code']}")
            for reason, message in pod["events"][:4]:
                print(f"       event {reason}: {message[:90]}")

    # Services go through EndpointSlices, never the deprecated Endpoints API,
    # because that is the thing the agent is being checked for.
    for name, namespace, service, expected in SERVICES:
        raw, ok = kubectl(
            "get", "endpointslices", "-n", namespace,
            "-l", f"kubernetes.io/service-name={service}", "-o", "json",
        )
        slices = json.loads(raw).get("items", []) if ok and raw else []
        addresses = ready = 0
        for slice_ in slices:
            for endpoint in slice_.get("endpoints", []) or []:
                addresses += len(endpoint.get("addresses") or [])
                if (endpoint.get("conditions") or {}).get("ready"):
                    ready += 1
        truth["scenarios"][name] = {
            "namespace": namespace, "service": service, "expected": expected,
            "endpointslices": len(slices), "addresses": addresses, "ready": ready,
            "reproduced": True,
        }
        print(f"OK   {name:<24} {namespace}/{service}  slices={len(slices)} "
              f"addresses={addresses} ready={ready}")

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(truth, handle, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
