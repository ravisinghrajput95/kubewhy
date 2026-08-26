"""
kubewhy.

Wraps the host and Kubernetes collectors in routers/ as tools and lets a local
Ollama model call them to work out what is wrong and why.

    python agent.py "why is this machine slow?"
    python agent.py "what is broken in the demo namespace?"
    python agent.py --scan                 # every namespace, no model involved
    python agent.py --scan --explain 3     # then diagnose the worst three
"""

import json
import logging
import os
import re
import sys
import time
import uuid

import audit
import backends  # noqa: F401  -- re-exported for callers that name a backend

import grounding
import inference
import observability
import targeting
import telemetry
from routers.platform_info import get_platform_info
from routers.system_info import get_system_info
from routers.process_info import get_processes
from routers.top_cpu import get_top_cpu_processes
from routers.top_memory import get_top_memory_processes
from routers.k8s_pods_info import (
    service_namespace,
    scan_cluster,
    list_pods,
    describe_pod,
    get_pod_events,
    get_pod_logs,
    list_nodes,
    list_deployments,
    get_service_endpoints,
    scan_references,
)

# OLLAMA_HOST is read by the ollama client itself; in a container it needs to
# point back at the host, e.g. http://host.docker.internal:11434
MODEL = os.getenv("TRIAGE_MODEL", "qwen3")

# TIMEOUT and KEEP_ALIVE moved to backends.py on 2026-08-22, with the provider
# they belong to -- both are named for Ollama and neither means anything to a
# hosted API. They are deliberately NOT re-exported here: a module attribute
# that no longer reaches the request is worse than a missing one, because
# patching it in a test goes green while controlling nothing. That is exactly
# how it surfaced -- two keep-alive tests kept patching agent.KEEP_ALIVE and
# started asserting against a value the request never saw.

# Thinking mode, on by default. Exposed so a benchmark can measure the
# trade-off rather than argue about it: qwen3's reasoning tokens are where
# essentially all of this agent's latency goes, and the question is what
# turning them off costs in accuracy. The default does not change.
THINK = os.getenv("TRIAGE_THINK", "true").lower() != "false"

log = logging.getLogger("triage.agent")

# Max tool-calling rounds before we give up. Guards against a model that
# keeps calling tools without ever settling on an answer.
MAX_ROUNDS = 8

# Wall-clock ceiling on one whole investigation, in seconds: every round, every
# tool call, every retry and any fallback.
#
# MAX_ROUNDS bounds the number of model calls and OLLAMA_TIMEOUT bounds each
# one, but their product is 8 x 300s = 40 minutes and nothing was enforcing
# anything smaller. Measured 2026-08-23 with a provider answering just under
# its timeout every round: the loop ran all eight, terminating but occupying
# the controller for the whole of it.
#
# **600s is derived, not chosen.** Across 1273 recorded investigations the
# median is 54.6s, p95 is 182.2s and p99 is 318.0s; 600s clears p99 with
# roughly 1.9x headroom. Sixteen runs exceeded 300s and five exceeded 600s,
# and those five are the runs this project has already attributed to the host
# suspending mid-run rather than to the model -- see `slept_ms`. It is also
# twice OLLAMA_TIMEOUT, so a run survives one complete provider timeout and a
# fallback attempt, and a third of the controller's 1800s per-workload
# cooldown, so a finding can never outlive the window that dedups it.
#
# Raise it for a CPU-only cluster: a GKE node with no accelerator needed ~128s
# per diagnosis with thinking off and exceeded 300s with it on.
INVESTIGATION_BUDGET = int(os.getenv("TRIAGE_INVESTIGATION_BUDGET", "600"))

# How many times a run may be sent back for naming a tool it did not call.
# One: the point is to catch the model that stopped one step short, not to
# argue with a model that has decided it is finished.
MAX_NUDGES = 1

TOOLS = {
    "get_platform_info": get_platform_info,
    "get_system_info": get_system_info,
    "get_processes": get_processes,
    "get_top_cpu_processes": get_top_cpu_processes,
    "get_top_memory_processes": get_top_memory_processes,
    "scan_cluster": scan_cluster,
    "list_pods": list_pods,
    "describe_pod": describe_pod,
    "get_pod_events": get_pod_events,
    "get_pod_logs": get_pod_logs,
    "list_nodes": list_nodes,
    "list_deployments": list_deployments,
    "get_service_endpoints": get_service_endpoints,
    "scan_references": scan_references,
}

SYSTEM_PROMPT = """You are a triage assistant. You can inspect two separate
systems, and you must not confuse them:

- The local host this process runs on: platform, system, process, cpu and
  memory tools. Use these for questions about "this machine".
- A Kubernetes cluster: the pod tools. Use these for questions about pods,
  containers, namespaces, deployments or workloads.

You answer by calling tools and reading the real values they return. Never
invent a number or a status: if you have not called a tool for it, you do not
know it. Answer every part of a multi-part question, calling one tool per part
if needed.

When the question is about the cluster as a whole and names no namespace, start
with scan_cluster: it finds failing workloads across every namespace at once
and names one example pod for each. Drill into that example pod from there. If
a namespace is named, use list_pods instead. On a large cluster, narrow the
scan with its namespaces argument rather than reading everything.

When you are asked about one particular workload, pass its name as
scan_cluster's workload argument: that reports its state whether or not
anything is wrong with it. Two rules follow, and they matter more than being
helpful:

- If the thing you were asked about is healthy, say exactly that and stop. "It
  is running normally" is a complete answer. Do not go looking for some other
  problem to report instead.
- Only ever describe the workload you were asked about. If you cannot find it,
  say you could not find it. Answering about a different workload is worse than
  saying nothing, because it reads as an answer.

To diagnose a failing pod, work down the chain: list_pods to find what is
unhealthy, then describe_pod for the termination reason and resource limits,
then get_pod_events or get_pod_logs for the underlying cause. Do not stop at
the status name -- OOMKilled or CrashLoopBackOff is the symptom, and the user
wants the reason behind it.

For a service that is unreachable, start with get_service_endpoints: a service
with no ready endpoints has nowhere to send traffic, and the matching pods are
what to inspect next. For a workload that is degraded rather than dead, use
list_deployments to compare ready against desired replicas. If pods are
Pending or being evicted, check list_nodes for pressure before blaming the
workload.

When reporting a problem, name the specific pod or process responsible, give
the measured figure, and say what you would change. Be concise -- a few
sentences, not a report.

Never state an inference as if you measured it. If you read it from a tool,
say it plainly; if you are reasoning past what the tools showed, mark it --
"likely", "probably", "worth checking". A guess printed in the same voice as a
measurement is the one thing a reader cannot recover from."""


_BACKEND = None


def _backend():
    """
    The inference gateway, resolved once.

    Not a raw backend since 2026-08-23. `inference.Gateway` presents the same
    four methods a backend does, so nothing in this loop changed -- but behind
    them sit the three decisions a backend has no business making: where
    inference happens, whether cluster evidence may leave the network to get
    there, and what to do when it cannot be reached. See inference.py.

    The name is kept. Every call site here asks "which provider shapes this
    message", and the gateway answers that question for whichever provider
    actually replied.
    """
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = inference.gateway()
    return _BACKEND


def _chat(model, messages, think, timeout=None):
    """
    One model call, through whichever backend is configured.

    The provider lives in backends.py: it makes the call, normalises the reply
    and owns the wire shape of messages, because providers disagree there in
    ways this loop must not care about. Ollama remains the default, and
    changing that default would change what this project claims about your
    data -- see the module docstring there.

    Returns (reply, think_actually_used). The second value is what the
    provider did rather than what was asked for: a model with no thinking mode
    answers without one, and a run that records the request instead of the
    outcome cannot say which arm it measured.
    """
    reply = _backend().chat(model, messages, _backend().tools(TOOLS), think,
                            timeout=timeout)
    return reply, reply.think_used


# Argument values that are a model describing an argument rather than supplying
# one. Observed live 2026-08-18: llama3.2 called
# describe_pod(name="bad-image-<random chars>") -- the placeholder from its own
# reasoning, sent as a literal Kubernetes object name. The API answers 404 to
# that, which reads to the model as "the pod does not exist" rather than "you
# did not name a pod", and the run continues down a false trail.
_PLACEHOLDER = re.compile(
    r"<[^>]*>"                      # <random chars>, <pod-name>, <name>
    r"|\{\{?[a-z_ -]+\}?\}"        # {name}, {{ pod }}
    r"|^\.\.\.$"                    # bare ellipsis
    r"|^(pod|deployment|service|workload|namespace)[-_]?(name|here)$"
    r"|^(your|the|some|any)[-_ ]",   # "your-pod", "the namespace"
    re.IGNORECASE,
)


def unresolved(arguments):
    """
    The arguments whose values are placeholders rather than real names.

    A tool argument is a fact about the cluster, and a model that has not
    discovered a name yet must go and discover it -- not invent something
    shaped like one. Returning this as a tool error rather than raising keeps
    rule 3: the loop survives, and the model is told what it actually did
    wrong, which "404 not found" never says.
    """
    bad = []
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and value.strip() and _PLACEHOLDER.search(value.strip()):
            bad.append(f"{key}={value!r}")
    return bad


def _resolve_entity(name):
    """
    Where the cluster has a workload or service by this name.

    Returns {"kind", "namespace"} so the caller inherits the namespace the
    lookup already had to find. Returning the kind alone left the model to
    guess `default` and report a service in `demo` as nonexistent.

    One read-only lookup, the same scan_cluster the agent would call, so the
    check costs a single API request and can never see anything the agent
    could not. Returns None on any error: an unreachable cluster must leave
    the target unset rather than guess, because a wrong target is worse than
    none.
    """
    try:
        found = scan_cluster(workload=name)
    except Exception:
        return None

    if isinstance(found, dict) and found and "error" not in found and "result" not in found:
        # scan_cluster keys are "namespace/workload".
        key = next((k for k in found if not str(k).startswith("_")), "")
        namespace = str(key).split("/")[0] if "/" in str(key) else None
        return {"kind": "workload", "namespace": namespace}

    # A Service is not a workload and scan_cluster does not report one, which
    # is why "Why is crasher-svc unreachable?" resolved to nothing on the
    # first cut of this.
    namespace = service_namespace(name)
    return {"kind": "service", "namespace": namespace} if namespace else None


def _run_tool(name, arguments):
    """Execute one tool call, returning its result as a JSON string."""
    func = TOOLS.get(name)
    if func is None:
        return json.dumps({"error": f"no such tool: {name}"})

    placeholders = unresolved(arguments)
    if placeholders:
        log.info("rejected_placeholder_argument", extra={"tool": name, "arguments": arguments})
        return json.dumps({"error":
            f"{', '.join(placeholders)} is a placeholder, not a real name. "
            "Call a discovery tool first -- list_pods or scan_cluster -- and "
            "use a name it returned."})

    try:
        result = func(**arguments)
    except Exception as exc:
        # Hand the failure back to the model rather than crashing the loop;
        # it can usually recover by trying a different tool.
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    return json.dumps(result, default=str)


def _outcome(output):
    """
    Whether a tool result is an error, for the metric.

    Rule 3 says errors come back as {"error": ...} data rather than raised, so
    "did that tool fail" is a question about the document, not about an
    exception that never happened. A run where every list_pods returns an
    error is a broken RBAC grant and looks, from the outside, exactly like a
    run where the model simply chose badly -- which is what this label is for.
    """
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return "ok"
    return "error" if isinstance(parsed, dict) and "error" in parsed else "ok"


def scoped_target(workload, namespace, pod=None):
    """
    The structured target for a surface that already knows its selection.

    `scoped_question` states the target in prose for the model; this states the
    same target as data for `targeting.enforce`. Both are built from one
    selection, so the two cannot disagree.

    They used to. `targeting.target_of()` re-derived the target by parsing the
    prompt `scoped_question` had just written, and that prompt is full of
    name-before-kind English: "(for example pod nightly-sync-abc)" yields a
    workload called `example`, and "Do not report on any other workload" yields
    one called `other`. Whichever it picked, `enforce` then rewrote every tool
    call to it -- including calls the model had got right -- and the run died on
    "no workload named example exists in this cluster". Deterministic, so it
    reproduced identically on two different models.

    Naming the target is the caller's job whenever the caller knows it. Parsing
    stays for the surfaces that genuinely only have a sentence: the CLI, Slack,
    and an MCP client.

    `name` is the bare workload name, not `namespace/workload`: enforce matches
    pod names against it by prefix, so `crasher` has to own
    `crasher-5964d99948-9g8vg`.
    """
    bare = (workload or "").split("/")[-1].split(":")[0].strip()
    if not bare:
        return None
    return {"kind": "workload", "name": bare.lower(),
            "namespace": (namespace or "").strip().lower() or None,
            "pod": pod}


def scoped_question(question, workload, namespace, pod=None):
    """
    Bind a question to one workload, for every surface that has a selection.

    This lived in the browser UI, which meant the CLI, the REST API and an MCP
    client had no way to say "answer about this one". A fix that only one of
    five entry points benefits from is not a fix.

    Directive on purpose. Phrasing it as a hint failed in testing: asked "what
    is the issue here?" about a healthy workload, the model read the question as
    cluster-wide, called scan_cluster() with its default only_unhealthy=True --
    which by design omits a healthy workload -- and reported the first failure
    it happened to find instead.

    Naming the tool matters as much as naming the workload. Without workload=,
    no call the model can make will see a healthy workload, so "it is fine" is
    not an available answer and the silence gets filled with something else.
    """
    # "(for example pod X)" reads to a human as an aside and to
    # targeting.target_of() as a name-before-kind phrase naming a workload
    # called "example" -- which then rewrote every tool call in the run. A
    # colon cannot be read as either: _KIND_FIRST requires whitespace after the
    # kind word, and there is no bare word in front of "pod" to be taken as a
    # name. Keep it that way.
    # The audit trail wants what the person typed, not the four sentences of
    # direction wrapped around it. Recorded here because this is the one place
    # that does the wrapping, and the loop below never sees the difference.
    audit.asked(question)

    example = f" (pod: {pod})" if pod else ""
    return (
        f"Answer only about the workload {workload} in namespace {namespace}"
        f"{example}. Start with scan_cluster(workload='{workload}') to read its "
        "current state, which reports it whether or not it is failing. If it is "
        "healthy, say so and stop. Do not report on any other workload, even if "
        f"you find one that is broken.\n\nQuestion: {question}"
    )


def capture_pod_logs(pod, namespace, tail=50):
    """
    Read a pod's logs now, to hand to a diagnosis that runs later.

    Shared by the controller's watch and the CLI's --explain, because both
    know which pod they are about to ask about and both are slower than the
    pod. A CronJob pod lives about two minutes; a diagnosis takes longer, so
    by the time the model calls get_pod_logs the pod is gone and every tool
    answers 404. Measured on --explain against a real CronJob: the chain ran
    scan_cluster, describe_pod, get_pod_logs, then list_pods -- the last call
    being the model going to look elsewhere -- and the answer reached no root
    cause at all.

    Returns the list shape ask(prefetched=...) takes, or [] if the read fails
    or comes back as an error. Failing to capture restores the previous
    behaviour rather than introducing a new one.
    """
    arguments = {"name": pod, "namespace": namespace, "tail": tail}
    try:
        result = get_pod_logs(**arguments)
    except Exception:  # noqa: BLE001
        return []

    # An error is data, not logs. Passing one on would hand the model a 404
    # dressed up as a measurement.
    if isinstance(result, dict) and (result.get("error") or result.get("result")):
        return []

    return [{
        "name": "get_pod_logs",
        "arguments": arguments,
        "result": json.dumps(result, default=str),
        "captured_at": time.strftime("%H:%M:%S"),
    }]


def _prefetched_block(prefetched):
    """
    Render evidence collected before the loop started, for the user message.

    Written to be read by the model as fact rather than as a hint, and
    timestamped, because the whole reason it exists is that the subject may no
    longer be there to re-read. It says so explicitly: a tool returning 404 for
    this pod is expected, and does not mean the evidence below is wrong.
    """
    parts = []
    for item in prefetched:
        args = ", ".join(f"{k}={v!r}" for k, v in (item.get("arguments") or {}).items())
        parts.append(
            f"{item['name']}({args}) returned, at {item.get('captured_at', 'an earlier time')}:\n"
            f"{item['result']}"
        )
    return (
        "\n\nEvidence already collected for you, while the pod was still "
        "running. The pod may have been deleted since — if a tool now returns "
        "a 404 for it, that is expected and does not contradict this. For a "
        "Job or CronJob pod this is the only record that will ever exist, so "
        "do not ask for it again and do not withhold a diagnosis for want of "
        "it:\n\n" + "\n\n".join(parts)
    )


def _timing(model_ms, tool_ms, round_ms, wall_ms=None, slept_ms=0.0):
    """
    Where a run's wall clock went, split model against tools.

    Reported because the run-level timer in the evals could only say *that* a
    run took 2217s, never *where*. Two hypotheses have already died on that
    ambiguity, so the next one should not have to.

    `slowest_round_ms` is the field to look at first: a stall concentrated in
    one round is a hung request, while a run that is uniformly slow across
    every round is something systemic. `unaccounted_ms` is the rest -- JSON
    encoding, grounding, the generator's own overhead -- and should be small.
    If it is not, the interesting thing is happening outside both.

    `slept_ms` is the first thing that turned out to be. Every other timer
    here is monotonic, and a monotonic clock does not advance while the
    machine is asleep, so a laptop that naps mid-run reports a wall clock
    hundreds of seconds longer than the work it did. Measured 2026-08-17: a
    725s run against a 62s median, of which the model accounted for 180s;
    `pmset -g log` shows the machine asleep for 548s inside that window
    (`Idle Sleep` 184s, then `Maintenance Sleep` 364s) against 545s
    unaccounted. That is the stall this project has chased through two dead
    hypotheses. An unattended benchmark on battery sleeps because nobody is
    typing -- macOS idle sleep counts HID input, not CPU load -- which is why
    the stalls preferred idle machines and arrived in adjacent runs.
    """
    total = model_ms + tool_ms
    timing = {
        "model_ms": round(model_ms, 1),
        "tool_ms": round(tool_ms, 1),
        "rounds": len(round_ms),
        "round_ms": round_ms,
        "slowest_round_ms": max(round_ms) if round_ms else 0.0,
        "model_share": round(model_ms / total, 3) if total else None,
    }
    if wall_ms is not None:
        timing["wall_ms"] = round(wall_ms, 1)
        timing["unaccounted_ms"] = round(wall_ms - total, 1)
        timing["slept_ms"] = round(slept_ms, 1)
    return timing


def named_but_not_called(answer, called):
    """
    Tools the answer tells the reader to run, which this run never ran.

    Measured on `crashloop_root_cause`, the case this project exists to get
    right: on 2 of 10 runs the model read describe_pod, saw `exit_code: 1`,
    and finished with "Next Step: check the container logs (get_pod_logs)"
    -- naming the one tool that holds the answer ("FATAL: could not connect
    to db:5432") instead of calling it. It is not a model that misunderstood
    where the cause lives; it is a model that wrote the plan and stopped.

    Matching is on the literal registered name, which is how the model refers
    to them in these answers. Prose about "the logs" is not enough to act on:
    it names no call, so there is nothing to insist on and the guess would
    fire on answers that are already complete.
    """
    return [
        name for name in TOOLS
        if name in answer and name not in called
    ]


# Sent back when an answer names a tool it never called. Deliberately says
# nothing about pods, logs or what the cause might be: it restates what the
# model already decided was the next step and points out that it can take it
# itself. A hint about where to look would be this file guessing at the
# diagnosis, and would make every answer after it suspect.
#
# It re-states the question and insists on keeping what was already found,
# because the first version did neither and cost a case that had been passing.
# Measured on cluster_wide_scan, which asks what is broken anywhere: the model
# listed the three broken workloads and offered describe_pod for detail, was
# sent back, drilled into one pod and then answered about that pod alone --
# losing the three names it had already reported. 3/3 before, 2/4 after. The
# last thing in its context by then is one pod's detail and an instruction to
# call a tool and answer, so a model that answers exactly that is not being
# unreasonable. The question has to be put back in front of it.
# Statuses whose cause is not in the pod's own status block. The kubelet puts
# the reason in an Event and nowhere else, so describe_pod returns a pod that
# is plainly stuck and silent about why.
#
# Measured 2026-08-15 and again live on 2026-08-18: a ConfigMap referenced by a
# VOLUME leaves the pod in ContainerCreating with NO waiting message at all --
# the name appears only in a FailedMount event. Asked why such a pod was stuck,
# the agent read describe_pod, never called get_pod_events, and hedged: "these
# ConfigMaps may not exist ... or are misreferenced". The right answer, from
# evidence it never collected, was `configmap "nginx-conf" not found`.
#
# An env-var reference is the opposite: the name IS in the waiting message, so
# describe_pod is sufficient and requiring events would be noise. The split is
# by volume-versus-env, not by ConfigMap-versus-Secret.
EVIDENCE_IN_EVENTS = (
    "containercreating",
    "podinitializing",
    "failedmount",
    "createcontainerconfigerror",
)

# A crashing pod reports the symptom in its status and the cause in its logs.
# "Do not stop at the status name" is in the system prompt and mostly works;
# this is the floor under it. Narrow on purpose: it fires only when the logs
# themselves were not read for that pod, so the ordinary
# describe_pod -> get_pod_logs chain never sees it. Reading Events instead is
# not enough and used to be treated as though it were -- see evidence_gap.
# OOMKilled is deliberately absent. The kernel killed the container for
# exceeding a limit describe_pod already reports, so the status IS the cause
# and the logs usually end mid-sentence. Requiring them there would spend a
# round on every ordinary memory diagnosis -- the same reason the events
# policy ignores statuses that explain themselves.
EVIDENCE_IN_LOGS = ("crashloopbackoff", "error")

def uncovered_workloads(outputs, answer):
    """
    Workloads a `scan_cluster` result reported that the answer never names.

    Measured 2026-08-21, and the mechanism is list length rather than which
    workloads or where they sit. Holding identity and order constant by
    permuting, and varying only how many entries the tool returned:

        entries   complete summaries   entries dropped
              5             8/8                 0/40
             10             0/8                13/80
             20             0/8                47/160

    Fisher exact p=0.000155 for 5 against either longer arm. Drops spread
    evenly over fault class and position, so the two hypotheses that survived
    earlier rounds die once count is the variable being held. Latency is flat
    across the arms -- 145s, 144s, 169s -- so this is not the model running
    out of time; it produces an answer just as fast and leaves entries out.

    Generous about what counts as named, deliberately: the defect is an entry
    vanishing entirely, so naming the example pod instead of its workload is a
    worse answer and not a dropped one. A strict check would fold two defects
    into one number.

    Returns keys in the order the tool reported them, so the caller can name
    the missing ones in an order the reader can follow back to the scan.
    """
    lowered = (answer or "").lower()
    missing = []
    for text in outputs:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            continue
        # A scan result is a mapping of "namespace/workload" to a projection.
        # An error, a bare {"result": ...} message, or any other tool's output
        # is not one, and treating it as an empty scan would report every
        # workload as covered.
        if not isinstance(parsed, dict) or "error" in parsed or "result" in parsed:
            continue
        for key, value in parsed.items():
            if key == "_truncated" or not isinstance(value, dict):
                continue
            if "status" not in value or "pods" not in value:
                continue
            _, _, workload = key.partition("/")
            workload = workload.partition(":")[0]
            example = value.get("example") or ""
            if any(form and form.lower() in lowered
                   for form in (workload, key, example)):
                continue
            if key not in missing:
                missing.append(key)
    return missing


COVERAGE_POLICY = (
    "Your answer leaves out {count} of the workloads the scan reported: "
    "{missing}. A cluster summary that silently drops entries is worse than a "
    "long one -- the reader has no way to tell the difference between a "
    "workload that is fine and one you did not mention. Answer this question "
    "again, in full:\n\n{question}\n\nCover every workload the scan "
    "returned, including the ones above and everything you already reported. "
    "One line each is enough; brevity per workload is fine, leaving a "
    "workload out is not."
)


LOGS_POLICY = (
    "You have {pod}'s status and nothing it wrote. {status} is the symptom, "
    "not the cause -- the reason the container exited is in its logs. Call "
    "get_pod_logs on {pod} in namespace {namespace}, read what it printed, and "
    "then answer this question in full:\n\n{question}\n\nKeep every finding "
    "you already have. If the logs are empty, say so rather than proposing a "
    "cause they do not show."
)

EVIDENCE_POLICY = (
    "The evidence for a {status} pod is not in its status block -- the kubelet "
    "puts the reason in an Event and nowhere else, which is why {pod} looks "
    "stuck and says nothing about why. Call get_pod_events on {pod} in "
    "namespace {namespace}, read the reason it reports, and then answer this "
    "question in full:\n\n{question}\n\nKeep every finding you already have "
    "and add to it. If the events do not establish the cause either, say that "
    "plainly rather than proposing one."
)


def _looks_like_a_target(word):
    """
    Whether a word in the question names a Kubernetes object rather than
    ordinary English. Hyphenated or digit-bearing, the same shape test the
    grounding entity check uses.
    """
    word = word.strip(".,;:?!'\"`")
    return len(word) > 3 and any(ch == "-" or ch.isdigit() for ch in word)


def workload_prefix(pod):
    """
    The names a question might use for this pod's workload.

    A Deployment pod carries two generated suffixes
    (`crasher-5964d99948-9g8vg` -> `crasher`) and a DaemonSet pod carries one
    (`log-shipper-8gnqk` -> `log-shipper`). Nothing in the name says which, so
    both candidates are returned and either may match -- guessing one trim
    turned `log-shipper-8gnqk` into `log`, which matches nothing a person
    would ever type.
    """
    parts = pod.split("-")
    candidates = {pod}
    if len(parts) >= 2:
        candidates.add("-".join(parts[:-1]))
    if len(parts) >= 3:
        candidates.add("-".join(parts[:-2]))
    return candidates


def _reported_pods(trace, outputs):
    """
    Every (pod, namespace, status) a tool result described, whatever its shape.

    Three shapes, and missing two of them was a real hole: reading only
    documents with a top-level "pod" key meant a run that answered straight
    from list_pods was invisible to the policy, and answering straight from a
    listing is the commonest way to stop early. Observed live 2026-08-19 --
    asked for the crasher pod's status, the model called list_pods, reported
    "Error with 4 restarts" and stopped, and the gap detector saw nothing.

    trace and outputs are appended in lockstep, so the call's arguments supply
    the namespace that a listing result does not carry. Paired by index with a
    fallback rather than zipped, because a strict zip silently drops every
    result once the two lists disagree by one -- which is a bug that would
    disable the policy rather than announce itself.
    """
    seen = []
    for index, output in enumerate(outputs):
        call = trace[index] if index < len(trace) else {}
        try:
            data = json.loads(output)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        namespace = (call.get("arguments") or {}).get("namespace", "default")

        # describe_pod / get_pod_logs: one document about one pod.
        if isinstance(data.get("pod"), str):
            seen.append((data["pod"], data.get("namespace", namespace),
                         str(data.get("status", ""))))
            continue

        # list_pods / scan_cluster: a mapping of name to detail.
        for key, value in data.items():
            if not isinstance(value, dict) or str(key).startswith("_"):
                continue
            status = str(value.get("status", ""))
            if not status:
                continue
            if "example" in value:
                # scan_cluster keys are "namespace/workload"; the pod to look
                # at is the example it names.
                where = key.split("/")[0] if "/" in key else namespace
                seen.append((value["example"], where, status))
            else:
                seen.append((key, namespace, status))
    return seen


# Termination reasons that ARE the cause, so a run holding one needs no logs.
# The kernel killed the container for exceeding a limit describe_pod reports
# in the same document, and the container's last line is whatever it happened
# to be printing when it died -- for the memory-hog fixture, `stress` output.
# Sending that run for logs spends its one policy to learn nothing.
SELF_EXPLANATORY_TERMINATION = ("oomkilled",)


def _terminated_for(outputs, pod):
    """
    The last termination reasons any result recorded for this pod, lowercased.

    Read from the evidence rather than from the status string, because the two
    disagree and the status is the one that lies. The same OOM-killed pod
    reports `OOMKilled` when list_pods catches it mid-crash and
    `CrashLoopBackOff` when it catches it mid-backoff -- a timing artefact of
    which phase the kubelet was in, and both spellings are recorded for the
    same memory-hog pod inside one eval set (think-OFF-16cases-n3, 2026-08-22).
    describe_pod carries last_termination.reason in both, and that is the
    stable fact.

    This is why the OOMKilled exclusion could not stay a status check. It was
    written as one, it reads correctly, and it leaks whenever the pod is
    sampled in backoff -- which is most of the time, because backoff is where
    a crashlooping pod spends most of its life.
    """
    found = set()
    for output in outputs:
        try:
            data = json.loads(output)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("pod") != pod:
            continue
        for container in (data.get("containers") or {}).values():
            if not isinstance(container, dict):
                continue
            reason = (container.get("last_termination") or {}).get("reason")
            if reason:
                found.add(str(reason).lower())
    return found


def evidence_gap(trace, outputs, question=""):
    """
    A pod whose cause is provably not in its status block, that this run never
    went and read.

    Returns ("events"|"logs", pod, namespace, status) or None. Deterministic,
    and deliberately narrow: it fires on the statuses whose cause is provably
    elsewhere, and only when the tool holding that cause was not called for
    that pod. The model still chooses its own path -- this catches the one gap
    where stopping early is guaranteed to produce a guess.

    Which tool closes which gap is the whole design, and getting it wrong in
    either direction costs a round or a diagnosis. Events hold the cause for a
    container that never started; logs hold it for one that started and
    exited. They are not interchangeable, and treating them as though they
    were is what let a CrashLoopBackOff run answer from a stale
    FailedScheduling event.
    """
    def called(tool):
        return {
            (c["arguments"].get("name"), c["arguments"].get("namespace", "default"))
            for c in trace if c["name"] == tool
        }

    asked = called("get_pod_events")
    read = called("get_pod_logs")
    reported = _reported_pods(trace, outputs)

    # Prefer a pod the question actually asked about. Observed live
    # 2026-08-19: asked about `crasher`, the model listed every unhealthy pod
    # in the namespace and this policy pointed at the first one it found --
    # log-shipper -- which sent the run to collect evidence about a workload
    # nobody had mentioned. The answer came back "The crasher pod
    # log-shipper-8gnqk", which is the wrong-entity failure this project has
    # spent months removing, reintroduced by its own safety net.
    #
    # Substring on the pod name, since a question names the workload
    # (`crasher`) and the listing keys the pod (`crasher-5964d99948-9g8vg`).
    lowered_question = (question or "").lower()
    if lowered_question:
        named = [
            row for row in reported
            if any(part and part.lower() in lowered_question
                   for part in workload_prefix(row[0]))
        ]
        if named:
            reported = named
        elif any(_looks_like_a_target(word) for word in lowered_question.split()):
            # The question names something and none of it is here. Firing now
            # would send the run to collect evidence about whichever unrelated
            # pod happened to be listed first, and the answer comes back about
            # that pod -- which is how "is correctly-configured unhealthy?"
            # became a diagnosis of missing-configmap-key on 2 of 3 runs.
            # Measured 2026-08-19; the policy caused the very wrong-entity
            # failure it was hardened against.
            return None

    # Events first: an unmountable volume means the container never started,
    # so there are no logs to read and sending the run for them would spend
    # its one policy on an empty result.
    for pod, namespace, status in reported:
        lowered = status.lower()
        if not any(marker in lowered for marker in EVIDENCE_IN_EVENTS):
            continue
        if (pod, namespace) in asked:
            continue
        return "events", pod, namespace, status

    # Logs second -- and reading Events does NOT close this one. That is the
    # correction, and it is measured rather than reasoned: in
    # results/seam-regression-n1.json (2026-08-23) `crashloop_root_cause`
    # called list_pods, describe_pod and get_pod_events, never get_pod_logs,
    # and recorded `policies: 0`. The events it read were "Back-off restarting
    # failed container" -- the status restated -- and a seven-minute-old
    # FailedScheduling left over from start-up. The answer came back naming an
    # "untolerated taint" as the cause of the crash. So for a container that
    # started and exited, Events are not merely insufficient evidence: on that
    # run they supplied a wrong cause, which is the failure this policy exists
    # to prevent.
    #
    # The exception is a pod that qualifies for BOTH lists, which is not
    # hypothetical: "error" is a substring of CreateContainerConfigError. There
    # the container never started, its reason is in the Event and there are no
    # logs to read, so events reading closes the gap and sending the run for
    # logs would spend its one policy on an empty result. That is the case the
    # original `or ... in asked` was protecting, and it keeps its protection.
    for pod, namespace, status in reported:
        lowered = status.lower()
        if not any(marker in lowered for marker in EVIDENCE_IN_LOGS):
            continue
        if (pod, namespace) in read:
            continue
        if (pod, namespace) in asked and any(
                marker in lowered for marker in EVIDENCE_IN_EVENTS):
            continue
        if _terminated_for(outputs, pod) & set(SELF_EXPLANATORY_TERMINATION):
            continue
        return "logs", pod, namespace, status

    return None


COVERAGE_NOTE = (
    "**Also reported by the scan, not covered above ({count}):**\n{missing}"
)


NUDGE = (
    "You wrote that {tools} should be run, but you did not run {them}. You "
    "have {them} available now, and the person asking cannot run tools -- an "
    "answer that ends in a next step is a plan, not a diagnosis. Call {them}, "
    "read what comes back, and then answer this question in full:\n\n"
    "{question}\n\n"
    "Keep every finding you have already reported and add to it -- narrowing "
    "to whatever you looked at last would lose the rest. If you already have "
    "everything you need, answer without calling anything."
)


def stream(question, model=MODEL, think=None, prefetched=None, target=None):
    """
    The loop, with one audit record per investigation.

    A thin wrapper rather than a `finally` threaded through the body below,
    because the runs worth auditing most are the ones that do not reach the
    answer event: a model that raised, a deadline that fired, a caller that
    closed the generator half way through. `finally` catches all three,
    including GeneratorExit, which is the one an `except Exception` misses.

    Every surface reaches the loop through here -- CLI, REST, MCP, controller,
    console, Slack -- so this is the only place the hook has to exist. Who is
    asking arrives on a ContextVar the surface sets; see audit.actor().
    """
    record = audit.begin(question, model)
    try:
        for event in _stream(question, model, think, prefetched, target):
            record.observe(event)
            yield event
    except GeneratorExit:
        # The caller walked away -- a browser tab closed mid-investigation,
        # an HTTP client that hung up. Recorded as abandoned rather than as an
        # error, because an audit trail that files those as failures has
        # people chasing incidents that did not happen. The run still read
        # whatever it read by then, which is the part worth having.
        record.abandoned()
        raise
    except BaseException as exc:
        record.failed(exc)
        raise
    finally:
        record.emit()


def _stream(question, model=MODEL, think=None, prefetched=None, target=None):
    """
    Run the loop, yielding each step as it happens.

    Every event is a dict with a "type":

        {"type": "tool_call",   "name":..., "arguments":...}
        {"type": "tool_result", "name":..., "result": <json str>, "duration_ms":...}
        {"type": "answer",      "answer":..., "tool_calls":[...],
                                "confidence":..., "unverified":[...]}

    Every event also carries "run_id", and the answer carries "target": one
    investigation's artifacts are identifiable as its own rather than by having
    arrived most recently.

    Exactly one "answer" event is emitted, last. ask() is this drained to
    completion, so the two cannot drift.

    This exists because a diagnosis takes tens of seconds and the tool chain is
    the product: a caller handed only the final answer has nothing to show for
    minutes, and showing the chain live is what distinguishes this from waiting
    on a spinner. The CLI's --verbose trace, the browser UI and any streaming
    endpoint are all consumers of these events.
    """
    think = THINK if think is None else think
    # One identity per investigation, minted before anything is collected.
    # Every artifact this run produces is stamped with it, so "is this the
    # evidence for the answer above it?" is a comparison rather than an
    # assumption -- which is the only way a caller tells a stale panel from a
    # fresh one.
    run_id = uuid.uuid4().hex[:12]
    # The entity this question is about, fixed before the first round and not
    # revisable by the model. It may choose how to investigate; it may not
    # change what it is investigating. See targeting.py.
    # An explicit target from a surface that made a selection is authoritative
    # and is never second-guessed by re-reading the prompt. See scoped_target().
    target = target or targeting.target_of(question)
    if not (target or {}).get("name"):
        # The question names something and never says what kind of thing it
        # is -- "Why is crasher-svc unreachable?". Guessing from the text
        # alone would be worse than not guessing: a target the cluster has
        # never heard of rewrites every call and breaks the run. So the guess
        # is checked against the cluster first, with the same read-only tools
        # the agent uses, and only a name that resolves becomes a target.
        guessed = targeting.confirm(
            targeting.candidate_names(question), _resolve_entity
        )
        if guessed:
            # A namespace the question stated wins over the one the lookup
            # found: the user may be asking about one of two same-named things.
            guessed["namespace"] = (
                (target or {}).get("namespace") or guessed.get("namespace")
            )
            target = guessed
    if target:
        # Prefixed keys: `name` is reserved on LogRecord and logging raises
        # KeyError rather than overwriting it. Caught by the suite only
        # because another test enables INFO -- which is how the controller
        # runs, so this would have crashed there and nowhere else.
        log.info("investigation_target", extra={
            "target_kind": target["kind"],
            "target_name": target["name"],
            "target_namespace": target["namespace"],
        })
    prefetched = list(prefetched or [])
    trace = []
    # Seeded with the prefetched results so grounding treats them as
    # measurements. They ARE measurements -- a tool produced them against the
    # live cluster -- and the alternative is an answer quoting the one piece of
    # evidence that survived being marked unverified for doing so.
    outputs = [item["result"] for item in prefetched]
    # The same results as `outputs`, carrying which tool produced each one so
    # a verified claim can cite the tool and field it came from rather than
    # "something in the transcript said this". See grounding.records.
    evidence = [
        {"id": f"tool-{i}", "tool": item.get("name"), "result": item["result"]}
        for i, item in enumerate(prefetched, 1)
    ]

    content = question + (_prefetched_block(prefetched) if prefetched else "")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]

    for item in prefetched:
        # Shown in the chain, flagged, so a reader can tell what the model went
        # and got from what it was handed.
        trace.append({
            "name": item["name"],
            "arguments": item.get("arguments") or {},
            "prefetched": True,
        })

    # Where the wall clock actually went. Tool calls were already timed; the
    # model rounds were not, so a run that took 2217s against a 62s median
    # could not be attributed to anything -- and both hypotheses that died
    # (weights unloading, machine contention) died on exactly that ambiguity.
    # Per round rather than a total, because "one round hung" and "every round
    # was slow" are different faults and the sum cannot tell them apart.
    model_ms = 0.0
    tool_ms = 0.0
    round_ms = []
    nudges = 0
    policies = 0
    coverage = 0

    # Two clocks, deliberately. perf_counter is monotonic and stops while the
    # machine is asleep; time.time() does not. Their difference over the same
    # interval is how long the host was suspended mid-run -- which is the
    # difference between "the model hung" and "the laptop napped", and those
    # were indistinguishable in every stall this project has recorded.
    began_wall = time.time()
    began_mono = time.perf_counter()

    def remaining():
        """
        Seconds of budget left, on the monotonic clock.

        Monotonic deliberately: a host that suspends mid-investigation did not
        spend that time working, and killing a run for a nap the wall clock
        recorded would be this project's oldest measurement mistake wired into
        a control path.
        """
        return INVESTIGATION_BUDGET - (time.perf_counter() - began_mono)

    def elapsed():
        wall_ms = (time.time() - began_wall) * 1000
        mono_ms = (time.perf_counter() - began_mono) * 1000
        return wall_ms, max(wall_ms - mono_ms, 0.0)

    def terminated(reason, text):
        """
        The terminal event for a run that stopped without the model answering.

        One builder for both non-answer exits so they carry the same shape: a
        consumer that special-cases the fields present on one of them is a
        consumer that breaks when the other fires.
        """
        telemetry.INVESTIGATIONS.inc(outcome=reason)
        wall_ms, slept_ms = elapsed()
        telemetry.INVESTIGATION_DURATION.observe(
            max(wall_ms - slept_ms, 0.0) / 1000)
        log.warning("investigation_terminated", extra={
            "reason": reason,
            "rounds": len(round_ms),
            "budget_s": INVESTIGATION_BUDGET,
            "elapsed_s": round((time.perf_counter() - began_mono), 1),
            "tool_calls": len(trace),
        })
        return {
            "type": "answer",
            "run_id": run_id,
            "target": target,
            "answer": text,
            # Why this run stopped, as data rather than as prose a caller has
            # to pattern-match. `deadline_exceeded` and `max_rounds` are
            # different operational problems and only one of them is about the
            # model being indecisive.
            "termination": reason,
            "budget_s": INVESTIGATION_BUDGET,
            "tool_calls": trace,
            "evidence": evidence,
            "draft": None,
            "timing": _timing(model_ms, tool_ms, round_ms, *elapsed()),
            "nudges": nudges,
            "policies": policies,
            "coverage": coverage,
            "confidence": "ungrounded",
            "unverified": [],
        }

    for round_index in range(MAX_ROUNDS):
        if remaining() <= 0:
            yield terminated(
                "deadline_exceeded",
                f"Gave up after {INVESTIGATION_BUDGET}s: the investigation "
                f"budget was exhausted before an answer was reached."
            )
            return

        began = time.perf_counter()
        # The model call is capped by whatever is left, so the deadline cannot
        # be overrun by a provider timeout that outlives it.
        #
        # And the cap has to be caught. Clamping the provider timeout to the
        # remaining budget means the budget expiring *during* a call surfaces
        # as that provider's timeout exception -- so without this the deadline
        # ended runs by raising ReadTimeout out of the generator instead of
        # terminating them with a reason, which is a worse outcome than the one
        # it was added to prevent. Found by measuring the fix rather than by
        # writing it.
        #
        # Only when the budget is actually gone. A provider that times out with
        # budget still on the clock is a provider failure and still propagates,
        # exactly as it did before.
        budget_at_call = remaining()
        try:
            reply, think = _chat(model, messages, think, timeout=budget_at_call)
        except Exception:
            # Did this call run out the clock it was given, or did the provider
            # break inside it? The call's ceiling is min(remaining, provider
            # timeout), so a failure after roughly that long is the deadline
            # arriving and anything sooner is a real provider failure, which
            # still propagates exactly as before.
            #
            # Compared against the budget rather than against remaining() > 0,
            # because those two race at precisely this boundary: measured
            # 2026-08-23, the clamped timeout fired and remaining() came back
            # a thousandth of a second positive, so the run re-raised
            # ReadTimeout instead of terminating with a reason.
            spent = time.perf_counter() - began
            if spent < budget_at_call - 0.5:
                raise
            model_ms += (time.perf_counter() - began) * 1000
            round_ms.append(round((time.perf_counter() - began) * 1000, 1))
            yield terminated(
                "deadline_exceeded",
                f"Gave up after {INVESTIGATION_BUDGET}s: the investigation "
                f"budget was exhausted waiting for the model."
            )
            return
        this_round = (time.perf_counter() - began) * 1000
        model_ms += this_round
        round_ms.append(round(this_round, 1))

        # The provider's own assistant object where it has one: rebuilding it
        # as a dict drops fields the server round-trips.
        messages.append(_backend().assistant_message(reply))

        calls = reply.tool_calls
        if not calls:
            answer = (reply.content or "").strip()

            # A run that ends by naming a tool it never called has stopped one
            # step short of the thing it was asked for. Send it back once,
            # rather than returning the plan as if it were a diagnosis.
            # Never on the last round: there would be no round left to call
            # the tool in or to answer from, so the only thing a nudge could
            # achieve there is trading a usable answer for "gave up".
            rounds_left = MAX_ROUNDS - round_index - 1
            skipped = named_but_not_called(answer, {c["name"] for c in trace})
            if skipped and nudges < MAX_NUDGES and rounds_left >= 2:
                nudges += 1
                them = "it" if len(skipped) == 1 else "them"
                log.info(
                    "nudged_for_named_tools",
                    extra={"tools": skipped, "nudge": nudges},
                )
                messages.append({
                    "role": "user",
                    "content": NUDGE.format(
                        tools=", ".join(skipped), them=them, question=question
                    ),
                })
                continue

            # Deterministic evidence policy, checked after the tool-naming
            # nudge because that one is about what the model SAID and this one
            # is about what the cluster REQUIRES. A pod stuck in
            # ContainerCreating has its reason in an Event and nowhere else, so
            # answering without reading events is guessing however confident
            # the prose sounds. Same budget as the nudge: once per run, never
            # on the last rounds.
            gap = evidence_gap(trace, outputs, question)
            if gap and policies < MAX_NUDGES and rounds_left >= 2:
                kind, pod, namespace, status = gap
                policies += 1
                log.info(
                    "evidence_policy_applied",
                    extra={"policy": kind, "pod": pod,
                           "namespace": namespace, "status": status},
                )
                template = EVIDENCE_POLICY if kind == "events" else LOGS_POLICY
                messages.append({
                    "role": "user",
                    "content": template.format(
                        status=status, pod=pod, namespace=namespace,
                        question=question,
                    ),
                })
                continue

            # Coverage, checked last of the three because the other two are
            # about going and getting evidence and this one is about
            # reporting what is already in hand. Same budget, same
            # never-on-the-last-rounds rule.
            uncovered = uncovered_workloads(outputs, answer)
            if uncovered and coverage < MAX_NUDGES and rounds_left >= 2:
                coverage += 1
                log.info(
                    "coverage_policy_applied",
                    extra={"missing": uncovered, "count": len(uncovered)},
                )
                messages.append({
                    "role": "user",
                    "content": COVERAGE_POLICY.format(
                        count=len(uncovered),
                        missing=", ".join(uncovered),
                        question=question,
                    ),
                })
                continue

            # The backstop. The re-ask above helps and does not guarantee:
            # at twenty entries a run named as few as 7, and asking again can
            # trade one omission for another rather than closing the set. A
            # summary that silently drops workloads is the defect, so when the
            # model has had its round and entries are still missing, they are
            # appended as data. Deliberately terse and clearly separated --
            # this is a completeness guarantee, not a second diagnosis, and it
            # must not read as though the model investigated them.
            #
            # Precedent: annotate() already appends an evidence audit the
            # model did not write, for the same reason -- the reader has to be
            # able to trust what the answer does not say.
            still_missing = uncovered_workloads(outputs, answer)
            if still_missing:
                log.info("coverage_appended",
                         extra={"missing": still_missing,
                                "count": len(still_missing)})
                answer = answer.rstrip() + "\n\n" + COVERAGE_NOTE.format(
                    count=len(still_missing),
                    missing="\n".join(f"- {key}" for key in still_missing),
                )

            verdict = grounding.check(answer, evidence)
            wall_ms, slept_ms = elapsed()
            telemetry.INVESTIGATIONS.inc(
                outcome=verdict.get("confidence", "unknown"))
            # Wall clock minus the nap, which is the only honest duration on a
            # laptop that suspends: this project has recorded a 725s run with
            # 548s of sleep inside it, and a histogram that counts the nap as
            # model latency reports a p95 that never happened.
            telemetry.INVESTIGATION_DURATION.observe(
                max(wall_ms - slept_ms, 0.0) / 1000)
            # Verify, then rewrite. Auditing alone left "the pod has a 512Mi
            # memory limit" standing in the prose with a correction printed
            # underneath it, and a reader skimming for the number found the
            # fabricated one. The draft is rewritten at the value: a measured
            # counterpart replaces it, or it is marked so it cannot be read as
            # a measurement.
            verified, edits = grounding.verify(answer, verdict, evidence)
            if edits:
                log.info("rewrote_unsupported_claims", extra={"edits": edits})

            yield {
                "type": "answer",
                # Which investigation this is, and what it was about. Read
                # together they are an acceptance test a UI can actually run:
                # the target it selected must equal the target that came back.
                "run_id": run_id,
                "target": target,
                "answer": grounding.annotate(verified, verdict),
                # What the answer establishes, split by how well: observations
                # carry the result id and field they came from, inferences do
                # not, and unknowns are what the run stated and could not
                # support. Built from the verified claims rather than asked of
                # the model, which could invent a citation as easily as a
                # figure.
                "rca": grounding.contract(verdict, edits),
                "rewrites": edits,
                # How many times a deterministic policy sent this run back for
                # evidence the status block provably does not contain. Separate
                # from nudges: one is the model stopping short of a tool it
                # named, the other is the cluster requiring a tool it did not.
                "policies": policies,
                # How many times the run was sent back for leaving workloads
                # out of its own summary. Separate from policies, which is
                # about evidence the run never gathered: this one is about
                # evidence it had and did not report.
                "coverage": coverage,
                "tool_calls": trace,
                # The measurements themselves, in the records() shape that was
                # handed to grounding.check() -- ids, tool names and the raw
                # projected result, prefetched entries included and numbered as
                # the checker numbered them. `tool_calls` says what was asked;
                # this says what came back, and without it a grounding verdict
                # can be read a year later but never re-derived. ask() drops it
                # unless the caller asks, so no surface pays for it by default.
                "evidence": evidence,
                # The answer as the checker saw it: the model's draft, before
                # verify() rewrote its unsupported values and annotate() added
                # the markers. Recorded with the evidence because the two are
                # only useful together -- `answer` below has been through both
                # transformations, so check(answer, evidence) cannot reproduce
                # the verdict this run recorded. Measured 2026-08-21 on five
                # live runs: two came back with a different unverified list,
                # one of them having lost a claim and gained two that are
                # artefacts of the audit footer's own digits.
                #
                # Never put on a wire. verify() exists so that a reader
                # skimming the prose for a number finds the measured one, and
                # handing back the draft would undo exactly that.
                "draft": answer,
                "timing": _timing(model_ms, tool_ms, round_ms, *elapsed()),
                # Recorded, not just acted on: without it a run that called
                # get_pod_logs cannot be told apart from one that had to be
                # sent back for it, and the guard's own effect is unmeasurable.
                "nudges": nudges,
                **verdict,
            }
            return

        for call in calls:
            if remaining() <= 0:
                # Mid-round. A model that asked for eight tools can spend
                # 8 x K8S_TIMEOUT here without the round ever returning, so
                # the check belongs inside the loop and not only above it.
                yield terminated(
                    "deadline_exceeded",
                    f"Gave up after {INVESTIGATION_BUDGET}s: the investigation "
                    f"budget was exhausted while collecting evidence."
                )
                return

            name = call.name
            arguments = dict(call.arguments)

            # Hold the call to the target before it runs. A call that can carry
            # the scope is rewritten; one that names a different entity
            # outright is refused, because no rewrite could be honest about it.
            arguments, violation = targeting.enforce(target, name, arguments)
            if violation:
                log.info("scope_violation", extra={
                    "requested": target,
                    "attempted_tool": violation["tool"],
                    "reason": violation["reason"],
                    "action": violation["action"],
                })

            trace.append({"name": name, "arguments": arguments,
                          **({"scope": violation} if violation else {})})

            yield {"type": "tool_call", "run_id": run_id, "name": name,
                   "arguments": arguments,
                   **({"scope_violation": violation} if violation else {})}

            started = time.perf_counter()
            if violation and violation["action"] == "refused":
                # Handed back as data, like any other tool failure, so the loop
                # survives and the model is told what it may look at.
                output = json.dumps({"error":
                    f"{violation['reason']}. This question is about "
                    f"{target['name']}; investigate that."})
            else:
                output = _run_tool(name, arguments)
            outputs.append(output)
            evidence.append(
                {"id": f"tool-{len(outputs)}", "tool": name, "result": output}
            )
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            tool_ms += duration_ms
            telemetry.TOOL_CALLS.inc(tool=name, outcome=_outcome(output))

            log.info(
                "tool_call",
                extra={
                    "tool": name,
                    "arguments": arguments,
                    "duration_ms": duration_ms,
                    "result_chars": len(output),
                },
            )
            # Shaped by the backend: Ollama matches a result to its call by
            # tool name, OpenAI by tool_call_id. The loop must not know.
            messages.append(_backend().tool_message(call, output))

            yield {
                "type": "tool_result",
                "run_id": run_id,
                "name": name,
                "result": output,
                "duration_ms": duration_ms,
            }

    yield terminated("max_rounds",
                     f"Gave up after {MAX_ROUNDS} rounds of tool calls.")


def ask(question, model=MODEL, verbose=False, think=None, prefetched=None,
        evidence=False, target=None):
    """
    Answer a question about this host, letting the model call collectors.

    think defaults to True: without it qwen3 tends to answer multi-part
    questions from only the first tool it calls and invent the rest. It costs
    a few seconds per round. Returns

        {"answer": str,
         "tool_calls": [{"name":..., "arguments":...}],
         "confidence": "grounded" | "partial" | "ungrounded",
         "unverified": [claims not found in any tool result]}

    evidence=True adds the checker's two inputs: "evidence", the tool results
    in grounding.records() shape, and "draft", the model's answer before
    verify() rewrote it and annotate() marked it up. Both or neither --
    "answer" has been through both transformations, so replaying the check
    against it reproduces a different verdict.

    Off by default. Every other caller of this function returns its result
    over a wire -- REST, MCP, Slack -- and a projected scan of a busy cluster
    is a large thing to put in a reply nobody asked for, while the draft is
    the text still carrying the unsupported figures that verify() exists to
    take out of the reader's way.

    The evals turn it on: a recorded run whose tool output was thrown away
    cannot be re-scored when the checker changes, and re-running to find out
    costs an hour and asks a non-deterministic model a second time.

    See grounding.py for what confidence means and why it is a lint rather
    than a gate. Callers that need the steps as they happen want stream().
    """
    answer = None
    think = THINK if think is None else think

    for event in stream(question, model=model, think=think,
                        prefetched=prefetched, target=target):
        if verbose and event["type"] == "tool_call":
            print(f"  -> {event['name']}({event['arguments']})", file=sys.stderr)
        if event["type"] == "answer":
            answer = event

    # "type" is a routing field for stream() consumers, not part of this
    # function's long-standing contract. "evidence" and "draft" are opt-in:
    # one for size, the other because it is the un-rewritten text.
    dropped = {"type"} if evidence else {"type", "evidence", "draft"}
    return {key: value for key, value in answer.items() if key not in dropped}


def _report_unverified(result):
    """
    Surface unsupported claims rather than leaving them to be spotted by eye.

    stderr, so piping the answer stays clean. Every path that prints an answer
    has to call this: a diagnosis shown without its confidence is the failure
    mode grounding.py exists to prevent.
    """
    if result["unverified"]:
        print(
            f"\n[{result['confidence']}] not found in any tool result: "
            + ", ".join(result["unverified"]),
            file=sys.stderr,
        )


def scan(explain=0):
    """
    Print a cluster-wide scan, and optionally diagnose the worst findings.

    The listing itself never touches the model: it is one API call and returns
    in under a second, which is what makes it usable as a first look. Only
    --explain pays for inference, and only for the workloads asked about.
    """
    findings = scan_cluster()

    if "error" in findings:
        print(findings["error"], file=sys.stderr)
        return 1
    if "result" in findings:
        print(findings["result"])
        return 0

    truncated = findings.pop("_truncated", None)
    width = max(len(key) for key in findings)
    for key, entry in findings.items():
        print(f"{key:<{width}}  {entry['status']:<20} {entry['pods']} pod(s)")
    if truncated:
        print(f"\n{truncated}")

    for key, entry in list(findings.items())[:explain]:
        # Keys are "namespace/workload", or "namespace/workload:fault" when one
        # workload carries two faults at once.
        namespace = key.split("/", 1)[0]
        print(f"\n--- {key} ---")
        # Read the logs before the diagnosis rather than during it. For a
        # CronJob the example pod is routinely collected mid-chain, and the
        # model then has nothing to reason from -- see capture_pod_logs.
        result = ask(
            scoped_question(
                "Find the root cause and say what should change.",
                key.split(":", 1)[0],
                namespace,
                entry["example"],
            ),
            verbose=True,
            prefetched=capture_pod_logs(entry["example"], namespace),
        )
        print(result["answer"])
        _report_unverified(result)

    return 0


if __name__ == "__main__":
    # The CLI has a real user and no authentication: whoever is at the
    # terminal already holds the kubeconfig, so the OS account is both the
    # honest answer and the only one available. Recorded as `os` rather than
    # dressed up as an authenticated identity, because an audit trail that
    # cannot distinguish "an OIDC session said so" from "this is whose shell
    # it was" is one nobody can rely on.
    try:
        import getpass

        audit.actor(getpass.getuser(), surface="cli", auth="os")
    except Exception:
        audit.actor("unknown", surface="cli", auth="os")

    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        raise SystemExit(1)

    if sys.argv[1] == "--scan":
        rest = sys.argv[2:]
        count = 0
        if "--explain" in rest:
            after = rest[rest.index("--explain") + 1 :]
            count = int(after[0]) if after and after[0].isdigit() else 3
        raise SystemExit(scan(explain=count))

    result = ask(" ".join(sys.argv[1:]), verbose=True)
    print(result["answer"])
    _report_unverified(result)
