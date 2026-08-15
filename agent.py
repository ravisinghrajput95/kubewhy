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

log = logging.getLogger("triage.agent")

# Max tool-calling rounds before we give up. Guards against a model that
# keeps calling tools without ever settling on an answer.
MAX_ROUNDS = 8

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


def _run_tool(name, arguments):
    """Execute one tool call, returning its result as a JSON string."""
    func = TOOLS.get(name)
    if func is None:
        return json.dumps({"error": f"no such tool: {name}"})

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


def stream(question, model=MODEL, think=True, prefetched=None):
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
    prefetched = list(prefetched or [])
    trace = []
    # Seeded with the prefetched results so grounding treats them as
    # measurements. They ARE measurements -- a tool produced them against the
    # live cluster -- and the alternative is an answer quoting the one piece of
    # evidence that survived being marked unverified for doing so.
    outputs = [item["result"] for item in prefetched]

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

    for _ in range(MAX_ROUNDS):
        response, think = _chat(model, messages, think)
        message = response.message
        messages.append(message)

        calls = message.tool_calls
        if not calls:
            answer = (message.content or "").strip()
            yield {
                "type": "answer",
                "answer": answer,
                "tool_calls": trace,
                **grounding.check(answer, outputs),
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
            duration_ms = round((time.perf_counter() - started) * 1000, 1)

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
        "confidence": "ungrounded",
        "unverified": [],
    }


def ask(question, model=MODEL, verbose=False, think=True, prefetched=None):
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
