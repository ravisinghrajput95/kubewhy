"""
local-triage-agent.

Wraps the host and Kubernetes collectors in routers/ as tools and lets a local
Ollama model call them to work out what is wrong and why.

    python agent.py "why is this machine slow?"
    python agent.py "what is broken in the demo namespace?"
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
sentences, not a report."""


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
    than a gate.
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
            return {
                "answer": answer,
                "tool_calls": trace,
                **grounding.check(answer, outputs),
            }

        for call in calls:
            name = call.function.name
            arguments = dict(call.function.arguments or {})
            trace.append({"name": name, "arguments": arguments})

            if verbose:
                print(f"  -> {name}({arguments})", file=sys.stderr)

            started = time.perf_counter()
            output = _run_tool(name, arguments)
            outputs.append(output)

            log.info(
                "tool_call",
                extra={
                    "tool": name,
                    "arguments": arguments,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "result_chars": len(output),
                },
            )
            messages.append(
                {"role": "tool", "tool_name": name, "content": output}
            )

    return {
        "answer": f"Gave up after {MAX_ROUNDS} rounds of tool calls.",
        "tool_calls": trace,
        "confidence": "ungrounded",
        "unverified": [],
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        raise SystemExit(1)

    result = ask(" ".join(sys.argv[1:]), verbose=True)
    print(result["answer"])

    # Surface unsupported claims rather than leaving them to be spotted by
    # eye; stderr so piping the answer stays clean.
    if result["unverified"]:
        print(
            f"\n[{result['confidence']}] not found in any tool result: "
            + ", ".join(result["unverified"]),
            file=sys.stderr,
        )
