"""
Tests for the HTTP surface: auth, health split, and error shape.
"""

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


class TestTheAuditTrailNamesTheApiCaller:
    """
    The defect a live run found and every unit test had missed.

    FastAPI runs a sync dependency on an AnyIO worker thread, so a ContextVar
    set there lives in that thread's copied context and is discarded when the
    dependency returns. audit.actor() was called from require_caller, and the
    result was that every API investigation was recorded as `anonymous` while
    the request log line immediately beside it named the caller correctly --
    which is the worst shape for this defect, because the surface that would
    make you doubt the audit trail is the one that looks right.

    Measured: a value set in middleware reaches both sync and async endpoints;
    one set in a sync dependency reaches neither.
    """

    @pytest.fixture
    def canned(self, monkeypatch):
        """One trivial investigation, so no model or cluster is needed."""
        import agent

        def fake(*a, **k):
            yield {"type": "tool_call", "run_id": "api-1", "name": "list_pods",
                   "arguments": {"namespace": "demo"}}
            yield {"type": "tool_result", "run_id": "api-1", "name": "list_pods",
                   "result": "{}", "duration_ms": 1.0}
            yield {"type": "answer", "run_id": "api-1", "answer": "ok",
                   "target": {"name": "crasher", "namespace": "demo"},
                   "confidence": "grounded", "tool_calls": [], "unverified": []}

        monkeypatch.setattr(agent, "_stream", fake)

    def audit_record(self, caplog):
        lines = [r for r in caplog.records if r.message == "investigation"]
        assert len(lines) == 1, f"expected one audit record, got {len(lines)}"
        return lines[0]

    def test_a_proxied_caller_is_named_in_the_audit_record(
            self, monkeypatch, caplog, canned):
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        with patch.object(app_module, "API_TOKEN", ""):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            with caplog.at_level("INFO", logger="triage.audit"):
                response = client.post(
                    "/ask", json={"question": "why?"},
                    headers={"X-Forwarded-Email": "sre@example.com"})

        assert response.status_code == 200
        line = self.audit_record(caplog)
        assert line.principal == "sre@example.com"
        assert line.auth == "proxy"
        assert line.surface == "api"

    def test_a_token_caller_is_named_as_a_token(self, monkeypatch, caplog, canned):
        with patch.object(app_module, "API_TOKEN", "s3cret-token"):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            with caplog.at_level("INFO", logger="triage.audit"):
                client.post("/ask", json={"question": "why?"},
                            headers={"Authorization": "Bearer s3cret-token"})

        line = self.audit_record(caplog)
        assert line.auth == "token"
        assert line.surface == "api"

    def test_the_audit_record_and_the_request_line_agree(
            self, monkeypatch, caplog, canned):
        """
        They disagreed, and that is what made the defect survivable. Pinned so
        a future change cannot let them drift apart again.
        """
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        with patch.object(app_module, "API_TOKEN", ""):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            with caplog.at_level("INFO"):
                client.post("/ask", json={"question": "why?"},
                            headers={"X-Forwarded-Email": "sre@example.com"})

        request_line = next(r for r in caplog.records if r.message == "request")
        audit_line = self.audit_record(caplog)

        assert request_line.principal == audit_line.principal
        assert request_line.auth == audit_line.auth


class TestTheInvestigationCeiling:
    """
    429 on the endpoints that drive the model.

    The ordering assertions matter as much as the ceiling: telling someone
    "you have asked too often" when they were never let in is a confusing
    thing to say, and it leaks that the endpoint exists.
    """

    @pytest.fixture
    def canned(self, monkeypatch):
        import agent
        import limits

        limits.reset()
        monkeypatch.setattr(agent, "_stream", lambda *a, **k: iter([
            {"type": "answer", "run_id": "r", "answer": "ok",
             "confidence": "grounded", "tool_calls": [], "unverified": []}]))
        yield
        limits.reset()

    def test_a_caller_past_the_ceiling_gets_429(self, monkeypatch, canned):
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "2")
        with patch.object(app_module, "API_TOKEN", ""):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            codes = [client.post("/ask", json={"question": "why?"}).status_code
                     for _ in range(3)]

        assert codes == [200, 200, 429]

    def test_the_429_says_when_to_come_back(self, monkeypatch, canned):
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "1")
        with patch.object(app_module, "API_TOKEN", ""):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            client.post("/ask", json={"question": "why?"})
            refused = client.post("/ask", json={"question": "why?"})

        assert refused.status_code == 429
        assert int(refused.headers["Retry-After"]) > 0

    def test_an_unauthenticated_caller_gets_401_not_429(self, monkeypatch, canned):
        """
        Order, not coincidence. require_caller is listed first on every one of
        these endpoints.
        """
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "1")
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        with patch.object(app_module, "API_TOKEN", ""):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            for _ in range(3):
                response = client.post("/ask", json={"question": "why?"})

        assert response.status_code == 401

    def test_a_refused_caller_does_not_spend_another_allowance(
            self, monkeypatch, canned):
        """
        Otherwise a client in a retry loop pushes its own window out forever
        and never recovers, which turns a rate limit into a permanent ban.
        """

        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "1")
        with patch.object(app_module, "API_TOKEN", ""):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            client.post("/ask", json={"question": "why?"})
            first = client.post("/ask", json={"question": "why?"})
            second = client.post("/ask", json={"question": "why?"})

        assert first.status_code == second.status_code == 429
        assert int(second.headers["Retry-After"]) <= int(first.headers["Retry-After"])

    def test_two_callers_have_separate_allowances(self, monkeypatch, canned):
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "1")
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        with patch.object(app_module, "API_TOKEN", ""):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            one = {"X-Forwarded-Email": "a@example.com"}
            two = {"X-Forwarded-Email": "b@example.com"}
            client.post("/ask", json={"question": "why?"}, headers=one)
            blocked = client.post("/ask", json={"question": "why?"}, headers=one)
            other = client.post("/ask", json={"question": "why?"}, headers=two)

        assert blocked.status_code == 429
        assert other.status_code == 200

    def test_reads_are_not_rate_limited(self, monkeypatch, canned):
        """
        /scan and /pods cost no model time, and the same ceiling on them would
        make the console's own browsing count against the person using it.
        """
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "1")
        with patch.object(app_module, "API_TOKEN", ""):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            client.post("/ask", json={"question": "why?"})
            codes = [client.get("/platform").status_code for _ in range(5)]

        assert codes == [200] * 5

    @pytest.mark.parametrize("path", ["/ask", "/ask/jobs", "/ask/stream"])
    def test_every_investigation_endpoint_carries_the_ceiling(self, path):
        """
        Enumerated, for the reason /references was: a new endpoint is
        unlimited until someone remembers the dependency, and it works.
        """
        route = next(r for r in app_module.app.routes
                     if getattr(r, "path", "") == path)
        names = [getattr(d.dependency, "__name__", "") for d in route.dependencies]

        assert "budgeted" in names, f"{path} has no ceiling"

    def test_the_posture_is_reported(self, monkeypatch, canned):
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "42")
        with patch.object(app_module, "API_TOKEN", ""), patch("ollama.Client"):
            client = TestClient(app_module.app, client=("127.0.0.1", 51000))
            body = client.get("/inference").json()

        assert body["limits"]["investigations_per_hour"] == 42


class TestTheStatusCodeIsTheContract:
    """
    app.py had never been surveyed -- `--all` looks for tests/test_app.py and
    this file is called test_api.py, so 42 mutants went unmeasured until
    2026-09-03, and 25 of them survived. Four were status codes.

    A status code is the part of an error a client acts on without reading.
    503 is retried, 403 is not; 404 says the job is gone rather than that the
    server broke. Each of these was a constant no case pinned, so any of them
    could have been changed by a typo and every test still passed.
    """

    def test_a_model_that_cannot_be_reached_is_503(self, open_client):
        """
        Retryable, and matching /readyz. The handler exists to keep this out
        of the 500s, where it would read as a bug in the agent.
        """
        with patch.object(app_module, "ask", side_effect=ConnectionError("refused")):
            response = open_client.post("/ask", json={"question": "why?"})

        assert response.status_code == 503
        assert "inference unreachable" in response.json()["detail"]

    def test_policy_refusing_to_send_evidence_is_403_not_500(self):
        """
        The handler's own docstring is the contract: "a 500 reads as a bug and
        gets retried, while a 403 reads as a decision and gets read". Nothing
        checked it, so the code could have been anything.
        """
        with patch.object(app_module, "API_TOKEN", ""), \
             patch.object(app_module, "ask",
                          side_effect=PermissionError("egress refused to api.example")):
            response = TestClient(app_module.app).post("/ask", json={"question": "q"})

        assert response.status_code == 403
        assert response.status_code != 500

    def test_an_accepted_job_is_202_not_200(self, open_client):
        """
        202 is the difference between "here is your answer" and "I have taken
        the question". A client that reads 200 as done stops polling.
        """
        with patch.object(app_module, "ask", return_value={"answer": "ok"}):
            response = open_client.post("/ask/jobs", json={"question": "why?"})

        assert response.status_code == 202
        assert response.json()["id"]

    def test_asking_for_a_job_that_does_not_exist_is_404(self, open_client):
        response = open_client.get("/ask/jobs/no-such-job-id")

        assert response.status_code == 404


class TestExpiryIsChargedToTheSubmitter:
    """
    There is no reaper thread: every submission purges what has aged out.
    The cutoff is `now - JOB_TTL_SECONDS`, and the sign is the whole of it --
    `now + TTL` is a cutoff in the future, which purges every job in the
    store including the one being created. Nothing tested the direction.
    """

    def test_a_job_just_created_survives_its_own_purge(self, open_client):
        with patch.object(app_module, "ask", return_value={"answer": "ok"}):
            created = open_client.post("/ask/jobs", json={"question": "why?"})
        job_id = created.json()["id"]

        assert open_client.get(f"/ask/jobs/{job_id}").status_code == 200

    def test_the_cutoff_is_a_time_in_the_past(self, open_client):
        """
        Read at the call rather than inferred, because a purge that removed
        everything and a purge that removed nothing both leave the new job
        reachable if it is written after the sweep.
        """
        import store

        with patch.object(app_module, "ask", return_value={"answer": "ok"}), \
             patch.object(app_module.JOBS, "purge_jobs") as purge:
            open_client.post("/ask/jobs", json={"question": "why?"})

        cutoff = purge.call_args.args[0]
        assert cutoff < store.now()
        assert store.now() - cutoff == pytest.approx(store.JOB_TTL_SECONDS, abs=5)
