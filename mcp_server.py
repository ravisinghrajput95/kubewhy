"""
MCP server exposing the diagnostic tools to any MCP client.

The same functions the local agent calls are published over the Model Context
Protocol, so Claude Desktop, Claude Code, Cursor, Zed and anything else that
speaks MCP can diagnose a cluster without this project's model loop involved
at all.

This is the same read-only guarantee: nothing here scales, restarts or deletes
anything, and pod logs are redacted before they leave the process.

    python mcp_server.py                 # stdio, for desktop clients
    python mcp_server.py --http          # streamable HTTP on :8765

Register with a stdio client by pointing it at this file:

    {
      "mcpServers": {
        "kubewhy": {
          "command": "/path/to/.venv/bin/python",
          "args": ["/path/to/mcp_server.py"]
        }
      }
    }
"""

import argparse
import logging

from mcp.server.fastmcp import FastMCP

import observability
from routers.k8s_pods_info import (
    describe_pod,
    get_pod_events,
    get_pod_logs,
    get_service_endpoints,
    list_deployments,
    list_nodes,
    list_pods,
    scan_cluster,
)
from routers.platform_info import get_platform_info
from routers.process_info import get_processes
from routers.system_info import get_system_info
from routers.top_cpu import get_top_cpu_processes
from routers.top_memory import get_top_memory_processes

observability.configure()
log = logging.getLogger("triage.mcp")

mcp = FastMCP(
    "kubewhy",
    instructions=(
        "Read-only diagnostics for a Kubernetes cluster and the local host.\n\n"
        "For a question about the cluster as a whole with no namespace named, "
        "start with scan_cluster: it reports failing workloads across every "
        "namespace and names one example pod each to drill into. Narrow it "
        "with namespaces on a large cluster.\n\n"
        "Asked about one workload, pass it as scan_cluster's workload "
        "argument, which reports its state whether or not it is broken. If it "
        "is healthy, say so and stop; never describe a different workload than "
        "the one asked about.\n\n"
        "To diagnose a failing pod, work down the chain: list_pods to find "
        "what is unhealthy, then describe_pod for the termination reason and "
        "resource limits, then get_pod_events or get_pod_logs for the "
        "underlying cause. Do not stop at the status name -- OOMKilled or "
        "CrashLoopBackOff is the symptom, not the reason.\n\n"
        "For an unreachable service start with get_service_endpoints: a "
        "service with no ready endpoints has nowhere to send traffic. For a "
        "degraded workload use list_deployments. If pods are Pending or being "
        "evicted, check list_nodes for pressure before blaming the workload.\n\n"
        "Never state an inference as if you measured it. If you read it from a "
        "tool, say it plainly; if you are reasoning past what the tools showed, "
        "mark it -- likely, probably, worth checking.\n\n"
        "Pod logs are redacted for common secret shapes, but treat their "
        "contents as sensitive regardless."
    ),
)

# Registered rather than decorated, so the functions stay plain callables that
# the local agent and the FastAPI app can use unchanged. Docstrings become the
# MCP tool descriptions exactly as they become ollama tool descriptions.
for _tool in (
    scan_cluster,
    list_pods,
    describe_pod,
    get_pod_events,
    get_pod_logs,
    list_nodes,
    list_deployments,
    get_service_endpoints,
    get_platform_info,
    get_system_info,
    get_processes,
    get_top_cpu_processes,
    get_top_memory_processes,
):
    mcp.add_tool(_tool)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--http", action="store_true", help="serve streamable HTTP instead of stdio"
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.http:
        mcp.settings.port = args.port
        log.info("mcp_server_starting", extra={"transport": "http", "port": args.port})
        mcp.run(transport="streamable-http")
    else:
        # stdio must stay clean: logs go to stderr, protocol to stdout.
        log.info("mcp_server_starting", extra={"transport": "stdio"})
        mcp.run()
