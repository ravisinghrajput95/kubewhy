"""
Kubernetes inspection tools.

Every function here returns a small projection of the API response, never the
raw object. A raw pod list runs roughly 1,500 tokens per pod, so a single
namespace can exceed the model's whole context window; the projections below
keep a typical answer in the low hundreds of tokens.

All functions return a dict with an "error" key instead of raising when the
cluster is unreachable, so the agent can report the problem and move on.
"""

import logging
import os

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from redaction import redact

log = logging.getLogger(__name__)

# Every call is bounded. An unreachable API server otherwise hangs the whole
# agent loop with no way out, since the model is waiting on the tool result.
TIMEOUT = int(os.getenv("K8S_TIMEOUT", "15"))

_core_v1 = None
_apps_v1 = None
_discovery_v1 = None

# Recorded at load time, never re-read. See active_context().
_active_context = None

# Set by use_context() to override the kubeconfig's current-context.
_requested_context = None


def _load_config():
    global _active_context
    try:
        config.load_incluster_config()
        _active_context = "in-cluster"
    except config.ConfigException:
        config.load_kube_config(context=_requested_context)
        try:
            _, active = config.list_kube_config_contexts()
            _active_context = _requested_context or active["name"]
        except Exception:
            _active_context = _requested_context or "unknown"


def list_contexts():
    """
    Every context in the kubeconfig, for callers that let you choose one.

    Empty in-cluster, where there is no kubeconfig and no choice to make.
    """
    try:
        contexts, _ = config.list_kube_config_contexts()
        return [context["name"] for context in contexts]
    except Exception:
        return []


def use_context(name):
    """
    Rebind every client to a named context.

    Working against two clusters at once is normal -- one being fixed, one
    being compared against -- and the alternative is restarting the process to
    look at the other. Dropping the cached clients is the whole job: they are
    built once and would otherwise keep talking to the previous cluster.

    Process-wide, deliberately: these clients are module state. That is fine
    for a loopback single-user tool and wrong for a shared one, so if this ever
    grows concurrent users the clients have to move into the session.
    """
    global _core_v1, _apps_v1, _discovery_v1, _requested_context, _active_context

    _requested_context = name
    _core_v1 = _apps_v1 = _discovery_v1 = None
    _active_context = None


def active_context():
    """
    Which cluster the API client is actually talking to.

    Deliberately not "whatever current-context says now". The client is built
    once and cached for the process, so a kubeconfig rewritten afterwards
    moves the file without moving the client -- and `kind create cluster`
    rewrites current-context as a side effect.

    This is not cosmetic. It was observed: the browser UI labelled itself
    kind-loglens-cri while displaying pods that exist only in kind-triage-demo,
    because a second kind cluster had been created in another shell after the
    process started. A surface that names the wrong cluster is worse than one
    that names none, and SECURITY.md tells people to check this before
    trusting what they see.
    """
    if _active_context is None:
        # Nothing bound yet: force the lazy load so there is a real answer
        # rather than a guess.
        try:
            _api()
        except Exception:
            # Errors are data everywhere else in this module, and a caller
            # asking which cluster it is on must not be the one thing that
            # raises -- the browser UI asks before any tool runs, so raising
            # here replaces the whole page with a traceback.
            #
            # Not hypothetical: `kind delete cluster` removes current-context
            # from the kubeconfig entirely, and the next render died on it.
            return "unavailable"
    return _active_context or "unavailable"


def _api():
    """Lazily build a CoreV1Api, so importing this module never needs a cluster."""
    global _core_v1
    if _core_v1 is None:
        _load_config()
        _core_v1 = client.CoreV1Api()
    return _core_v1


def _apps_api():
    """AppsV1Api, for deployments. Lazy for the same reason as _api()."""
    global _apps_v1
    if _apps_v1 is None:
        _load_config()
        _apps_v1 = client.AppsV1Api()
    return _apps_v1


def _discovery_api():
    """DiscoveryV1Api, for EndpointSlices."""
    global _discovery_v1
    if _discovery_v1 is None:
        _load_config()
        _discovery_v1 = client.DiscoveryV1Api()
    return _discovery_v1


def _handle(exc):
    if isinstance(exc, ApiException):
        return {"error": f"kubernetes API error {exc.status}: {exc.reason}"}
    return {"error": f"cluster unreachable: {type(exc).__name__}: {exc}"}


# One fault surfaces under several status names as a pod transitions, so
# anything grouping or deduping findings has to work on the fault rather than
# the symptom: a bad image reports ErrImagePull then ImagePullBackOff, and a
# pod killed for memory restarts into CrashLoopBackOff. Lives here rather than
# in controller.py because scan_cluster and the controller must agree on what
# counts as the same problem.
FAULT_CLASS = {
    "ErrImagePull": "image-pull",
    "ImagePullBackOff": "image-pull",
    "CrashLoopBackOff": "crash",
    "Error": "crash",
    # OOMKilled belongs with the crashes: a pod killed for memory restarts and
    # enters CrashLoopBackOff, so treating them separately made the controller
    # post the OOM diagnosis and then a near-identical crash diagnosis a minute
    # later.
    "OOMKilled": "crash",
    "Evicted": "evicted",
    "CreateContainerConfigError": "config",
}


def _pod_status(pod):
    """
    The status a human would recognise, matching what kubectl displays.

    A pod stuck in CrashLoopBackOff reports phase "Running", so the container
    waiting/terminated reason is the more truthful signal when present.
    """
    statuses = pod.status.container_statuses or []
    for cs in statuses:
        state = cs.state
        if state.waiting and state.waiting.reason:
            return state.waiting.reason
        if state.terminated and state.terminated.reason:
            return state.terminated.reason
    return pod.status.phase or "Unknown"


def _is_healthy(pod):
    """Running with every container ready -- the bar list_pods uses."""
    statuses = pod.status.container_statuses or []
    return bool(statuses) and _pod_status(pod) == "Running" and all(
        cs.ready for cs in statuses
    )


def workload_of(pod):
    """
    The owning workload, so ten crashing replicas count as one problem.

    ReplicaSet names are the deployment name plus a hash suffix; trimming it
    groups replicas of the same rollout together.
    """
    for ref in pod.metadata.owner_references or []:
        if ref.kind == "ReplicaSet":
            return ref.name.rsplit("-", 1)[0]
        return ref.name
    return None


def list_pods(namespace: str = "default", only_unhealthy: bool = False):
    """
    Lists pods in a namespace with name, status, ready count, restarts and node.

    This is the entry point for any question about workloads in the cluster.
    Args: namespace -- which namespace to inspect, defaults to "default";
    only_unhealthy -- when true, return only pods that are not Running and
    Ready, which is what you usually want when hunting for a problem.
    Follow up on a specific pod with describe_pod, get_pod_events or
    get_pod_logs.
    """
    try:
        pods = _api().list_namespaced_pod(namespace, _request_timeout=TIMEOUT)
    except Exception as exc:
        return _handle(exc)

    result = {}
    for pod in pods.items:
        statuses = pod.status.container_statuses or []
        ready = sum(1 for cs in statuses if cs.ready)
        restarts = sum(cs.restart_count for cs in statuses)
        status = _pod_status(pod)

        if only_unhealthy and _is_healthy(pod):
            continue

        result[pod.metadata.name] = {
            "status": status,
            "ready": f"{ready}/{len(statuses)}",
            "restarts": restarts,
            "node": pod.spec.node_name,
        }

    if not result:
        return {"result": f"no matching pods in namespace {namespace}"}
    return result


def scan_cluster(only_unhealthy: bool = True, limit: int = 20):
    """
    Scans every namespace at once and reports failing workloads.

    This is the tool for a question about the cluster as a whole -- "is
    anything broken?", "what is failing right now?" -- where no namespace was
    named. Once you know which namespace to look in, use list_pods instead.

    Results are grouped by owning workload rather than by pod, so a deployment
    with ten crashing replicas is one entry, not ten. Each entry names one
    example pod: pass that to describe_pod, get_pod_events or get_pod_logs.
    This tool tells you where to look and never why -- the cause always needs
    a follow-up call on the example pod.
    Args: only_unhealthy -- when true, the default, omit workloads whose pods
    are all Running and Ready; limit -- how many workloads to return, largest
    blast radius first, default 20.
    """
    try:
        pods = _api().list_pod_for_all_namespaces(_request_timeout=TIMEOUT)
    except Exception as exc:
        return _handle(exc)

    groups = {}
    for pod in pods.items:
        # A pod that is already terminating is not a fault to report; on a
        # busy cluster these are the majority of the non-Running pods.
        if pod.metadata.deletion_timestamp:
            continue

        healthy = _is_healthy(pod)
        if only_unhealthy and healthy:
            continue

        status = _pod_status(pod)
        namespace = pod.metadata.namespace
        workload = workload_of(pod) or pod.metadata.name
        fault = FAULT_CLASS.get(status, status)

        # Fault rather than status in the key: a rollout part-way through has
        # replicas reporting ErrImagePull and ImagePullBackOff at the same
        # instant, and splitting those would report one problem twice.
        entry = groups.setdefault(
            (namespace, workload, fault),
            {"status": status, "pods": 0, "example": pod.metadata.name},
        )
        entry["pods"] += 1
        if status in FAULT_CLASS:
            # Only when it adds something: for Pending or Running the fault
            # class is just the status again, and the model pays for the
            # repetition in context.
            entry["fault"] = fault

    if not groups:
        scope = "unhealthy workloads" if only_unhealthy else "pods"
        return {"result": f"no {scope} in any namespace"}

    # Largest blast radius first, so what survives truncation is what matters.
    ordered = sorted(groups.items(), key=lambda kv: (-kv[1]["pods"], kv[0]))
    shown, omitted = ordered[: max(limit, 1)], ordered[max(limit, 1) :]

    # A workload can carry two distinct faults at once -- a bad rollout leaves
    # the new ReplicaSet ImagePullBackOff while the old one still crashes --
    # and an unqualified key would drop one of them silently.
    seen = {}
    for (namespace, workload, _), _entry in shown:
        seen[(namespace, workload)] = seen.get((namespace, workload), 0) + 1

    result = {}
    for (namespace, workload, fault), entry in shown:
        key = f"{namespace}/{workload}"
        if seen[(namespace, workload)] > 1:
            key = f"{key}:{fault}"
        result[key] = entry

    if omitted:
        namespaces = {namespace for (namespace, _, _), _ in omitted}
        result["_truncated"] = (
            f"{len(omitted)} more not shown, across {len(namespaces)} "
            f"namespace(s); raise limit, or use list_pods on one namespace"
        )

    return result


def describe_pod(name: str, namespace: str = "default"):
    """
    Returns why a specific pod is in its current state.

    Includes each container's image, resource requests and limits, current
    state, and -- crucially -- the reason and exit code of the last
    termination. Use this after list_pods to explain a restart or a crash:
    an OOMKilled reason next to a low memory limit is the usual smoking gun.
    Args: name -- the pod name; namespace -- defaults to "default".
    """
    try:
        pod = _api().read_namespaced_pod(name, namespace, _request_timeout=TIMEOUT)
    except Exception as exc:
        return _handle(exc)

    containers = {}
    statuses = {cs.name: cs for cs in (pod.status.container_statuses or [])}

    for spec in pod.spec.containers:
        resources = spec.resources or client.V1ResourceRequirements()
        info = {
            "image": spec.image,
            "requests": resources.requests or {},
            "limits": resources.limits or {},
        }

        cs = statuses.get(spec.name)
        if cs:
            info["ready"] = cs.ready
            info["restarts"] = cs.restart_count

            if cs.state and cs.state.waiting:
                info["waiting_reason"] = cs.state.waiting.reason
                if cs.state.waiting.message:
                    info["waiting_message"] = cs.state.waiting.message[:300]

            last = cs.last_state.terminated if cs.last_state else None
            terminated = cs.state.terminated if cs.state else None
            ended = last or terminated
            if ended:
                info["last_termination"] = {
                    "reason": ended.reason,
                    "exit_code": ended.exit_code,
                }

        containers[spec.name] = info

    return {
        "pod": name,
        "namespace": namespace,
        "status": _pod_status(pod),
        "node": pod.spec.node_name,
        "containers": containers,
    }


def get_pod_events(name: str, namespace: str = "default", limit: int = 10):
    """
    Returns recent Warning events for a pod, newest first.

    Events explain scheduling failures, image pull errors and repeated
    restarts that the pod object itself does not spell out. Normal events are
    filtered out because they are rarely relevant to a fault.
    Args: name -- the pod name; namespace -- defaults to "default";
    limit -- how many events to return, default 10.
    """
    try:
        events = _api().list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.name={name}",
            _request_timeout=TIMEOUT,
        )
    except Exception as exc:
        return _handle(exc)

    warnings = [e for e in events.items if e.type == "Warning"]
    warnings.sort(key=lambda e: e.last_timestamp or e.event_time, reverse=True)

    if not warnings:
        return {"result": f"no warning events for pod {name}"}

    return {
        "pod": name,
        "events": [
            {
                "reason": e.reason,
                "count": e.count,
                # Events echo container args, which sometimes carry secrets.
                "message": redact((e.message or "")[:300]),
            }
            for e in warnings[:limit]
        ],
    }


def get_pod_logs(name: str, namespace: str = "default", tail: int = 20):
    """
    Returns the last few log lines from a pod, including from a crashed
    container's previous run when the current one has no output.

    Use this to find the application-level error behind a crash, after
    describe_pod has told you the pod is restarting.
    Args: name -- the pod name; namespace -- defaults to "default";
    tail -- how many lines to return, capped at 100 to protect the context
    window.
    """
    tail = min(tail, 100)
    api = _api()

    def _read(previous):
        # _preload_content=False returns the raw response. Without it the
        # client stringifies the body and the model sees a literal b'...'
        # repr instead of the log text.
        resp = api.read_namespaced_pod_log(
            name,
            namespace,
            tail_lines=tail,
            previous=previous,
            _preload_content=False,
            _request_timeout=TIMEOUT,
        )
        return resp.data.decode("utf-8", errors="replace")

    try:
        logs = _read(previous=False).strip()
        source = "current"

        # A container that just died usually has an empty current log; the
        # useful output belongs to the run that failed.
        if not logs:
            logs = _read(previous=True).strip()
            source = "previous (crashed) container"
    except Exception as exc:
        try:
            logs = _read(previous=True).strip()
            source = "previous (crashed) container"
        except Exception:
            return _handle(exc)

    if not logs:
        return {"result": f"no logs available for pod {name}"}

    # Logs are the most likely place for a credential to surface. Redact
    # before this reaches the model context or the user's scrollback.
    cleaned = redact(logs)
    if cleaned != logs:
        log.warning("redacted secrets from logs of pod %s/%s", namespace, name)

    return {"pod": name, "source": source, "logs": cleaned}


def list_nodes():
    """
    Returns cluster nodes with their ready state, any active pressure
    conditions, and allocatable CPU and memory.

    Use this when pods are Pending, being evicted, or failing to schedule, and
    to rule out a node-level cause before blaming a workload. A node under
    MemoryPressure or DiskPressure explains problems that look like
    application faults.
    """
    try:
        nodes = _api().list_node(_request_timeout=TIMEOUT)
    except Exception as exc:
        return _handle(exc)

    result = {}
    for node in nodes.items:
        conditions = {c.type: c.status for c in (node.status.conditions or [])}

        # Ready is "True" when healthy; the pressure conditions invert, so
        # only surface the ones that are actually firing.
        pressures = [
            name
            for name, status in conditions.items()
            if name != "Ready" and status == "True"
        ]

        allocatable = node.status.allocatable or {}
        result[node.metadata.name] = {
            "ready": conditions.get("Ready") == "True",
            "pressure": pressures or None,
            "allocatable_cpu": allocatable.get("cpu"),
            "allocatable_memory": allocatable.get("memory"),
            "unschedulable": bool(node.spec.unschedulable),
        }

    return result or {"result": "no nodes found"}


def list_deployments(namespace: str = "default"):
    """
    Returns deployments with desired, ready and available replica counts.

    Use this when a workload is degraded rather than a single pod: a
    deployment whose ready count is below its desired count is the signal
    that something is wrong, and names the pods worth inspecting next.
    Args: namespace -- which namespace to inspect, defaults to "default".
    """
    try:
        deployments = _apps_api().list_namespaced_deployment(namespace, _request_timeout=TIMEOUT)
    except Exception as exc:
        return _handle(exc)

    result = {}
    for dep in deployments.items:
        status = dep.status
        desired = dep.spec.replicas or 0
        ready = status.ready_replicas or 0

        result[dep.metadata.name] = {
            "desired": desired,
            "ready": ready,
            "available": status.available_replicas or 0,
            "healthy": ready == desired,
            "images": [c.image for c in dep.spec.template.spec.containers],
        }

    return result or {"result": f"no deployments in namespace {namespace}"}


def get_service_endpoints(name: str, namespace: str = "default"):
    """
    Returns a service's selector, ports, and the pod endpoints currently
    backing it, split into ready and not-ready.

    This is the tool for "why is my service unreachable". A service with zero
    ready endpoints is reachable in name only -- traffic has nowhere to go.
    That usually means the selector matches no pods, or the pods it matches
    are failing their readiness probe.
    Args: name -- the service name; namespace -- defaults to "default".
    """
    api = _api()

    try:
        svc = api.read_namespaced_service(name, namespace, _request_timeout=TIMEOUT)
    except Exception as exc:
        return _handle(exc)

    info = {
        "service": name,
        "namespace": namespace,
        "type": svc.spec.type,
        "selector": svc.spec.selector,
        "ports": [
            {"port": p.port, "target_port": str(p.target_port)}
            for p in (svc.spec.ports or [])
        ],
    }

    # EndpointSlice, not the v1 Endpoints resource: Endpoints is deprecated
    # and silently truncates above 1000 addresses, which would quietly
    # misdiagnose exactly the large services that matter most.
    try:
        slices = _discovery_api().list_namespaced_endpoint_slice(
            namespace,
            label_selector=f"kubernetes.io/service-name={name}",
            _request_timeout=TIMEOUT,
        )
    except Exception as exc:
        return _handle(exc)

    if not slices.items:
        info["ready_endpoints"] = []
        info["not_ready_endpoints"] = []
        info["diagnosis"] = (
            "no EndpointSlice exists for this service -- nothing has ever "
            "matched its selector"
        )
        return info

    ready, not_ready = [], []
    for endpoint_slice in slices.items:
        for endpoint in endpoint_slice.endpoints or []:
            # conditions.ready is None on older API servers; absent means
            # ready, per the EndpointSlice spec.
            conditions = endpoint.conditions
            is_ready = conditions is None or conditions.ready in (True, None)
            (ready if is_ready else not_ready).extend(endpoint.addresses or [])

    info["ready_endpoints"] = ready
    info["not_ready_endpoints"] = not_ready

    if not ready:
        # The two causes need different fixes, and the endpoint lists tell
        # them apart: pods that match but are unready vs nothing matching.
        if not_ready:
            info["diagnosis"] = (
                f"no ready endpoints: {len(not_ready)} pod(s) match the "
                "selector but none are ready -- inspect those pods, they are "
                "crashing or failing a readiness probe"
            )
        else:
            info["diagnosis"] = (
                "no ready endpoints: the selector matches no pods at all -- "
                "check the selector against the pod labels"
            )

    return info


if __name__ == "__main__":
    print(list_pods("demo", only_unhealthy=True))
