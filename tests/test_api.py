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

    def test_readyz_reports_unavailable_when_the_model_is_down(self, open_client):
        with patch("ollama.Client") as client:
            client.return_value.list.side_effect = ConnectionError("refused")
            response = open_client.get("/readyz")

        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail["ready"] is False
        assert detail["primary"]["error"] == "ConnectionError"

    def test_readyz_says_which_provider_answered(self, open_client):
        """
        Not just a boolean. "Ready on the fallback" and "ready on the primary"
        are different states of the world, and an endpoint that renders them
        identically hides an ongoing outage behind a green check.
        """
        with patch("ollama.Client"):
            body = open_client.get("/readyz").json()

        assert body["status"] == "ready"
        assert body["primary"]["ready"] is True
        assert body["primary"]["provider"] == "ollama"
        assert body["primary"]["mode"] == "local"

    def test_readyz_never_names_the_endpoint_it_probed(self, open_client):
        # An endpoint can carry a token in its userinfo or query string, and
        # /readyz is unauthenticated.
        with patch("ollama.Client") as client:
            client.return_value.list.side_effect = ConnectionError("refused")
            body = open_client.get("/readyz").text

        assert "11434" not in body and "http" not in body


class TestInferenceReporting:
    def test_the_configured_mode_is_reportable(self, open_client):
        """
        "Which mode is this actually running in?" used to be answerable only
        by reading the pod's environment -- and the answer changes what the
        deployment claims about your data.
        """
        body = open_client.get("/inference").json()

        assert body["primary"]["mode"] in ("local", "cluster", "api")
        assert body["allow_external"] is False

    def test_no_secret_reaches_the_report(self, open_client):
        assert "api_key" not in open_client.get("/inference").text
        assert "endpoint" not in open_client.get("/inference").text

    def test_metrics_render_in_prometheus_format(self, open_client):
        response = open_client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "# TYPE kubewhy_inference_requests_total counter" in response.text

    def test_a_refused_configuration_is_a_503_with_the_reason(self, open_client):
        """
        Not a 500. The distinction matters to whoever reads this at 3am: a 500
        says kubewhy is broken, a 503 with the reason says the values file is.
        """
        with patch("app.inference.gateway",
                   side_effect=ValueError("endpoint is off your network")):
            ready = open_client.get("/readyz")
            reported = open_client.get("/inference")

        assert ready.status_code == 503
        assert ready.json()["detail"]["error"] == "inference_misconfigured"
        assert "off your network" in ready.json()["detail"]["reason"]
        assert reported.status_code == 503

    def test_a_refused_configuration_does_not_kill_the_tool_endpoints(
            self, open_client):
        """
        The opposite call from the controller's, and deliberately. This API
        also serves /scan, /pods and /nodes, none of which touch a model --
        refusing to start all of that because inference is misconfigured would
        remove working functionality to punish a setting they do not use.
        """
        with patch("app.inference.gateway",
                   side_effect=ValueError("endpoint is off your network")):
            assert open_client.get("/healthz").status_code == 200
            assert open_client.get("/platform").status_code == 200

    def test_metrics_are_behind_the_same_token_as_everything_else(
            self, secured_client):
        # These series carry no cluster state, but they do carry which models
        # you run and how often each tool is failing. One rule, not two.
        assert secured_client.get("/metrics").status_code == 401


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

    def test_the_evidence_the_loop_carries_is_not_put_on_the_wire(self, open_client):
        """The answer event grew an "evidence" field for the evals, which
        record it so a run can be re-scored offline. /ask does not return it,
        so the event documented as matching /ask must not either -- and every
        result in it has already gone out as its own tool_result event."""
        answer = {
            "type": "answer",
            "answer": "memory-hog is OOMKilled",
            "tool_calls": [{"name": "describe_pod", "arguments": {}}],
            "evidence": [{"id": "tool-1", "tool": "describe_pod",
                          "result": '{"status": "OOMKilled"}'}],
            "confidence": "grounded",
            "unverified": [],
        }
        with patch.object(app_module, "stream", return_value=iter([answer])):
            response = open_client.post("/ask/stream", json={"question": "q"})

        _, data = sse_events(response.text)[-1]
        assert "evidence" not in data
        assert data["answer"] == "memory-hog is OOMKilled"

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

    That happened: GET /references shipped without Depends(require_caller) and
    served cluster topology to anyone who could reach the port while every
    other endpoint returned 401. Found by testing auth on a live cluster
    rather than on /scan alone. This enumerates the routes so the next one
    cannot repeat it.
    """

    # Liveness must not require a token: a probe that needs a secret fails
    # closed and gets the container killed during a credential problem.
    PUBLIC = {"/healthz", "/readyz", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

    def test_no_route_is_missing_the_auth_dependency(self):
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
            if "require_caller" not in names:
                unprotected.append(f"{sorted(getattr(route, 'methods', []) or [])} {path}")

        assert not unprotected, f"routes served without auth: {unprotected}"

    def test_references_specifically(self):
        """The one that was actually wrong, pinned by name."""
        import app as api

        route = next(r for r in api.app.routes if getattr(r, "path", "") == "/references")
        names = [getattr(d.dependency, "__name__", "") for d in route.dependencies]
        assert "require_caller" in names


class TestProxyAuthentication:
    """
    TRIAGE_AUTH_MODE=proxy: a person arrives through the authenticating proxy
    and their identity is a header it set.

    Every client here is built with an explicit loopback peer, because that is
    what a sidecar produces and because the default TestClient peer is the
    string "testclient" -- which is correctly refused, and would otherwise
    make every test in this class pass for the wrong reason.
    """

    HEADERS = {"X-Forwarded-Email": "sre@example.com"}

    @pytest.fixture
    def proxied(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        with patch.object(app_module, "API_TOKEN", ""):
            yield TestClient(app_module.app, client=("127.0.0.1", 51000))

    def test_a_proxied_identity_is_admitted(self, proxied):
        assert proxied.get("/platform", headers=self.HEADERS).status_code == 200

    def test_no_identity_is_refused(self, proxied):
        response = proxied.get("/platform")
        assert response.status_code == 401
        assert "no identity header" in response.json()["detail"]

    def test_an_empty_identity_is_refused(self, proxied):
        assert proxied.get("/platform",
                           headers={"X-Forwarded-Email": ""}).status_code == 401

    def test_liveness_stays_open(self, proxied):
        """A probe that needs an OIDC session is a probe that kills the pod."""
        assert proxied.get("/healthz").status_code == 200

    def test_an_identity_from_off_loopback_is_refused(self, monkeypatch):
        """
        The premise the header trust rests on, checked. A request that reached
        the app from a pod address got around the proxy, so nothing it carries
        is worth reading however well-formed it is.
        """
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        with patch.object(app_module, "API_TOKEN", ""):
            client = TestClient(app_module.app, client=("10.244.0.7", 51000))
            response = client.get("/platform", headers=self.HEADERS)

        assert response.status_code == 401
        assert "did not arrive over loopback" in response.json()["detail"]

    def test_a_token_still_works_alongside_the_proxy(self, monkeypatch):
        """
        Prometheus has no browser to complete an OIDC flow with. Removing the
        token when the proxy arrived would break every machine caller, and
        /metrics is behind the same auth as everything else.
        """
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        with patch.object(app_module, "API_TOKEN", "s3cret-token"):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            response = client.get("/metrics",
                                  headers={"Authorization": "Bearer s3cret-token"})

        assert response.status_code == 200

    def test_a_wrong_token_does_not_fall_through_to_the_header_path(self, monkeypatch):
        """
        Otherwise a caller could probe tokens for free, and a request that was
        simultaneously guessing credentials would be handed a valid session.
        """
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        with patch.object(app_module, "API_TOKEN", "s3cret-token"):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            response = client.get("/platform", headers={
                "Authorization": "Bearer wrong", **self.HEADERS})

        assert response.status_code == 401
        assert response.json()["detail"] == "invalid or missing bearer token"

    def test_a_configured_token_is_still_required_when_no_proxy_is_claimed(self):
        """
        The pre-proxy contract, unchanged. Adding the header path must not
        turn an existing token-secured deployment into an open one.
        """
        with patch.object(app_module, "API_TOKEN", "s3cret-token"):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            assert client.get("/platform", headers=self.HEADERS).status_code == 401


class TestPrincipalIsLogged:
    """
    The request line names who asked. This is the seam the per-question audit
    trail is built on, so it is pinned now rather than assumed later.
    """

    def test_the_caller_is_named(self, monkeypatch, caplog):
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        with patch.object(app_module, "API_TOKEN", ""):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            with caplog.at_level("INFO", logger="triage.api"):
                client.get("/platform", headers={"X-Forwarded-Email": "sre@example.com"})

        line = next(r for r in caplog.records if r.message == "request")
        assert line.principal == "sre@example.com"
        assert line.auth == "proxy"

    def test_an_unauthenticated_request_is_logged_as_anonymous(self, open_client, caplog):
        """
        Not omitted. A request line missing the field is one a log query
        cannot count, which is the query an audit review actually runs.
        """
        with caplog.at_level("INFO", logger="triage.api"):
            open_client.get("/platform")

        line = next(r for r in caplog.records if r.message == "request")
        assert line.principal == "anonymous"
        assert line.auth == "anonymous"
