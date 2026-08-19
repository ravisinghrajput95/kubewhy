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

import ollama

import grounding
import observability
from routers.platform_info import get_platform_info
from routers.system_info import get_system_info
from routers.process_info import get_processes
from routers.top_cpu import get_top_cpu_processes
from routers.top_memory import get_top_memory_processes
from routers.k8s_pods_info import (
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

# A hung model would otherwise block the loop forever, with the caller holding
# an HTTP request open. Generous, because a cold model load plus a deep chain
# is legitimately slow.
TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "300"))

# How long Ollama holds the weights in memory after a request. This is a
# SERVER-side setting, and the client library never reads it -- so the command
# CONTRIBUTING has documented for two months,
#
#     OLLAMA_KEEP_ALIVE=24h python evals/run_eval.py
#
# set a variable that nothing on the path read. Measured: unload the model,
# run one chat through this client with that variable exported, and the model
# comes back with an expiry five minutes out, the server default. Every
# latency figure this project has published was taken under a five-minute
# unload window by a person who believed they had disabled unloading.
#
# Forwarding it here makes the documented command true without requiring a
# restart of somebody's Ollama.app. None is excluded from the request body, so
# leaving it unset is byte-identical to not sending the field at all.
KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE") or None

# Thinking mode, on by default. Exposed so a benchmark can measure the
# trade-off rather than argue about it: qwen3's reasoning tokens are where
# essentially all of this agent's latency goes, and the question is what
# turning them off costs in accuracy. The default does not change.
THINK = os.getenv("TRIAGE_THINK", "true").lower() != "false"

log = logging.getLogger("triage.agent")

# Max tool-calling rounds before we give up. Guards against a model that
# keeps calling tools without ever settling on an answer.
MAX_ROUNDS = 8

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


def _chat(model, messages, think):
    """
    One model call, degrading gracefully when the model has no thinking mode.

    Only some models support it -- llama3.2 rejects the request outright with
    a 400 -- so rather than making thinking a hard requirement, fall back and
    let the caller decide whether the answers are good enough.
    """
    client = ollama.Client(timeout=TIMEOUT)
    try:
        return client.chat(
            model=model, messages=messages, tools=list(TOOLS.values()), think=think,
            keep_alive=KEEP_ALIVE,
        ), think
    except ollama.ResponseError as exc:
        if think and "does not support thinking" in str(exc):
            return client.chat(
                model=model, messages=messages, tools=list(TOOLS.values()), think=False,
                keep_alive=KEEP_ALIVE,
            ), False
        raise


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
    example = f" (for example pod {pod})" if pod else ""
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
# this is the floor under it. Narrow on purpose: it fires only when NOTHING
# was read for that pod beyond its status, so the ordinary
# describe_pod -> get_pod_logs chain never sees it.
# OOMKilled is deliberately absent. The kernel killed the container for
# exceeding a limit describe_pod already reports, so the status IS the cause
# and the logs usually end mid-sentence. Requiring them there would spend a
# round on every ordinary memory diagnosis -- the same reason the events
# policy ignores statuses that explain themselves.
EVIDENCE_IN_LOGS = ("crashloopbackoff", "error")

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


def evidence_gap(trace, outputs, question=""):
    """
    A pod whose cause lives in Events, that this run never asked Events about.

    Returns (pod, namespace, status) or None. Deterministic, and deliberately
    narrow: it fires on the statuses whose cause is provably not in the status
    block, and only when get_pod_events was not called for that pod. The model
    still chooses its own path -- this catches the one gap where stopping early
    is guaranteed to produce a guess.
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

    for pod, namespace, status in reported:
        lowered = status.lower()
        if not any(marker in lowered for marker in EVIDENCE_IN_LOGS):
            continue
        if (pod, namespace) in read or (pod, namespace) in asked:
            continue
        return "logs", pod, namespace, status

    return None


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


def stream(question, model=MODEL, think=None, prefetched=None):
    """
    Run the loop, yielding each step as it happens.

    Every event is a dict with a "type":

        {"type": "tool_call",   "name":..., "arguments":...}
        {"type": "tool_result", "name":..., "result": <json str>, "duration_ms":...}
        {"type": "answer",      "answer":..., "tool_calls":[...],
                                "confidence":..., "unverified":[...]}

    Exactly one "answer" event is emitted, last. ask() is this drained to
    completion, so the two cannot drift.

    This exists because a diagnosis takes tens of seconds and the tool chain is
    the product: a caller handed only the final answer has nothing to show for
    minutes, and showing the chain live is what distinguishes this from waiting
    on a spinner. The CLI's --verbose trace, the browser UI and any streaming
    endpoint are all consumers of these events.
    """
    think = THINK if think is None else think
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

    # Two clocks, deliberately. perf_counter is monotonic and stops while the
    # machine is asleep; time.time() does not. Their difference over the same
    # interval is how long the host was suspended mid-run -- which is the
    # difference between "the model hung" and "the laptop napped", and those
    # were indistinguishable in every stall this project has recorded.
    began_wall = time.time()
    began_mono = time.perf_counter()

    def elapsed():
        wall_ms = (time.time() - began_wall) * 1000
        mono_ms = (time.perf_counter() - began_mono) * 1000
        return wall_ms, max(wall_ms - mono_ms, 0.0)

    for round_index in range(MAX_ROUNDS):
        began = time.perf_counter()
        response, think = _chat(model, messages, think)
        this_round = (time.perf_counter() - began) * 1000
        model_ms += this_round
        round_ms.append(round(this_round, 1))

        message = response.message
        messages.append(message)

        calls = message.tool_calls
        if not calls:
            answer = (message.content or "").strip()

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

            verdict = grounding.check(answer, evidence)
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
                "tool_calls": trace,
                "timing": _timing(model_ms, tool_ms, round_ms, *elapsed()),
                # Recorded, not just acted on: without it a run that called
                # get_pod_logs cannot be told apart from one that had to be
                # sent back for it, and the guard's own effect is unmeasurable.
                "nudges": nudges,
                **verdict,
            }
            return

        for call in calls:
            name = call.function.name
            arguments = dict(call.function.arguments or {})
            trace.append({"name": name, "arguments": arguments})

            yield {"type": "tool_call", "name": name, "arguments": arguments}

            started = time.perf_counter()
            output = _run_tool(name, arguments)
            outputs.append(output)
            evidence.append(
                {"id": f"tool-{len(outputs)}", "tool": name, "result": output}
            )
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            tool_ms += duration_ms

            log.info(
                "tool_call",
                extra={
                    "tool": name,
                    "arguments": arguments,
                    "duration_ms": duration_ms,
                    "result_chars": len(output),
                },
            )
            messages.append(
                {"role": "tool", "tool_name": name, "content": output}
            )

            yield {
                "type": "tool_result",
                "name": name,
                "result": output,
                "duration_ms": duration_ms,
            }

    yield {
        "type": "answer",
        "answer": f"Gave up after {MAX_ROUNDS} rounds of tool calls.",
        "tool_calls": trace,
        "timing": _timing(model_ms, tool_ms, round_ms, *elapsed()),
        "nudges": nudges,
        "policies": policies,
        "confidence": "ungrounded",
        "unverified": [],
    }


def ask(question, model=MODEL, verbose=False, think=None, prefetched=None):
    """
    Answer a question about this host, letting the model call collectors.

    think defaults to True: without it qwen3 tends to answer multi-part
    questions from only the first tool it calls and invent the rest. It costs
    a few seconds per round. Returns

        {"answer": str,
         "tool_calls": [{"name":..., "arguments":...}],
         "confidence": "grounded" | "partial" | "ungrounded",
         "unverified": [claims not found in any tool result]}

    See grounding.py for what confidence means and why it is a lint rather
    than a gate. Callers that need the steps as they happen want stream().
    """
    answer = None
    think = THINK if think is None else think

    for event in stream(question, model=model, think=think, prefetched=prefetched):
        if verbose and event["type"] == "tool_call":
            print(f"  -> {event['name']}({event['arguments']})", file=sys.stderr)
        if event["type"] == "answer":
            answer = event

    # "type" is a routing field for stream() consumers, not part of this
    # function's long-standing contract.
    return {key: value for key, value in answer.items() if key != "type"}


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
