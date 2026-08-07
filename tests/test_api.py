"""
Tests for the HTTP surface: auth, health split, and error shape.
"""

import importlib
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
            open_client.get("/scan?only_unhealthy=false&limit=5")

        scan.assert_called_once_with(False, 5)

    def test_defaults_to_unhealthy_only(self, open_client):
        """Cluster-wide including healthy workloads is the expensive call."""
        with patch.object(app_module, "scan_cluster", return_value={}) as scan:
            open_client.get("/scan")

        scan.assert_called_once_with(True, 20)


class TestRequestLogging:
    def test_request_id_returned(self, open_client):
        assert open_client.get("/healthz").headers.get("X-Request-ID")
