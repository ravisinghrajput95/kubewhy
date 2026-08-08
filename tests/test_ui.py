"""
Tests for the browser UI.

Streamlit's AppTest runs ui.py headlessly, so these need no browser. They skip
entirely when streamlit is absent, because it lives in requirements-ui.txt and
the default install deliberately does not have it.

The property worth testing is not layout. It is that a collector's failure
modes stay visible: the collectors return {"error": ...} rather than raising,
and a UI that renders a 403 as an empty table would tell an operator that
nothing is wrong during an incident.
"""

import os
from unittest.mock import patch

import pytest

pytest.importorskip("streamlit", reason="UI extra not installed (requirements-ui.txt)")

from streamlit.testing.v1 import AppTest  # noqa: E402

import routers.k8s_pods_info as k8s  # noqa: E402

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui.py")

FINDINGS = {
    "staging/payments-api": {
        "status": "ImagePullBackOff",
        "pods": 3,
        "example": "payments-api-66df957946-2hl47",
        "fault": "image-pull",
    },
    "demo/memory-hog": {
        "status": "OOMKilled",
        "pods": 1,
        "example": "memory-hog-bc76968c6-s24kn",
        "fault": "crash",
    },
}


def run(scan_result, nodes=None):
    """Run ui.py with the collectors stubbed, returning the finished app."""
    import streamlit as st

    # cache_data persists for the process, so one test's cluster would
    # otherwise still be on screen during the next.
    st.cache_data.clear()

    with patch.object(k8s, "scan_cluster", return_value=scan_result), patch.object(
        k8s, "list_nodes", return_value=nodes or {}
    ), patch.object(k8s, "describe_pod", return_value={"pod": "x", "containers": {}}), patch.object(
        k8s, "get_pod_events", return_value={"pod": "x", "events": []}
    ), patch.object(
        k8s, "get_pod_logs", return_value={"pod": "x", "source": "current", "logs": "boom"}
    ):
        app = AppTest.from_file(UI, default_timeout=30)
        app.run()
    return app


class TestFailureModesStayVisible:
    def test_cluster_error_is_shown_as_an_error(self):
        app = run({"error": "kubernetes API error 403: Forbidden"})

        assert any("403" in element.value for element in app.error)
        # The critical part: no table implying a healthy cluster.
        assert not app.dataframe

    def test_unreachable_cluster_is_shown_as_an_error(self):
        app = run({"error": "cluster unreachable: MaxRetryError"})

        assert any("unreachable" in element.value for element in app.error)

    def test_clean_cluster_is_information_not_an_error(self):
        """A healthy cluster must not look like a broken UI."""
        app = run({"result": "no unhealthy workloads in any namespace"})

        assert not app.error
        assert any("no unhealthy workloads" in element.value for element in app.info)

    def test_truncation_is_surfaced(self):
        # Silently dropping workloads during an incident is the worst
        # available behaviour, so it renders as a warning.
        app = run({**FINDINGS, "_truncated": "12 more not shown, across 4 namespace(s)"})

        assert any("12 more not shown" in element.value for element in app.warning)


class TestRendersFindings:
    def test_one_row_per_workload(self):
        app = run(FINDINGS)

        assert not app.error
        # Streamlit converts the list of dicts to a DataFrame on the way in.
        assert len(app.dataframe[0].value) == len(FINDINGS)

    def test_truncation_marker_is_not_rendered_as_a_workload(self):
        """_truncated is a message, not a failing workload."""
        app = run({**FINDINGS, "_truncated": "1 more not shown, across 1 namespace(s)"})

        workloads = list(app.dataframe[0].value["workload"])
        assert "_truncated" not in workloads
        assert len(workloads) == len(FINDINGS)

    def test_names_the_tool_behind_each_panel(self):
        """'Shows its working' applies to this surface too."""
        app = run(FINDINGS)

        assert any("scan_cluster" in element.value for element in app.caption)


class TestContextIsPerSession:
    """
    Two browser sessions in one process are two callers. A process-wide context
    meant one switching cluster switched it under the other, which then went on
    rendering with a label naming a cluster it was no longer reading.
    """

    def test_each_session_binds_its_own_context(self):
        import streamlit as st

        st.cache_data.clear()
        bound = []

        with patch.object(k8s, "scan_cluster", return_value={}), patch.object(
            k8s, "list_nodes", return_value={}
        ), patch.object(k8s, "list_contexts", return_value=["cluster-a", "cluster-b"]), patch.object(
            k8s, "use_context", side_effect=bound.append
        ), patch.object(
            k8s, "active_context", return_value="cluster-a"
        ):
            first = AppTest.from_file(UI, default_timeout=30)
            first.session_state["context"] = "cluster-a"
            first.run()

            second = AppTest.from_file(UI, default_timeout=30)
            second.session_state["context"] = "cluster-b"
            second.run()

        # Each run rebinds from its own session state rather than inheriting
        # whatever the last session happened to select.
        assert "cluster-a" in bound and "cluster-b" in bound
        assert not first.exception and not second.exception

    def test_the_context_is_part_of_every_cache_key(self):
        """
        st.cache_data is shared by the whole process, so without the context in
        the key a session on one cluster serves another session's results.
        """
        import inspect

        import ui

        for name in ("_scan", "_namespaces", "_workload_pods", "_describe", "_events", "_logs", "_nodes"):
            first = list(inspect.signature(getattr(ui, name)).parameters)[0]
            assert first == "context", f"{name} is cached without the context in its key"
