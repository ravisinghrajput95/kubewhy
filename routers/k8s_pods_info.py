"""
Kubernetes pod inspection tools.

Every function here returns a small projection of the API response, never the
raw object. A raw pod list runs roughly 1,500 tokens per pod, so a single
namespace can exceed the model's whole context window; the projections below
keep a typical answer in the low hundreds of tokens.

All functions return a dict with an "error" key instead of raising when the
cluster is unreachable, so the agent can report the problem and move on.
"""

from kubernetes import client, config
from kubernetes.client.rest import ApiException

_core_v1 = None


def _api():
    """Lazily build a CoreV1Api, so importing this module never needs a cluster."""
    global _core_v1
    if _core_v1 is None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        _core_v1 = client.CoreV1Api()
    return _core_v1


def _handle(exc):
    if isinstance(exc, ApiException):
        return {"error": f"kubernetes API error {exc.status}: {exc.reason}"}
    return {"error": f"cluster unreachable: {type(exc).__name__}: {exc}"}


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
        pods = _api().list_namespaced_pod(namespace)
    except Exception as exc:
        return _handle(exc)

    result = {}
    for pod in pods.items:
        statuses = pod.status.container_statuses or []
        ready = sum(1 for cs in statuses if cs.ready)
        restarts = sum(cs.restart_count for cs in statuses)
        status = _pod_status(pod)

        healthy = status == "Running" and ready == len(statuses) and statuses
        if only_unhealthy and healthy:
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
        pod = _api().read_namespaced_pod(name, namespace)
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
                "message": (e.message or "")[:300],
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

    return {"pod": name, "source": source, "logs": logs}


if __name__ == "__main__":
    print(list_pods("demo", only_unhealthy=True))
