"""
The entity the user asked about, held fixed for the whole investigation.

The agent may choose HOW to investigate. It may not change WHAT it is
investigating, and until now nothing stopped it. `scoped_question` states the
target in words, and words were not enough: measured 2026-08-19 over three
runs of "Is the correctly-configured pod in config-faults unhealthy?", two
answers described `missing-configmap-key` instead. Both had called

    list_pods(namespace="config-faults", only_unhealthy=True)

which excludes a healthy pod by construction, so the pod being asked about was
not in the result and the neighbours were. The one run that passed had called
`scan_cluster(workload="correctly-configured")`. The targeted tools exist; the
model simply does not always reach for them.

So the target is extracted once, before the first round, and every tool call is
checked against it. A call that would widen or move the scope is rewritten to
the target where the arguments allow it, and refused where they do not.

Precision over recall throughout. A target wrongly extracted would rewrite
every call to a workload that does not exist and break the run, while a target
missed simply leaves the old behaviour in place -- so a name is only taken when
the question labels it with a kind ("the crasher deployment", "pod X"), and
never from a bare word that might be English.
"""

import re

# Kinds a question names, mapped to how the tools address them.
WORKLOAD_KINDS = ("pod", "deployment", "daemonset", "statefulset", "job",
                  "cronjob", "workload", "replicaset")
SERVICE_KINDS = ("service", "svc")
NODE_KINDS = ("node",)

_ALL_KINDS = WORKLOAD_KINDS + SERVICE_KINDS + NODE_KINDS

# Words that follow a kind without naming one. "The pod restarted 9 times"
# must not yield a pod called `restarted`.
_NOT_A_NAME = {
    "is", "are", "was", "were", "be", "been", "the", "a", "an", "this", "that",
    "these", "those", "it", "its", "in", "on", "of", "and", "or", "for", "to",
    "has", "have", "had", "restarted", "restarting", "running", "failing",
    "crashing", "unhealthy", "healthy", "broken", "stuck", "logs", "log",
    "status", "events", "event", "name", "names", "why", "what", "which",
    "there", "any", "all", "some", "my", "your", "their", "does", "did", "do",
    "can", "should", "would", "will", "not", "no", "yes", "with", "without",
    "from", "into", "about", "actually", "still", "now", "here",
    # Adjectives that follow a kind and describe it rather than name it.
    "unreachable", "down", "up", "ready", "pending", "degraded", "ok", "fine",
    "wrong", "bad", "good", "slow", "missing", "failed", "errored",
    # Imperatives that precede a kind: "describe pod X", "check the service Y".
    "describe", "show", "list", "get", "check", "inspect", "diagnose",
    "explain", "tell", "find", "look", "read", "give", "report", "please",
}

_NAME = r"[a-z0-9][a-z0-9.-]*"

# "pod frontend-abc", "deployment named payments"
_KIND_FIRST = re.compile(
    r"\b(" + "|".join(_ALL_KINDS) + r")s?\b\s+(?:named\s+|called\s+)?"
    r"[`\"'*]*(" + _NAME + r")[`\"'*]*",
    re.IGNORECASE,
)
# "the crasher deployment", "correctly-configured pod"
_NAME_FIRST = re.compile(
    r"[`\"'*]*(" + _NAME + r")[`\"'*]*\s+\b(" + "|".join(_ALL_KINDS) + r")s?\b",
    re.IGNORECASE,
)
_NAMESPACE = re.compile(
    r"\bnamespace\s+[`\"'*]*(" + _NAME + r")[`\"'*]*"
    r"|\bin\s+(?:the\s+)?[`\"'*]*(" + _NAME + r")[`\"'*]*\s+namespace",
    re.IGNORECASE,
)


def _kind_of(word):
    word = word.lower().rstrip("s")
    if word in SERVICE_KINDS:
        return "service"
    if word in NODE_KINDS:
        return "node"
    return "workload"


def target_of(question):
    """
    The entity a question is about, or None when it names none.

    Returns {"kind": "workload"|"service"|"node", "name": ..., "namespace": ...}.
    A question about a whole namespace yields a target with no name, which
    still pins the namespace -- "what is broken in shop?" must not wander into
    `default`.
    """
    text = question or ""
    namespace = None
    match = _NAMESPACE.search(text)
    if match:
        namespace = (match.group(1) or match.group(2) or "").lower() or None
        if namespace in _NOT_A_NAME:
            namespace = None

    name = kind = None
    # Name-before-kind first, because that is how English asks: "the frontend
    # service", "the crasher deployment". Trying kind-first first read "service
    # unreachable" and made the adjective the target.
    for pattern, name_group, kind_group in (
        (_NAME_FIRST, 1, 2), (_KIND_FIRST, 2, 1),
    ):
        for found in pattern.finditer(text):
            candidate = found.group(name_group).strip(".,;:").lower()
            if not candidate or candidate in _NOT_A_NAME:
                continue
            if candidate in _ALL_KINDS or candidate == namespace:
                continue
            name, kind = candidate, _kind_of(found.group(kind_group))
            break
        if name:
            break

    if not name and not namespace:
        return None
    return {"kind": kind or "namespace", "name": name, "namespace": namespace}


def _same_workload(name, target_name):
    """
    Whether a pod name belongs to the target.

    A Deployment target `crasher` owns `crasher-5964d99948-9g8vg`; a DaemonSet
    target owns `log-shipper-8gnqk`. Prefix on a segment boundary, so `crasher`
    does not match `crasher-two`.
    """
    name, target_name = name.lower(), target_name.lower()
    return name == target_name or name.startswith(target_name + "-")


# Which argument each tool scopes by. Tools absent from this map are not
# entity-scoped -- the host collectors, and scan_references.
_WORKLOAD_ARG = {"list_pods": "workload", "scan_cluster": "workload"}
_POD_ARG = ("describe_pod", "get_pod_logs", "get_pod_events")
_NAMESPACE_ARG = ("list_pods", "describe_pod", "get_pod_events", "get_pod_logs",
                  "list_deployments", "get_service_endpoints", "scan_references")


def enforce(target, tool, arguments):
    """
    Hold a tool call to the target. Returns (arguments, violation or None).

    Two outcomes, never silent:

      retargeted  the arguments can carry the scope, so they are rewritten --
                  list_pods without a workload becomes list_pods for the
                  target, and a call into the wrong namespace is moved back
      refused     the arguments name a different pod or service outright, and
                  no rewrite can be honest about that, so the call is refused
                  with an error telling the model what it may look at

    The model still chooses the tool and the order. This only decides whether
    the call is about the entity that was asked about.
    """
    if not target:
        return arguments, None

    arguments = dict(arguments or {})
    name, namespace = target.get("name"), target.get("namespace")

    # Namespace first: it applies to every scoped tool, including ones with no
    # workload argument at all.
    if namespace and tool in _NAMESPACE_ARG:
        given = arguments.get("namespace")
        if given and str(given).lower() != namespace:
            was = arguments["namespace"]
            arguments["namespace"] = namespace
            return arguments, {
                "tool": tool, "action": "retargeted",
                "reason": f"namespace {was!r} is not the {namespace!r} that was asked about",
            }
        arguments.setdefault("namespace", namespace)

    if not name:
        return arguments, None

    if target["kind"] == "workload":
        argument = _WORKLOAD_ARG.get(tool)
        if argument:
            given = arguments.get(argument)
            if not given:
                arguments[argument] = name
                return arguments, {
                    "tool": tool, "action": "retargeted",
                    "reason": f"call was not scoped to {name!r}",
                }
            if not _same_workload(str(given), name):
                arguments[argument] = name
                return arguments, {
                    "tool": tool, "action": "retargeted",
                    "reason": f"{argument}={given!r} is not {name!r}",
                }

        if tool in _POD_ARG:
            given = str(arguments.get("name") or "")
            if given and not _same_workload(given, name):
                return arguments, {
                    "tool": tool, "action": "refused",
                    "reason": f"pod {given!r} does not belong to {name!r}",
                }

    if target["kind"] == "service" and tool == "get_service_endpoints":
        given = str(arguments.get("name") or "")
        if given and given.lower() != name:
            return arguments, {
                "tool": tool, "action": "refused",
                "reason": f"service {given!r} is not {name!r}",
            }

    return arguments, None
