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
)

# OLLAMA_HOST is read by the ollama client itself; in a container it needs to
# point back at the host, e.g. http://host.docker.internal:11434
MODEL = os.getenv("TRIAGE_MODEL", "qwen3")

# A hung model would otherwise block the loop forever, with the caller holding
# an HTTP request open. Generous, because a cold model load plus a deep chain
# is legitimately slow.
TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "300"))

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
            model=model, messages=messages, tools=list(TOOLS.values()), think=think
        ), think
    except ollama.ResponseError as exc:
        if think and "does not support thinking" in str(exc):
            return client.chat(
                model=model, messages=messages, tools=list(TOOLS.values()), think=False
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


def stream(question, model=MODEL, think=True):
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
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    trace = []
    outputs = []

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


def ask(question, model=MODEL, verbose=False, think=True):
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

    for event in stream(question, model=model, think=think):
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
        result = ask(
            f"Pod {entry['example']} in namespace {namespace} is "
            f"{entry['status']}. Find the root cause and say what should change.",
            verbose=True,
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
