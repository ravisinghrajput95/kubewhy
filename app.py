"""
HTTP surface: the collectors as REST endpoints, plus the agent at /ask.

Every endpoint here exposes something sensitive -- hostnames, usernames, the
process table, cluster state, pod logs. Bind to localhost, and set
TRIAGE_API_TOKEN before exposing it anywhere else.
"""

import json
import logging
import os
import threading
import secrets
import time
import uuid

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

import inference
import observability
import store
import telemetry
from agent import ask, scoped_question, stream
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

    # Resolve inference at startup so the configuration is in the log before
    # anyone asks a question, and so a configuration the gateway refuses is
    # discovered now rather than during an incident. See the same call in
    # controller.py for what installing the chart revealed.
    #
    # Unlike the controller this does NOT abort. The controller exists only to
    # diagnose, so a controller that cannot is broken. This API also serves
    # /scan, /pods, /nodes and the rest, none of which touch a model -- killing
    # all of that because inference is misconfigured would remove working
    # functionality to punish a setting they do not use. It is logged loudly
    # and /readyz reports it, which is the pair of things an operator needs.
    try:
        inference.gateway()
    except ValueError as exc:
        log.error("inference_misconfigured", extra={"error": str(exc)})
    yield


JOBS = store.build()

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


@app.exception_handler(ConnectionError)
async def model_unreachable(_request: Request, exc: ConnectionError):
    """
    A dead model backend is a 503, not a 500.

    /ask raises straight through the model client when nothing is listening,
    and FastAPI turned that into a bare "Internal Server Error" with no body --
    so a caller could not tell "the model is down" from "kubewhy is broken",
    and the one endpoint whose whole job is diagnosis gave a worse error
    message than the things it diagnoses.

    503 and the reason, matching /readyz. /ask/stream needs no handler: its
    status line is long gone by the time the model is called, so it emits an
    `error` event instead.
    """
    log.warning("model_unreachable", extra={"error": str(exc)})
    return JSONResponse(
        status_code=503,
        content={"detail": f"inference unreachable: {type(exc).__name__}"},
    )


@app.exception_handler(PermissionError)
async def egress_refused(_request: Request, exc: PermissionError):
    """
    Policy refused to send evidence off-network. That is a 403, not a 500.

    The distinction is the whole point of having the policy: a 500 reads as a
    bug and gets retried, while a 403 reads as a decision and gets read. The
    message names no endpoint -- see inference.Target.describe.
    """
    log.warning("inference_egress_refused", extra={"error": str(exc)})
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.get("/readyz", tags=["health"])
def readyz():
    """
    Readiness: inference is reachable, so /ask can succeed.

    Asks the gateway rather than a provider. This endpoint used to build an
    Ollama client directly, which meant the API server held provider-specific
    knowledge -- and would have reported an in-cluster vLLM deployment as
    permanently NotReady while it served requests perfectly well.

    Ready when *either* the primary or an enabled fallback answers, and the
    body says which. Those are different states of the world, and rendering
    them identically hides an ongoing outage behind a green check.
    """
    try:
        report = inference.gateway().probe()
    except ValueError as exc:
        # A configuration the gateway refuses is a readiness failure with a
        # cause, not a 500. The distinction matters to whoever is reading this
        # at 3am: a 500 says kubewhy is broken, a 503 with the reason says the
        # values file is.
        raise HTTPException(
            status_code=503,
            detail={"ready": False, "error": "inference_misconfigured",
                    "reason": str(exc)},
        )
    if not report["ready"]:
        raise HTTPException(status_code=503, detail=report)
    return {"status": "ready", **report}


@app.get("/inference", dependencies=[Depends(require_token)], tags=["health"])
def inference_config():
    """
    Where inference happens and whether evidence may leave, as configured.

    Exists because "which mode is this actually running in?" was answerable
    only by reading the pod's environment, and the answer changes what the
    deployment is claiming about your data. Safe fields only: no endpoint and
    no key, for the reason inference.Target.describe gives.
    """
    try:
        return inference.gateway().config.describe()
    except ValueError as exc:
        # The one endpoint whose entire job is saying what inference is
        # configured to do must answer when the answer is "something illegal".
        raise HTTPException(
            status_code=503,
            detail={"error": "inference_misconfigured", "reason": str(exc)},
        )


@app.get("/metrics", dependencies=[Depends(require_token)], tags=["health"])
def metrics():
    """
    Prometheus exposition.

    Behind the same bearer token as everything else rather than open. One rule
    is easier to reason about than two, and while these series carry no
    cluster state, they do carry which models you run and how often each tool
    is failing. Prometheus sends a bearer token with `authorization` in the
    scrape config.
    """
    return Response(content=telemetry.render(),
                    media_type="text/plain; version=0.0.4; charset=utf-8")


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
def scan(
    only_unhealthy: bool = True,
    limit: int = 20,
    namespaces: str = "",
    workload: str = "",
):
    return scan_cluster(only_unhealthy, limit, namespaces, workload)


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


@app.get("/references", dependencies=[Depends(require_token)], tags=["kubernetes"])
def references(namespace: str = "default"):
    """Objects in this namespace whose references do not resolve."""
    return scan_references(namespace)


# --- agent ------------------------------------------------------------------

class Question(BaseModel):
    question: str
    # Optional scoping, so an API caller with a selection gets the same
    # behaviour the browser UI does rather than a cluster-wide answer.
    workload: str = ""
    namespace: str = ""
    pod: str = ""


def _question(body):
    """Apply workload scoping when the caller asked for it."""
    if body.workload and body.namespace:
        return scoped_question(
            body.question, body.workload, body.namespace, body.pod or None
        )
    return body.question


@app.post("/ask", dependencies=[Depends(require_token)], tags=["agent"])
def ask_agent(body: Question):
    """
    Answer a plain-English question about the host or the cluster.

    Note this blocks for as long as the model takes -- tens of seconds is
    normal, and a deep chain can exceed a minute. Set generous client
    timeouts, or use /ask/stream to see progress while it works.
    """
    return ask(_question(body))


@app.post("/ask/jobs", status_code=202, dependencies=[Depends(require_token)], tags=["agent"])
def submit_job(body: Question):
    """
    Ask without holding the connection open. Returns immediately with an id.

    /ask blocks for the whole diagnosis and /ask/stream makes that wait legible
    without shortening it -- both need the caller to still be there minutes
    later. This detaches the work: poll GET /ask/jobs/{id} until state is
    "done", from a different process or a different day.

    With TRIAGE_STATE_DB set the result survives a restart of this process. It
    does not survive being answered by a *different* replica, which is why the
    chart still pins one -- see store.py.
    """
    job_id = store.new_job_id()
    question = _question(body)
    JOBS.create_job(job_id, question, store.now())
    # Expiry is charged to whoever submits, so an idle deployment does not need
    # a reaper thread to stop the file growing forever.
    JOBS.purge_jobs(store.now() - store.JOB_TTL_SECONDS)

    def run():
        JOBS.update_job(job_id, "running")
        try:
            result = ask(question)
        except Exception as exc:  # noqa: BLE001 - a failed job must be readable
            log.exception("job_failed")
            JOBS.update_job(
                job_id, "failed", {"error": str(exc)}, store.now()
            )
            return
        JOBS.update_job(job_id, "done", result, store.now())

    threading.Thread(target=run, daemon=True).start()
    return {"id": job_id, "state": "queued"}


@app.get("/ask/jobs/{job_id}", dependencies=[Depends(require_token)], tags=["agent"])
def job_status(job_id: str):
    """
    A job's state, and its answer once there is one.

    404 rather than an empty job: an id that was never issued and one whose
    result expired are the same thing to a caller, and inventing a "queued"
    job for a typo would leave them polling forever.
    """
    job = JOBS.get_job(job_id)
    if not job:
        raise HTTPException(404, f"no job {job_id}")
    return job


@app.post("/ask/stream", dependencies=[Depends(require_token)], tags=["agent"])
def ask_agent_streaming(body: Question):
    """
    The same answer, delivered as server-sent events while it is produced.

    Emits one `tool_call` event as each tool is dispatched, one `tool_result`
    when it returns, and a final `answer` event carrying the same body /ask
    would have returned. A client that only wants the answer can skip to the
    last event and be exactly where /ask leaves it.

    This fixes the silence, not the blocking. The connection is still held for
    the whole run -- what changes is that the caller sees the chain advancing
    instead of a dead socket, which is what made a two-minute /ask
    indistinguishable from a hang. Truly detaching the work needs a job store,
    and a job store shared across replicas is the same unsolved problem as the
    controller's in-memory dedup state.
    """

    def events():
        try:
            for event in stream(_question(body)):
                # SSE frames the type separately so a client can dispatch on it
                # without parsing the payload first.
                #
                # "evidence" is dropped for the same reason ask() drops it:
                # this event is documented above as carrying the same body
                # /ask would have returned, and /ask does not carry it. It
                # would be duplication here in any case -- every result in it
                # was already sent as its own tool_result event, which is the
                # streaming client's way of getting the same data as it lands.
                payload = json.dumps({
                    k: v for k, v in event.items()
                    if k not in ("type", "evidence", "draft")
                })
                yield f"event: {event['type']}\ndata: {payload}\n\n"
        except Exception as exc:
            # The stream has already begun, so the status line is long gone --
            # a failure has to arrive as an event or the client just sees the
            # connection end and cannot tell success from collapse.
            detail = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
            yield f"event: error\ndata: {detail}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # Proxies that buffer would defeat the entire point.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
