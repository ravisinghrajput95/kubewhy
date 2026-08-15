"""
Tests for the HTTP surface: auth, health split, and error shape.
"""

import importlib
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture
def open_client():
    """Default posture: no token configured, endpoints open."""
    with patch.object(app_module, "API_TOKEN", ""):
        yield TestClient(app_module.app)


@pytest.fixture
def secured_client():
    with patch.object(app_module, "API_TOKEN", "s3cret-token"):
        yield TestClient(app_module.app)


class TestHealth:
    def test_healthz_has_no_dependencies(self, open_client):
        """Liveness must not fail because Ollama or the cluster is down."""
        assert open_client.get("/healthz").json() == {"status": "ok"}

    def test_healthz_needs_no_auth(self, secured_client):
        # A probe that needs a credential is a probe that fails on rotation.
        assert secured_client.get("/healthz").status_code == 200

    def test_readyz_reports_unavailable_when_model_is_down(self, open_client):
        with patch("ollama.Client") as client:
            client.return_value.list.side_effect = ConnectionError("refused")
            response = open_client.get("/readyz")

        assert response.status_code == 503
        assert "ollama unreachable" in response.json()["detail"]


class TestAuth:
    def test_open_when_no_token_configured(self, open_client):
        assert open_client.get("/platform").status_code == 200

    def test_rejects_missing_token(self, secured_client):
        assert secured_client.get("/platform").status_code == 401

    def test_rejects_wrong_token(self, secured_client):
        response = secured_client.get(
            "/platform", headers={"Authorization": "Bearer wrong"}
        )
        assert response.status_code == 401

    def test_rejects_wrong_scheme(self, secured_client):
        response = secured_client.get(
            "/platform", headers={"Authorization": "Basic s3cret-token"}
        )
        assert response.status_code == 401

    def test_accepts_correct_token(self, secured_client):
        response = secured_client.get(
            "/platform", headers={"Authorization": "Bearer s3cret-token"}
        )
        assert response.status_code == 200

    def test_every_data_endpoint_is_guarded(self, secured_client):
        """A new endpoint added without the dependency would leak silently."""
        paths = [
            "/platform", "/system", "/processes", "/cpu", "/memory",
            "/scan", "/pods", "/pods/x", "/pods/x/events", "/pods/x/logs",
            "/nodes", "/deployments", "/services/x/endpoints",
        ]
        unguarded = [p for p in paths if secured_client.get(p).status_code != 401]
        assert unguarded == [], f"unauthenticated: {unguarded}"

    def test_ask_is_guarded(self, secured_client):
        response = secured_client.post("/ask", json={"question": "hi"})
        assert response.status_code == 401


class TestScan:
    def test_passes_query_parameters_through(self, open_client):
        with patch.object(app_module, "scan_cluster", return_value={}) as scan:
            open_client.get("/scan?only_unhealthy=false&limit=5&namespaces=prod,staging")

        scan.assert_called_once_with(False, 5, "prod,staging", "")

    def test_defaults_to_unhealthy_only(self, open_client):
        """Cluster-wide including healthy workloads is the expensive call."""
        with patch.object(app_module, "scan_cluster", return_value={}) as scan:
            open_client.get("/scan")

        scan.assert_called_once_with(True, 20, "", "")

    def test_can_ask_about_one_workload(self, open_client):
        """Reports its state healthy or not, so "it is fine" is answerable."""
        with patch.object(app_module, "scan_cluster", return_value={}) as scan:
            open_client.get("/scan?workload=payments-api")

        assert scan.call_args.args[3] == "payments-api"


def sse_events(text):
    """Parse an SSE body into [(event, data), ...]."""
    out = []
    for frame in text.strip().split("\n\n"):
        lines = dict(
            line.split(": ", 1) for line in frame.splitlines() if ": " in line
        )
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


class TestAskStream:
    """
    /ask/stream exists so a two-minute diagnosis is distinguishable from a
    hang. The ordering is the contract: a caller must see a tool dispatched
    before its result, and exactly one answer, last.
    """

    def test_streams_the_chain_then_the_answer(self, open_client):
        events = [
            {"type": "tool_call", "name": "list_pods", "arguments": {}},
            {"type": "tool_result", "name": "list_pods", "result": "{}", "duration_ms": 3.0},
            {
                "type": "answer",
                "answer": "all fine",
                "tool_calls": [],
                "confidence": "grounded",
                "unverified": [],
            },
        ]
        with patch.object(app_module, "stream", return_value=iter(events)):
            response = open_client.post("/ask/stream", json={"question": "q"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        parsed = sse_events(response.text)
        assert [name for name, _ in parsed] == ["tool_call", "tool_result", "answer"]

    def test_final_event_matches_what_ask_would_return(self, open_client):
        """A client that reads only the last event is where /ask leaves it."""
        answer = {
            "type": "answer",
            "answer": "memory-hog is OOMKilled",
            "tool_calls": [{"name": "describe_pod", "arguments": {}}],
            "confidence": "grounded",
            "unverified": [],
        }
        with patch.object(app_module, "stream", return_value=iter([answer])):
            response = open_client.post("/ask/stream", json={"question": "q"})

        name, data = sse_events(response.text)[-1]
        assert name == "answer"
        # "type" is SSE framing, not part of the payload.
        assert data == {k: v for k, v in answer.items() if k != "type"}

    def test_a_mid_stream_failure_arrives_as_an_event(self, open_client):
        """
        The status line is long gone by then, so a failure that is not sent as
        an event looks identical to a completed run.
        """
        def boom():
            yield {"type": "tool_call", "name": "list_pods", "arguments": {}}
            raise ConnectionError("ollama went away")

        with patch.object(app_module, "stream", return_value=boom()):
            response = open_client.post("/ask/stream", json={"question": "q"})

        name, data = sse_events(response.text)[-1]
        assert name == "error"
        assert "ollama went away" in data["error"]

    def test_is_guarded(self, secured_client):
        response = secured_client.post("/ask/stream", json={"question": "q"})
        assert response.status_code == 401


class TestRequestLogging:
    def test_request_id_returned(self, open_client):
        assert open_client.get("/healthz").headers.get("X-Request-ID")


class TestEveryRouteIsAuthenticated:
    """
    Auth is per-route, so a new endpoint is unprotected until someone
    remembers the dependency -- and nobody notices, because the endpoint
    works.

    That happened: GET /references shipped without Depends(require_token) and
    served cluster topology to anyone who could reach the port while every
    other endpoint returned 401. Found by testing auth on a live cluster
    rather than on /scan alone. This enumerates the routes so the next one
    cannot repeat it.
    """

    # Liveness must not require a token: a probe that needs a secret fails
    # closed and gets the container killed during a credential problem.
    PUBLIC = {"/healthz", "/readyz", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

    def test_no_route_is_missing_the_token_dependency(self):
        import app as api

        unprotected = []
        for route in api.app.routes:
            path = getattr(route, "path", None)
            if not path or path in self.PUBLIC:
                continue
            names = [
                getattr(d.dependency, "__name__", "")
                for d in getattr(route, "dependencies", [])
            ]
            if "require_token" not in names:
                unprotected.append(f"{sorted(getattr(route, 'methods', []) or [])} {path}")

        assert not unprotected, f"routes served without auth: {unprotected}"

    def test_references_specifically(self):
        """The one that was actually wrong, pinned by name."""
        import app as api

        route = next(r for r in api.app.routes if getattr(r, "path", "") == "/references")
        names = [getattr(d.dependency, "__name__", "") for d in route.dependencies]
        assert "require_token" in names
