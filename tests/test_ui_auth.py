"""
The console's authentication gate.

The interesting assertion in here is not that a refusal renders. It is that a
refused caller causes no cluster read: st.stop() has to fire before the scan,
or the page would collect pod logs for someone it just declined to name and
merely decline to show them.

Streamlit's AppTest reports st.context.headers as an empty mapping, so the
refusal path is the natural one here and the admitted path is the one that
needs the header injected.
"""

import os
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

pytest.importorskip("streamlit", reason="UI extra not installed (requirements-ui.txt)")

from streamlit.delta_generator_singletons import get_dg_singleton_instance  # noqa: E402
from streamlit.runtime.context import ContextProxy  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

import routers.k8s_pods_info as k8s  # noqa: E402

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui.py")

FINDINGS = {
    "demo/memory-hog": {
        "status": "OOMKilled",
        "pods": 1,
        "example": "memory-hog-bc76968c6-s24kn",
        "fault": "crash",
    },
}


@pytest.fixture(autouse=True)
def _no_form_state_left_behind():
    """See test_ui.py: `import ui` leaks form context onto a process-wide singleton."""
    yield
    singletons = get_dg_singleton_instance()
    singletons.main_dg._form_data = None
    singletons.sidebar_dg._form_data = None


def run(headers=None, address="127.0.0.1"):
    """
    ui.py with the cluster stubbed, returning the app and the scan stub.

    The stub is returned because "was the cluster read?" is the assertion this
    module exists to make, and it cannot be made from the rendered elements.
    """
    import streamlit as st

    st.cache_data.clear()
    scan = MagicMock(return_value=FINDINGS)
    real_get_option = st.get_option

    def get_option(name):
        return address if name == "server.address" else real_get_option(name)

    with patch.object(ContextProxy, "headers", new_callable=PropertyMock) as header_prop, \
         patch.object(k8s, "scan_cluster", scan), \
         patch.object(k8s, "list_nodes", return_value={}), \
         patch.object(st, "get_option", get_option):
        header_prop.return_value = dict(headers or {})
        app = AppTest.from_file(UI, default_timeout=30)
        app.run()

    return app, scan


def refusals(app):
    return [e.value for e in app.error if "Not signed in" in e.value]


class TestUnauthenticatedIsTheDefault:
    """`none` mode: a laptop on loopback, where the OS is the access control."""

    def test_the_page_renders(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_AUTH_MODE", raising=False)
        app, scan = run()

        assert not refusals(app)
        assert scan.called

    def test_nobody_is_named(self, monkeypatch):
        """No sign-in line, rather than one reading 'anonymous'."""
        monkeypatch.delenv("TRIAGE_AUTH_MODE", raising=False)
        app, _ = run()

        assert not any("Signed in as" in c.value for c in app.sidebar.caption)

    def test_loopback_earns_no_warning(self, monkeypatch):
        """
        The documented default must not carry a permanent red box. A banner on
        the ordinary case is a banner people learn to skip past.
        """
        monkeypatch.delenv("TRIAGE_AUTH_MODE", raising=False)
        app, _ = run(address="127.0.0.1")

        assert not any("no authentication" in w.value for w in app.warning)

    def test_binding_every_interface_without_auth_is_warned_about(self, monkeypatch):
        """The combination that is actually dangerous, and only that one."""
        monkeypatch.delenv("TRIAGE_AUTH_MODE", raising=False)
        app, _ = run(address="0.0.0.0")

        assert any("no authentication" in w.value for w in app.warning)

    def test_the_warning_is_not_shown_once_a_proxy_is_in_front(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        app, _ = run(headers={"X-Forwarded-Email": "sre@example.com"}, address="0.0.0.0")

        assert not any("no authentication" in w.value for w in app.warning)


class TestProxyMode:
    def test_a_proxied_identity_is_admitted(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        app, scan = run(headers={"X-Forwarded-Email": "sre@example.com"})

        assert not refusals(app)
        assert scan.called

    def test_the_viewer_is_named_in_the_sidebar(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        app, _ = run(headers={"X-Forwarded-Email": "sre@example.com"})

        assert any("sre@example.com" in c.value for c in app.sidebar.caption)

    def test_no_identity_is_refused(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        app, _ = run()

        assert refusals(app)

    def test_a_refused_caller_causes_no_cluster_read(self, monkeypatch):
        """
        The one that matters. A page that scans the cluster and then declines
        to render it has already read the pod logs of someone it could not
        name -- and on a shared console that read is the disclosure, not the
        rendering.
        """
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        _, scan = run()

        assert not scan.called

    def test_an_empty_identity_header_is_refused(self, monkeypatch):
        """oauth2-proxy sends one when the provider released no email claim."""
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        app, scan = run(headers={"X-Forwarded-Email": ""})

        assert refusals(app)
        assert not scan.called

    def test_the_refusal_says_why(self, monkeypatch):
        """
        Whoever hits this is either locked out of an incident or looking at a
        misconfigured proxy, and cannot tell which from "access denied".
        """
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        app, _ = run()

        assert any("no identity header" in message for message in refusals(app))

    def test_the_refusal_does_not_blank_the_page(self, monkeypatch):
        """
        st.error(icon="X") is not a valid emoji; Streamlit raises on it and
        the raise blanks the page. That happened here once, on every
        contradiction, and a refusal that blanks the page cannot tell anyone
        why they were refused.
        """
        monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")
        app, _ = run()

        assert not app.exception
        assert refusals(app)
