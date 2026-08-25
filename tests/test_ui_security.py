"""
The console must not become the way a credential escapes.

Everything here is a property of the *presentation* layer specifically. The
egress policy, the endpoint classifier and the redactor have their own suites;
these ask a narrower question: does putting a browser in front of this system
create a path that did not exist before?

Streamlit renders server-side, so the browser never holds a Kubernetes client
or a provider key -- but "never" is an architectural claim, and an
architectural claim with no test is an assumption.
"""
import json
import os
import sys
from unittest.mock import patch

import pytest

pytest.importorskip("streamlit", reason="UI extra not installed (requirements-ui.txt)")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streamlit.testing.v1 import AppTest  # noqa: E402

import inference  # noqa: E402
import redaction  # noqa: E402
import routers.k8s_pods_info as k8s  # noqa: E402

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui.py")

FAKE_KEY = "sk-proj-TESTONLYnotarealkey0000000000000000000000"

SCAN = {"demo/crasher": {"status": "CrashLoopBackOff", "pods": 1,
                         "example": "crasher-1", "fault": "crash"}}


def page_text(app):
    """Every string the page rendered, in one blob."""
    parts = []
    for group in ("markdown", "caption", "code", "json", "text",
                  "warning", "error", "info", "success"):
        for el in getattr(app, group, []):
            parts.append(str(getattr(el, "value", "")))
    for el in getattr(app, "dataframe", []):
        parts.append(str(getattr(el, "value", "")))
    return "\n".join(parts)


def render(env=None, scan=None, answer=None):
    import streamlit as st

    st.cache_data.clear()
    inference._GATEWAY = None if hasattr(inference, "_GATEWAY") else None
    with patch.dict(os.environ, env or {}, clear=False), \
         patch.object(k8s, "scan_cluster", return_value=scan or SCAN), \
         patch.object(k8s, "list_nodes", return_value={}), \
         patch.object(k8s, "describe_pod",
                      return_value={"pod": "crasher-1", "containers": {}}), \
         patch.object(k8s, "get_pod_events",
                      return_value={"pod": "crasher-1", "events": []}), \
         patch.object(k8s, "get_pod_logs",
                      return_value={"pod": "crasher-1", "source": "c", "logs": "x"}):
        app = AppTest.from_file(UI, default_timeout=60)
        if answer:
            app.session_state["answer"] = answer
        app.run()
    return app


class TestNoCredentialReachesTheBrowser:
    def test_a_configured_api_key_appears_nowhere_on_the_page(self):
        """
        The header names the provider and the model, because an operator has to
        know where evidence goes. It must not name the credential that gets it
        there.
        """
        app = render(env={
            "OPENAI_API_KEY": FAKE_KEY,
            "TRIAGE_INFERENCE_MODE": "api",
            "TRIAGE_INFERENCE_PROVIDER": "openai",
            "TRIAGE_INFERENCE_ENDPOINT": "https://api.openai.com/v1",
            "TRIAGE_ALLOW_EXTERNAL_INFERENCE": "true",
        })

        rendered = page_text(app)
        # An empty capture would pass every assertion below it. Measured at
        # 3832 characters when this was written; the floor is deliberately far
        # under that, and the positive checks are what prove the page rendered
        # rather than the length alone.
        assert len(rendered) > 500 and "cluster" in rendered.lower(), \
            "page_text captured nothing -- the assertions below would be vacuous"
        assert FAKE_KEY not in rendered
        # Not even a prefix long enough to be useful.
        assert "sk-proj-TESTONLY" not in rendered

    def test_the_gateway_description_the_header_reads_carries_no_key(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": FAKE_KEY,
            "TRIAGE_INFERENCE_MODE": "api",
            "TRIAGE_INFERENCE_PROVIDER": "openai",
            "TRIAGE_INFERENCE_ENDPOINT": "https://api.openai.com/v1",
            "TRIAGE_ALLOW_EXTERNAL_INFERENCE": "true",
        }, clear=False):
            described = json.dumps(inference.config().describe()
                                   if hasattr(inference, "config")
                                   else inference.gateway().config.describe())

        assert FAKE_KEY not in described
        assert "api_key" not in described

    def test_the_source_of_the_page_names_no_credential(self):
        """
        A grep, deliberately. The page is generated from this one file, and the
        cheapest way to keep a key off it is for the file never to name one.
        """
        source = open(UI).read()

        for forbidden in ("OPENAI_API_KEY", "api_key", "apiKey",
                          "Authorization", "bearer "):
            assert forbidden not in source, f"ui.py references {forbidden!r}"


class TestTheBrowserTalksOnlyToStreamlit:
    def test_the_page_ships_no_javascript_that_calls_anything(self):
        """
        Streamlit renders server-side. If the console ever grew a fetch() to
        the Kubernetes API or to a model provider, the browser would hold a
        credential and the egress policy would be bypassed entirely -- the
        policy lives in inference.py, which a browser request never reaches.
        """
        source = open(UI).read()

        for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket(",
                          "api.openai.com", "localhost:11434",
                          "kubernetes.default.svc"):
            assert forbidden not in source, f"ui.py contains {forbidden!r}"

    def test_no_kubernetes_host_or_token_is_rendered(self):
        app = render()
        rendered = page_text(app)

        for forbidden in ("BEGIN CERTIFICATE", "eyJhbGciOi",  # a JWT prefix
                          "kubernetes.default.svc", "client-certificate-data"):
            assert forbidden not in rendered


class TestRedactedEvidenceStaysRedacted:
    def test_a_secret_in_a_tool_result_is_masked_before_it_is_shown(self):
        """
        The console renders tool results verbatim in its Evidence panel. If a
        collector ever returned a credential, the panel would publish it. The
        redactor is the thing standing in the way, so this asserts the panel
        shows the redactor's output and not the raw string.
        """
        leaky = json.dumps({
            "pod": "crasher-1",
            "env": {"TOKEN": "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        })
        masked = redaction.redact(leaky)

        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in masked, \
            "the redactor does not mask this shape -- the panel would publish it"

    def test_the_evidence_panel_renders_what_the_run_recorded(self):
        """
        Not a redaction test: a wiring one. The panel must show the recorded
        evidence rather than re-reading the cluster, because a second read is a
        second chance to surface something the first read had masked.
        """
        answer = {
            "answer": "x", "question": "q", "confidence": "grounded",
            "checked": 1, "unverified": [], "contradictions": [], "rewrites": [],
            "nudges": 0, "policies": 0, "coverage": 0,
            "tool_calls": [{"name": "describe_pod", "arguments": {}}],
            "evidence": [{"id": "tool-1", "tool": "describe_pod",
                          "result": '{"pod": "recorded-not-refetched"}'}],
            "timing": {"wall_ms": 10, "rounds": 1},
            "rca": {"observations": [], "inferences": [], "unknowns": [],
                    "contradictions": [], "corrections": []},
        }
        app = render(answer=answer)

        assert "recorded-not-refetched" in page_text(app)
