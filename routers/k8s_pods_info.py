"""
Kubernetes inspection tools.

Every function here returns a small projection of the API response, never the
raw object. A raw pod list runs roughly 1,500 tokens per pod, so a single
namespace can exceed the model's whole context window; the projections below
keep a typical answer in the low hundreds of tokens.

All functions return a dict with an "error" key instead of raising when the
cluster is unreachable, so the agent can report the problem and move on.
"""

import contextvars
import datetime as dt
import json
import logging
import os
import threading

from kubernetes import client, config
from kubernetes.client.rest import ApiException

import podcache
from redaction import redact

log = logging.getLogger(__name__)

# Every call is bounded. An unreachable API server otherwise hangs the whole
# agent loop with no way out, since the model is waiting on the tool result.
TIMEOUT = int(os.getenv("K8S_TIMEOUT", "15"))

# Which context this caller asked for. A ContextVar rather than a global
# because two browser sessions in one process are two callers: with a global,
# one switching cluster switched it under the other, and the second session
# kept rendering with a label naming the cluster it was no longer reading.
_requested_context = contextvars.ContextVar("kubewhy_context", default=None)

# One set of clients per context, keyed by the requested name. Shared across
# callers on purpose -- the clients are read-only and their connection pools
# are worth reusing -- and cached rather than rebuilt, so switching back and
# forth costs nothing.
_bundles = {}

# Held across building a bundle, not just inserting it. load_kube_config sets
# the client library's global default configuration, so two threads binding
# different contexts at once can interleave between the load and the
# construction and hand one thread a client pointed at the other's cluster.
_bundle_lock = threading.Lock()


def _build_bundle(requested):
    """
    Every client for one context, plus the context they are actually bound to.

    new_client_from_config rather than load_kube_config: it returns an
    ApiClient for the named context instead of mutating the library's global
    default, which is what makes per-caller contexts possible at all.
    """
    try:
        config.load_incluster_config()
        api_client = client.ApiClient()
        active = "in-cluster"
    except config.ConfigException:
        api_client = config.new_client_from_config(context=requested)
        try:
            _, current = config.list_kube_config_contexts()
            active = requested or current["name"]
        except Exception:
            active = requested or "unknown"

    return {
        "core": client.CoreV1Api(api_client),
        "apps": client.AppsV1Api(api_client),
        "discovery": client.DiscoveryV1Api(api_client),
        # Read-only, and only ever listed -- these exist for scan_references,
        # which compares objects against each other rather than reading any
        # object's contents. Deliberately not SecretsV1: a reference to a
        # Secret can be reported without the Secret being read, and listing
        # them would return their data.
        "networking": client.NetworkingV1Api(api_client),
        "autoscaling": client.AutoscalingV2Api(api_client),
        "policy": client.PolicyV1Api(api_client),
        "storage": client.StorageV1Api(api_client),
        "active": active,
    }


def _bundle():
    """The client bundle for this caller's context, built once and cached."""
    requested = _requested_context.get()
    with _bundle_lock:
        if requested not in _bundles:
            _bundles[requested] = _build_bundle(requested)
        return _bundles[requested]


def list_namespaces():
    """
    Every namespace in the cluster.

    Not a model tool -- a UI helper. Deriving the list from a scan only showed
    namespaces that currently hold pods, so an empty namespace was unpickable
    and the filter looked broken. This asks the API directly, which the
    ClusterRole already allows and which costs one small request instead of a
    cluster-wide pod read.
    """
    try:
        found = _api().list_namespace(_request_timeout=TIMEOUT)
    except Exception:
        return []
    return sorted(ns.metadata.name for ns in found.items)


def workload_pods(namespace, workload):
    """
    Every pod belonging to one workload, not just the example the scan names.

    A Deployment is its replicas: showing one pod's logs and calling that the
    workload's story is wrong when three replicas fail for different reasons.
    UI helper, so it returns enough to choose between them.

    Narrowed by the owning controller's label selector where there is one, so
    the API server returns this workload's pods rather than the namespace's.
    The owner check below still decides what belongs: the selector is a
    transfer optimisation, and a selector that matched something extra would
    otherwise silently redefine what a workload is.
    """
    try:
        selector = _workload_selector(namespace, workload)
        pods = list(_iter_pods(namespace, label_selector=selector))
    except Exception as exc:
        return _handle(exc)

    matched = [
        {
            "pod": pod.metadata.name,
            "status": _pod_status(pod),
            "ready": all(cs.ready for cs in (pod.status.container_statuses or [])),
            "containers": [c.name for c in pod.spec.containers],
        }
        for pod in pods
        if (workload_of(pod) or pod.metadata.name) == workload
    ]
    return matched


def _workload_selector(namespace, workload):
    """
    The owning controller's pod selector, as a label_selector string.

    None when there is no single selector to find, which is ordinary rather
    than exceptional: a CronJob selects nothing (its Jobs each carry their own
    generated controller-uid), a static pod has no controller, and a bare pod
    is its own workload. Every one of those falls back to reading the namespace
    and filtering on owner references, which is what this function is an
    optimisation over -- so a miss costs the old behaviour, never an answer.

    A 404 is the normal negative result here, since the only way to learn which
    kind a workload is, is to ask. Anything else is a real failure and is
    raised, because silently degrading to a full namespace read would hide a
    403 as a performance problem.
    """
    apps = _apps_api()
    readers = (
        apps.read_namespaced_deployment,
        apps.read_namespaced_daemon_set,
        apps.read_namespaced_stateful_set,
    )

    for read in readers:
        try:
            spec = read(workload, namespace, _request_timeout=TIMEOUT).spec
        except ApiException as exc:
            if exc.status == 404:
                continue
            raise

        selector = spec.selector
        # match_expressions can be serialised into the same string, but they
        # are rare on controller pod selectors and getting the syntax subtly
        # wrong would silently drop pods. Falling back reads everything, which
        # is slower and right.
        if not selector or not selector.match_labels or selector.match_expressions:
            return None
        return ",".join(f"{k}={v}" for k, v in sorted(selector.match_labels.items()))

    return None


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
    look at the other.

    Scoped to the caller, not the process. Two browser sessions used to share
    one setting, so switching cluster in one switched it in the other, and the
    second went on labelling itself with a cluster it was no longer reading.
    The clients themselves are still shared, keyed by context -- they are
    read-only, and rebuilding a connection pool per session is waste.

    Callers that live across requests must set this per request rather than
    once: a ContextVar belongs to the context that set it, and Streamlit reruns
    the script on a fresh thread. ui.py re-asserts it from session state on
    every rerun for exactly that reason.
    """
    _requested_context.set(name)


def active_context():
    """
    Which cluster the API client is actually talking to.

    Deliberately not "whatever current-context says now". The client is built
    once and cached, so a kubeconfig rewritten afterwards moves the file
    without moving the client -- and `kind create cluster` rewrites
    current-context as a side effect.

    This is not cosmetic. It was observed: the browser UI labelled itself
    kind-loglens-cri while displaying pods that exist only in kind-triage-demo,
    because a second kind cluster had been created in another shell after the
    process started. A surface that names the wrong cluster is worse than one
    that names none, and SECURITY.md tells people to check this before
    trusting what they see.

    Answers for the caller asking, which is the point of scoping the context:
    two sessions on two clusters each get their own name back.
    """
    try:
        return _bundle()["active"] or "unavailable"
    except Exception:
        # Errors are data everywhere else in this module, and a caller asking
        # which cluster it is on must not be the one thing that raises -- the
        # browser UI asks before any tool runs, so raising here replaces the
        # whole page with a traceback.
        #
        # Not hypothetical: `kind delete cluster` removes current-context from
        # the kubeconfig entirely, and the next render died on it.
        return "unavailable"


def _api():
    """CoreV1Api for this caller's context. Built on first use, then cached."""
    return _bundle()["core"]


def _apps_api():
    """AppsV1Api, for deployments."""
    return _bundle()["apps"]


def _discovery_api():
    """DiscoveryV1Api, for EndpointSlices."""
    return _bundle()["discovery"]


def _networking_api():
    """NetworkingV1Api, for Ingresses."""
    return _bundle()["networking"]


def _autoscaling_api():
    """AutoscalingV2Api, for HorizontalPodAutoscalers."""
    return _bundle()["autoscaling"]


def _policy_api():
    """PolicyV1Api, for PodDisruptionBudgets."""
    return _bundle()["policy"]


def _storage_api():
    """StorageV1Api, for StorageClasses."""
    return _bundle()["storage"]


def _api_message(exc):
    """The API server's explanation, which is the part worth reading."""
    try:
        return json.loads(exc.body or "{}").get("message", "")
    except (TypeError, ValueError):
        return (exc.body or "").strip()[:300]


def _handle(exc):
    if isinstance(exc, ApiException):
        # reason alone is "Bad Request", which explains nothing. The body says
        # which container and why -- "is waiting to start: image can't be
        # pulled" is the whole diagnosis, and discarding it sent the model a
        # dead end instead.
        detail = _api_message(exc) or exc.reason
        return {"error": f"kubernetes API error {exc.status}: {detail}"}
    return {"error": f"cluster unreachable: {type(exc).__name__}: {exc}"}


def _age(when):
    """
    Compact age, the way kubectl prints it.

    Events are history, not state. A FailedScheduling warning from before a pod
    was scheduled sits in its event list forever, so a projection without an
    age presents a resolved 27-minute-old problem as if it were happening now.
    """
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)

    seconds = max(int((dt.datetime.now(dt.timezone.utc) - when).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


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
    # Init containers first, the way kubectl does it. A pod whose init
    # container is crashlooping -- "wait for the database" is the usual one --
    # has phase Pending and no app container status at all, so reading only
    # container_statuses reports "Pending" and it looks like a pod waiting to
    # be scheduled. The demo cluster has no init containers, so this was
    # invisible there and would be common anywhere real.
    for cs in pod.status.init_container_statuses or []:
        state = cs.state
        if state.terminated and state.terminated.exit_code == 0:
            continue  # this one finished; the pod moved on to the next
        if state.waiting and state.waiting.reason:
            return f"Init:{state.waiting.reason}"
        if state.terminated and state.terminated.reason:
            return f"Init:{state.terminated.reason}"

    statuses = pod.status.container_statuses or []
    for cs in statuses:
        state = cs.state
        if state.waiting and state.waiting.reason:
            return state.waiting.reason
        if state.terminated and state.terminated.reason:
            return state.terminated.reason
    return pod.status.phase or "Unknown"


def base_status(status):
    """The status without the Init: prefix, for classification."""
    return status[5:] if status.startswith("Init:") else status


def fault_of(status):
    """
    The fault class a status belongs to.

    An init container crashlooping is the same fault as an app container
    crashlooping: a different container, but the same problem and the same
    place to look. Grouping them apart would report one workload twice and
    give the init case no cooldown of its own.
    """
    return FAULT_CLASS.get(base_status(status), status)


def _is_healthy(pod):
    """
    Whether this pod is fine, meaning "do not report it".

    Running with every container ready is the obvious case. A Succeeded pod is
    the one that is easy to get wrong: a Job or CronJob that finished cleanly
    is not Running and has no ready containers, so a naive readiness check
    calls it broken. On a demo cluster nothing is ever Succeeded and the bug is
    invisible; on any real cluster every completed CronJob run would be
    reported as a failure, which is the fastest possible way to become noise.
    Observed exactly that against a cluster with finished Job pods on it.

    Failed pods are deliberately not exempt -- a Job that ran to failure is a
    real fault, and phase is Failed rather than Succeeded.
    """
    if pod.status.phase == "Succeeded":
        return True

    statuses = pod.status.container_statuses or []
    return bool(statuses) and _pod_status(pod) == "Running" and all(
        cs.ready for cs in statuses
    )


def workload_of(pod):
    """
    The owning workload, so ten crashing replicas count as one problem.

    Deployments, DaemonSets and StatefulSets all name their owner usefully.
    Three cases do not, and all three are ordinary on a real cluster:

    - **ReplicaSet** names are the deployment plus a hash; trim it, or every
      rollout looks like a new workload.
    - **Job** names created by a CronJob end in a scheduling timestamp. Without
      trimming, every scheduled run is a new workload, so the cooldown never
      applies and a job failing hourly reports hourly, forever.
    - **Node** means a static pod -- kube-apiserver, etcd, kube-scheduler.
      They are owned by the node they run on, so returning the owner name files
      every control plane component on that node under one entry, named after
      the node. Their pod name is "<component>-<node>", so drop the suffix.
    """
    for ref in pod.metadata.owner_references or []:
        if ref.kind == "ReplicaSet":
            return ref.name.rsplit("-", 1)[0]

        if ref.kind == "Job":
            head, _, tail = ref.name.rpartition("-")
            return head if head and tail.isdigit() else ref.name

        if ref.kind == "Node":
            name = pod.metadata.name
            suffix = f"-{ref.name}"
            return name[: -len(suffix)] if name.endswith(suffix) else name

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


def _iter_pods(namespace=None, page=500, label_selector=None):
    """
    Every pod, fetched a page at a time.

    One unbounded request for a large cluster is the wrong shape: at roughly
    7KB per pod a 1,000-pod cluster is a ~7MB response that has to arrive
    inside K8S_TIMEOUT, and a 10,000-pod cluster will not. Paging keeps each
    request small and bounded regardless of cluster size; the total transferred
    is the same, but no single call can time out on volume alone.

    A label_selector is the one filter the API server can actually apply to
    pods for us. Status cannot be narrowed server-side -- a CrashLoopBackOff
    pod is phase: Running -- so the scan has to read everything, but a caller
    that already knows which labels it wants should not.
    """
    api = _api()
    token = None

    while True:
        kwargs = {"limit": page, "_request_timeout": TIMEOUT}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if token:
            kwargs["_continue"] = token

        chunk = (
            api.list_namespaced_pod(namespace, **kwargs)
            if namespace
            else api.list_pod_for_all_namespaces(**kwargs)
        )
        yield from chunk.items

        token = (chunk.metadata._continue or None) if chunk.metadata else None
        if not token:
            return


def scan_cluster(
    only_unhealthy: bool = True,
    limit: int = 20,
    namespaces: str = "",
    workload: str = "",
):
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
    To answer "is X healthy?" pass workload -- it reports that workload's state
    whether or not anything is wrong with it, which is the only way to say a
    thing is fine. Never answer about a different workload than the one asked
    about; if it is not here, it does not exist under that name.
    Args: only_unhealthy -- when true, the default, omit workloads whose pods
    are all Running and Ready; limit -- how many workloads to return, largest
    blast radius first, default 20; namespaces -- comma-separated list to
    restrict the scan to, which is what you want on a large cluster; workload
    -- report only this workload, healthy or not.
    """
    wanted = [n.strip() for n in namespaces.split(",") if n.strip()]

    try:
        # One namespace is a much cheaper query than the whole cluster; take it
        # when it is the only one asked for.
        # A watch-backed cache when one is running and can vouch for being
        # current; otherwise exactly what this always did. pods_or_none()
        # returns None for every untrustworthy state, so there is one branch
        # here rather than a taxonomy of cache conditions.
        pods = podcache.pods_or_none()
        if pods is None:
            pods = list(_iter_pods(wanted[0] if len(wanted) == 1 else None))
        elif len(wanted) == 1:
            pods = [p for p in pods if p.metadata.namespace == wanted[0]]
    except Exception as exc:
        return _handle(exc)

    groups = {}
    for pod in pods:
        if wanted and pod.metadata.namespace not in wanted:
            continue
        # A pod that is already terminating is not a fault to report; on a
        # busy cluster these are the majority of the non-Running pods.
        if pod.metadata.deletion_timestamp:
            continue

        namespace = pod.metadata.namespace
        owner = workload_of(pod) or pod.metadata.name

        if workload:
            # Asked about one workload: report it whether or not it is broken.
            # "It is healthy" is an answer, and without this there was no way
            # to give it -- the scan returned only failures, so a question
            # about a healthy workload found nothing and got answered with
            # some other workload's problem instead.
            if workload.lower() not in (
                owner.lower(),
                f"{namespace}/{owner}".lower(),
                pod.metadata.name.lower(),
            ):
                continue
        elif only_unhealthy and _is_healthy(pod):
            continue

        status = _pod_status(pod)
        fault = fault_of(status)

        # Fault rather than status in the key: a rollout part-way through has
        # replicas reporting ErrImagePull and ImagePullBackOff at the same
        # instant, and splitting those would report one problem twice.
        entry = groups.setdefault(
            (namespace, owner, fault),
            {"status": status, "pods": 0, "example": pod.metadata.name},
        )
        entry["pods"] += 1
        if fault != status:
            # Only when it adds something: for Pending or Running the fault
            # class is just the status again, and the model pays for the
            # repetition in context.
            entry["fault"] = fault

    if not groups:
        if workload:
            # Distinct from "it is healthy", which now returns a row.
            return {"result": f"no workload named {workload} exists in this cluster"}
        where = f"namespace(s) {namespaces}" if wanted else "any namespace"
        scope = "unhealthy workloads" if only_unhealthy else "pods"
        return {"result": f"no {scope} in {where}"}

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

    Also reports each container's readiness, liveness and startup probes. A
    container can be Running with nothing terminated and still be broken: if
    it is not ready, its readiness probe is failing and the pod is receiving
    no traffic. A restart count next to a liveness probe means the probe is
    killing the container -- and an initial delay shorter than the app's start
    time makes the probe the fault rather than the app.

    Reports which ConfigMaps and Secrets each container consumes under
    "config", by name and by route, never by value. Use this when a
    configuration change appears to have had no effect: "updates_in_place":
    false means the container read that source once at startup and will not
    see an edit until it restarts, which is the case for env and envFrom and
    for any volume mounted with subPath. The pod stays Ready throughout and
    nothing else reports it.
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

        probes = _probes(spec)
        if probes:
            info["probes"] = probes

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

        config = _config_refs(pod, spec)
        if config:
            info["config"] = config

        containers[spec.name] = info

    projected = {
        "pod": name,
        "namespace": namespace,
        "status": _pod_status(pod),
        "node": pod.spec.node_name,
        "containers": containers,
    }

    pull_secrets = [s.name for s in (pod.spec.image_pull_secrets or []) if s.name]
    if pull_secrets:
        projected["image_pull_secrets"] = pull_secrets

    return projected


def _config_refs(pod, spec):
    """
    Which ConfigMaps and Secrets this container consumes, and by which route.

    The route is the whole point, not decoration. A ConfigMap reached through
    env or envFrom is read once when the container starts and is never read
    again: editing it changes the object and changes nothing in the running
    pod, which stays Ready and serves the old value indefinitely, with no
    event and no restart to give it away. Mounted as a volume it does refresh,
    within about a minute -- unless it is mounted with subPath, which never
    refreshes either. Three routes, two of which silently discard your change.

    So a reader asking "why did my config change not take effect" cannot be
    answered by the ConfigMap or by the pod, only by the edge between them.

    Names only. Never values: a Secret's whole point is that its contents do
    not travel, and this output reaches a model.
    """
    volumes = {v.name: v for v in (pod.spec.volumes or [])}
    refs = []

    def add(kind, name, via, updates):
        if name and not any(r["name"] == name and r["via"] == via for r in refs):
            refs.append(
                {"kind": kind, "name": name, "via": via, "updates_in_place": updates}
            )

    # envFrom: the whole ConfigMap or Secret as environment. Start-time only.
    for source in spec.env_from or []:
        if source.config_map_ref:
            add("ConfigMap", source.config_map_ref.name, "envFrom", False)
        if source.secret_ref:
            add("Secret", source.secret_ref.name, "envFrom", False)

    # Individual keys as environment. Also start-time only.
    for var in spec.env or []:
        source = var.value_from
        if not source:
            continue
        if source.config_map_key_ref:
            add("ConfigMap", source.config_map_key_ref.name, "env", False)
        if source.secret_key_ref:
            add("Secret", source.secret_key_ref.name, "env", False)

    # Volumes. These refresh, and subPath is the exception that does not.
    for mount in spec.volume_mounts or []:
        volume = volumes.get(mount.name)
        if not volume:
            continue
        via = "volume(subPath)" if mount.sub_path else "volume"
        updates = not mount.sub_path
        if volume.config_map:
            add("ConfigMap", volume.config_map.name, via, updates)
        if volume.secret:
            add("Secret", volume.secret.secret_name, via, updates)
        for source in getattr(volume.projected, "sources", None) or []:
            if source.config_map:
                add("ConfigMap", source.config_map.name, via, updates)
            if source.secret:
                add("Secret", source.secret.name, via, updates)

    return refs


def _probe(probe):
    """
    One probe, projected to what decides whether it is the fault.

    The handler says what is being checked, so a reader can run it themselves.
    The timings are here because a probe is as often the cause as the symptom:
    a container that needs 40s to start under a liveness probe with a 10s
    initial delay and a 3-failure threshold is killed at 40s, forever, and the
    pod looks like an application crash loop. Without the numbers that fault
    is invisible -- the status only ever says CrashLoopBackOff.
    """
    if probe.http_get:
        port = probe.http_get.port
        handler = f"httpGet {probe.http_get.path or '/'}:{port}"
    elif probe.tcp_socket:
        handler = f"tcpSocket :{probe.tcp_socket.port}"
    elif probe._exec:
        # _exec, not exec: the generated client cannot use a Python keyword as
        # an attribute name.
        #
        # Truncated, because an exec probe can carry a whole shell script and
        # this is a projection.
        handler = f"exec {' '.join(probe._exec.command or [])[:120]}"
    elif probe.grpc:
        handler = f"grpc :{probe.grpc.port}"
    else:
        handler = "unknown"

    projected = {"check": handler}

    # Only the values that were set. Defaulted timings are noise, and every
    # container carrying five extra fields is how a projection stops being one.
    for key, value in (
        ("initial_delay", probe.initial_delay_seconds),
        ("period", probe.period_seconds),
        ("timeout", probe.timeout_seconds),
        ("failure_threshold", probe.failure_threshold),
    ):
        if value is not None:
            projected[key] = value

    return projected


def _probes(spec):
    """Every probe configured on a container, keyed by kind."""
    found = {}
    for kind, probe in (
        ("readiness", spec.readiness_probe),
        ("liveness", spec.liveness_probe),
        ("startup", spec.startup_probe),
    ):
        if probe:
            found[kind] = _probe(probe)
    return found


def get_pod_events(name: str, namespace: str = "default", limit: int = 10):
    """
    Returns recent Warning events for a pod, newest first.

    Events explain scheduling failures, image pull errors and repeated
    restarts that the pod object itself does not spell out. Normal events are
    filtered out because they are rarely relevant to a fault.

    Each event carries an age, and it matters: events are history, not current
    state. A pod that waited on a taint before being scheduled keeps that
    FailedScheduling warning for its whole life, so a warning here does not
    mean the pod is failing now. Check the age against the pod's status before
    concluding anything -- a 27-minute-old warning on a Running pod is
    something that already resolved.
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
                "age": _age(e.last_timestamp or e.event_time),
                # Events echo container args, which sometimes carry secrets.
                "message": redact((e.message or "")[:300]),
            }
            for e in warnings[:limit]
        ],
    }


def _needs_container(exc):
    """Whether the API refused because the pod has several containers."""
    return (
        isinstance(exc, ApiException)
        and exc.status == 400
        and "container name must be specified" in (_api_message(exc) or "")
    )


def _no_logs(name, exc):
    """
    Explain an absence of logs, distinguishing expected from broken.

    A container that never started has no logs and never will -- normal for
    ImagePullBackOff or a failing init container. Reporting that as an API
    error tells the reader nothing and sends the model to a dead end.
    """
    detail = _api_message(exc) if isinstance(exc, ApiException) else str(exc)
    if "waiting to start" in detail or "ContainerCreating" in detail:
        return {
            "result": (
                f"no logs for pod {name}: its container has never started "
                f"({detail.split(': ')[-1].strip()}). Use describe_pod or "
                "get_pod_events to find out why."
            )
        }
    return _handle(exc)


def _failing_container(pod):
    """
    Which container's logs are worth reading in a multi-container pod.

    Sidecars are the norm at any scale -- a service mesh proxy, a log shipper,
    a metrics agent -- and the API refuses to guess: it returns 400 listing the
    names. Guessing "the first one" would hand back the proxy's logs while the
    application is the thing that crashed, which is worse than an error because
    it looks like an answer.

    So pick the container that is actually broken: not ready, or with a
    termination on record. Falling back to the first only when everything looks
    fine, where any choice is arbitrary anyway.
    """
    statuses = pod.status.container_statuses or []

    for cs in statuses:
        terminated = (cs.state and cs.state.terminated) or (
            cs.last_state and cs.last_state.terminated
        )
        if not cs.ready or terminated:
            return cs.name

    if statuses:
        return statuses[0].name
    return pod.spec.containers[0].name if pod.spec.containers else None


def get_pod_logs(
    name: str, namespace: str = "default", tail: int = 20, container: str = ""
):
    """
    Returns the last few log lines from a pod, including from a crashed
    container's previous run when the current one has no output.

    Use this to find the application-level error behind a crash, after
    describe_pod has told you the pod is restarting.

    A pod with more than one container -- anything with a service mesh or
    logging sidecar -- needs to know which one to read. Left unset, this picks
    the container that is failing rather than an arbitrary one, and reports
    which it chose. Pass container explicitly to read a specific one.
    Args: name -- the pod name; namespace -- defaults to "default";
    tail -- how many lines to return, capped at 100 to protect the context
    window; container -- which container, when the pod has several.
    """
    tail = min(tail, 100)
    api = _api()
    chosen = container or None

    def _read(previous):
        # _preload_content=False returns the raw response. Without it the
        # client stringifies the body and the model sees a literal b'...'
        # repr instead of the log text.
        kwargs = {"container": chosen} if chosen else {}
        resp = api.read_namespaced_pod_log(
            name,
            namespace,
            tail_lines=tail,
            previous=previous,
            _preload_content=False,
            _request_timeout=TIMEOUT,
            **kwargs,
        )
        return resp.data.decode("utf-8", errors="replace")

    def _attempt():
        # A container that just died usually has an empty current log; the
        # useful output belongs to the run that failed.
        logs = _read(previous=False).strip()
        if logs:
            return logs, "current"
        return _read(previous=True).strip(), "previous (crashed) container"

    try:
        logs, source = _attempt()
    except Exception as exc:
        if chosen is None and _needs_container(exc):
            # Multi-container pod. Resolve which one and try again -- only
            # paying for the extra read when the pod turns out to need it.
            try:
                chosen = _failing_container(
                    api.read_namespaced_pod(name, namespace, _request_timeout=TIMEOUT)
                )
                logs, source = _attempt()
            except Exception as retry:
                return _no_logs(name, retry)
        else:
            return _no_logs(name, exc)

    if not logs:
        return {"result": f"no logs available for pod {name}"}

    # Logs are the most likely place for a credential to surface. Redact
    # before this reaches the model context or the user's scrollback.
    cleaned = redact(logs)
    if cleaned != logs:
        log.warning("redacted secrets from logs of pod %s/%s", namespace, name)

    result = {"pod": name, "source": source, "logs": cleaned}
    if chosen:
        # Say which container these came from, or a sidecar's output reads as
        # the application's.
        result["container"] = chosen
    return result


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


def scan_references(namespace: str = "default"):
    """
    Finds references between objects that do not resolve, in one namespace.

    Use this when something is wrong and the pods all look fine. Most
    Kubernetes objects have no health of their own -- a Service, an Ingress, a
    PodDisruptionBudget is never "unhealthy", it is only pointing at something
    that is not there. Those faults leave every pod Running and Ready, so
    list_pods and scan_cluster cannot see them at all.

    No model reasoning is involved and nothing is inferred: a reference either
    resolves against the cluster or it does not. Reports the object, what it
    points at, and the symptom the break produces.

    Args: namespace -- defaults to "default".
    """
    broken = []
    checked = {}

    def failed(exc):
        return {"error": _api_message(exc) or str(exc)}

    # -- Services: selector resolving to nothing --------------------------
    try:
        services = _api().list_namespaced_service(
            namespace, _request_timeout=TIMEOUT
        ).items
        pods = _api().list_namespaced_pod(namespace, _request_timeout=TIMEOUT).items
    except Exception as exc:
        return failed(exc)

    checked["services"] = len(services)
    for svc in services:
        selector = svc.spec.selector
        if not selector:
            continue
        matched = [
            p for p in pods
            if all((p.metadata.labels or {}).get(k) == v for k, v in selector.items())
        ]
        if not matched:
            broken.append({
                "kind": "Service",
                "name": svc.metadata.name,
                "points_at": f"pods matching {selector}",
                "problem": "selector matches no pod in this namespace",
                "symptom": "the Service resolves but has no endpoints, so "
                           "connections fail immediately",
            })

    # -- Ingress: backend Service and port --------------------------------
    try:
        ingresses = _networking_api().list_namespaced_ingress(
            namespace, _request_timeout=TIMEOUT
        ).items
    except Exception:
        ingresses = []
    checked["ingresses"] = len(ingresses)

    by_name = {s.metadata.name: s for s in services}
    for ing in ingresses:
        for rule in ing.spec.rules or []:
            http = getattr(rule, "http", None)
            for path in (getattr(http, "paths", None) or []):
                backend = getattr(path.backend, "service", None)
                if not backend:
                    continue
                svc = by_name.get(backend.name)
                if svc is None:
                    broken.append({
                        "kind": "Ingress",
                        "name": ing.metadata.name,
                        "points_at": f"Service {backend.name}",
                        "problem": "no Service of that name exists here",
                        "symptom": "requests on this path return 503",
                    })
                    continue
                wanted = getattr(backend.port, "number", None)
                if wanted and wanted not in [p.port for p in (svc.spec.ports or [])]:
                    have = ", ".join(str(p.port) for p in (svc.spec.ports or []))
                    broken.append({
                        "kind": "Ingress",
                        "name": ing.metadata.name,
                        "points_at": f"Service {backend.name} port {wanted}",
                        "problem": f"that Service exposes {have or 'no ports'}",
                        "symptom": "requests on this path return 503",
                    })

    # -- HPA: scaleTargetRef ----------------------------------------------
    try:
        hpas = _autoscaling_api().list_namespaced_horizontal_pod_autoscaler(
            namespace, _request_timeout=TIMEOUT
        ).items
    except Exception:
        hpas = []
    checked["hpas"] = len(hpas)

    for hpa in hpas:
        # The API server already decided this and says so in a condition; that
        # is a measurement rather than a guess about what the name refers to.
        for condition in hpa.status.conditions or []:
            if condition.type == "AbleToScale" and condition.status == "False":
                ref = hpa.spec.scale_target_ref
                broken.append({
                    "kind": "HorizontalPodAutoscaler",
                    "name": hpa.metadata.name,
                    "points_at": f"{ref.kind} {ref.name}",
                    "problem": condition.reason or "cannot scale",
                    "symptom": "the workload never scales, which shows up only "
                               "under the load the HPA was added to absorb",
                })

    # -- PVC: StorageClass and binding ------------------------------------
    try:
        claims = _api().list_namespaced_persistent_volume_claim(
            namespace, _request_timeout=TIMEOUT
        ).items
        classes = {c.metadata.name for c in _storage_api().list_storage_class(
            _request_timeout=TIMEOUT
        ).items}
    except Exception:
        claims, classes = [], set()
    checked["pvcs"] = len(claims)

    for claim in claims:
        wanted = claim.spec.storage_class_name
        if wanted and classes and wanted not in classes:
            broken.append({
                "kind": "PersistentVolumeClaim",
                "name": claim.metadata.name,
                "points_at": f"StorageClass {wanted}",
                "problem": f"no such StorageClass (have: {', '.join(sorted(classes))})",
                "symptom": "the claim stays Pending and every pod mounting it "
                           "stays Pending behind it",
            })
        elif claim.status.phase == "Pending":
            broken.append({
                "kind": "PersistentVolumeClaim",
                "name": claim.metadata.name,
                "points_at": f"StorageClass {wanted or '(default)'}",
                "problem": "unbound",
                "symptom": "pods mounting this claim cannot schedule",
            })

    # -- PDB: permits no disruption at all --------------------------------
    try:
        budgets = _policy_api().list_namespaced_pod_disruption_budget(
            namespace, _request_timeout=TIMEOUT
        ).items
    except Exception:
        budgets = []
    checked["pdbs"] = len(budgets)

    for pdb in budgets:
        allowed = getattr(pdb.status, "disruptions_allowed", None)
        healthy = getattr(pdb.status, "current_healthy", 0) or 0
        if allowed == 0 and healthy > 0:
            broken.append({
                "kind": "PodDisruptionBudget",
                "name": pdb.metadata.name,
                "points_at": f"minAvailable {pdb.spec.min_available}"
                             if pdb.spec.min_available is not None
                             else f"maxUnavailable {pdb.spec.max_unavailable}",
                "problem": "permits zero voluntary disruptions",
                "symptom": "node drains and cluster upgrades block indefinitely; "
                           "this is an operations outage rather than an app one",
            })

    if not broken:
        return {
            "namespace": namespace,
            "checked": checked,
            "result": "every reference in this namespace resolves",
        }
    return {"namespace": namespace, "checked": checked, "broken": broken}


def _port_mismatch(api, namespace, svc):
    """
    Does this service's targetPort correspond to anything the pods declare?

    Two cases, and they differ in how certain the answer is.

    A NAMED targetPort must resolve to a port of that name on the container.
    If no backing pod declares it, the endpoint is built with no port and the
    service cannot work. That is a fact.

    A NUMERIC targetPort is only checked against declared containerPorts, and
    containerPort is informational -- a container may listen on a port it never
    declared, and kube-proxy will route there quite happily. So a numeric
    mismatch is strong evidence and not proof, and it is worded that way.
    Saying "this is broken" about a service that works would be the worse
    error: it sends someone to change a working config during an incident.
    """
    if not svc.spec.selector or not svc.spec.ports:
        return None

    selector = ",".join(f"{k}={v}" for k, v in svc.spec.selector.items())
    try:
        pods = api.list_namespaced_pod(
            namespace, label_selector=selector, limit=20, _request_timeout=TIMEOUT
        )
    except Exception:
        # Best effort. A service diagnosis must not fail because the extra
        # cross-check could not be made.
        return None

    numbers, names = set(), set()
    for pod in pods.items:
        for container in pod.spec.containers or []:
            for port in container.ports or []:
                if port.container_port is not None:
                    numbers.add(port.container_port)
                if port.name:
                    names.add(port.name)

    if not numbers and not names:
        # Nothing declared anywhere, so there is nothing to compare against.
        return None

    for port in svc.spec.ports:
        target = port.target_port
        if isinstance(target, str) and not target.isdigit():
            if target not in names:
                declared = ", ".join(sorted(names)) or "none"
                return (
                    f"port {port.port} targets the named port {target!r}, which "
                    f"no backing pod declares (declared: {declared}) -- a named "
                    "targetPort that does not resolve produces an endpoint with "
                    "no port, so this service cannot carry traffic even though "
                    "its endpoints are ready"
                )
        else:
            number = int(target) if target is not None else port.port
            if number not in numbers:
                declared = ", ".join(str(n) for n in sorted(numbers)) or "none"
                return (
                    f"port {port.port} targets container port {number}, which no "
                    f"backing pod declares (declared: {declared}) -- endpoints "
                    "are ready and traffic is very likely being refused. "
                    "containerPort is informational, so confirm by connecting "
                    "to a pod IP on that port before changing anything"
                )

    return None


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

    if ready:
        # The failure this catches is invisible everywhere else: pods Ready,
        # endpoints published, ready=true, Deployment Available, and every
        # connection refused because the service points at a port nothing
        # serves. Only worth checking when endpoints ARE ready -- with none,
        # the diagnoses below are the answer already.
        mismatch = _port_mismatch(api, namespace, svc)
        if mismatch:
            info["diagnosis"] = mismatch

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
