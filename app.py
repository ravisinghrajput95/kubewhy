"""
HTTP surface: the collectors as REST endpoints, plus the agent at /ask.

Every endpoint here exposes something sensitive -- hostnames, usernames, the
process table, cluster state, pod logs. Bind to localhost, and set
TRIAGE_API_TOKEN before exposing it anywhere else.
"""

import logging
import os
import secrets
import time
import uuid

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

import observability
from agent import ask
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

observability.configure()
log = logging.getLogger("triage.api")

API_TOKEN = os.getenv("TRIAGE_API_TOKEN", "")


def require_token(authorization: str = Header(default="")):
    """
    Bearer auth, enabled by setting TRIAGE_API_TOKEN.

    Left unset the API is open, which is only safe bound to localhost -- so
    startup warns loudly about it. Comparison is constant-time; a token check
    that leaks length via early exit is not a token check.
    """
    if not API_TOKEN:
        return

    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(presented, API_TOKEN):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


@asynccontextmanager
async def lifespan(_app):
    if not API_TOKEN:
        log.warning(
            "TRIAGE_API_TOKEN is unset: the API is unauthenticated and exposes "
            "cluster state, pod logs and the host process table. Bind to "
            "localhost only."
        )
    yield


app = FastAPI(
    title="kubewhy",
    description="Read-only host and Kubernetes diagnostics, answered by a local model.",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    """One structured line per request, with an id to correlate with tools."""
    request_id = str(uuid.uuid4())[:8]
    started = time.perf_counter()

    response = await call_next(request)

    log.info(
        "request",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    response.headers["X-Request-ID"] = request_id
    return response


# --- health -----------------------------------------------------------------
# Split deliberately: liveness must not depend on anything external, or a
# transient Ollama outage gets the container killed rather than drained.

@app.get("/healthz", tags=["health"])
def healthz():
    """Liveness: the process is up. No side effects, no dependencies."""
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
def readyz():
    """Readiness: the model backend is reachable, so /ask can succeed."""
    import ollama

    try:
        ollama.Client(timeout=5).list()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"ollama unreachable: {type(exc).__name__}"
        )
    return {"status": "ready"}


# --- host -------------------------------------------------------------------

@app.get("/platform", dependencies=[Depends(require_token)], tags=["host"])
def platform():
    return get_platform_info()


@app.get("/system", dependencies=[Depends(require_token)], tags=["host"])
def system():
    return get_system_info()


@app.get("/processes", dependencies=[Depends(require_token)], tags=["host"])
def process(name_filter: str = ""):
    return get_processes(name_filter)


@app.get("/cpu", dependencies=[Depends(require_token)], tags=["host"])
def top_cpu(limit: int = 5):
    return get_top_cpu_processes(limit)


@app.get("/memory", dependencies=[Depends(require_token)], tags=["host"])
def top_memory(limit: int = 5):
    return get_top_memory_processes(limit)


# --- kubernetes -------------------------------------------------------------

@app.get("/scan", dependencies=[Depends(require_token)], tags=["kubernetes"])
def scan(only_unhealthy: bool = True, limit: int = 20):
    return scan_cluster(only_unhealthy, limit)


@app.get("/pods", dependencies=[Depends(require_token)], tags=["kubernetes"])
def pods(namespace: str = "default", only_unhealthy: bool = False):
    return list_pods(namespace, only_unhealthy)


@app.get("/pods/{name}", dependencies=[Depends(require_token)], tags=["kubernetes"])
def pod_detail(name: str, namespace: str = "default"):
    return describe_pod(name, namespace)


@app.get("/pods/{name}/events", dependencies=[Depends(require_token)], tags=["kubernetes"])
def pod_events(name: str, namespace: str = "default", limit: int = 10):
    return get_pod_events(name, namespace, limit)


@app.get("/pods/{name}/logs", dependencies=[Depends(require_token)], tags=["kubernetes"])
def pod_logs(name: str, namespace: str = "default", tail: int = 20):
    return get_pod_logs(name, namespace, tail)


@app.get("/nodes", dependencies=[Depends(require_token)], tags=["kubernetes"])
def nodes():
    return list_nodes()


@app.get("/deployments", dependencies=[Depends(require_token)], tags=["kubernetes"])
def deployments(namespace: str = "default"):
    return list_deployments(namespace)


@app.get(
    "/services/{name}/endpoints",
    dependencies=[Depends(require_token)],
    tags=["kubernetes"],
)
def service_endpoints(name: str, namespace: str = "default"):
    return get_service_endpoints(name, namespace)


# --- agent ------------------------------------------------------------------

class Question(BaseModel):
    question: str


@app.post("/ask", dependencies=[Depends(require_token)], tags=["agent"])
def ask_agent(body: Question):
    """
    Answer a plain-English question about the host or the cluster.

    Note this blocks for as long as the model takes -- tens of seconds is
    normal, and a deep chain can exceed a minute. Set generous client
    timeouts; see the README for why this is not yet async.
    """
    return ask(body.question)
