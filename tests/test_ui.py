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
import sys
from unittest.mock import patch

import pytest

pytest.importorskip("streamlit", reason="UI extra not installed (requirements-ui.txt)")

from streamlit.delta_generator_singletons import get_dg_singleton_instance  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

import routers.k8s_pods_info as k8s  # noqa: E402

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui.py")


def clear_leaked_form_state():
    """Drop the form context left on Streamlit's process-wide singletons."""
    singletons = get_dg_singleton_instance()
    singletons.main_dg._form_data = None
    singletons.sidebar_dg._form_data = None


@pytest.fixture(autouse=True)
def _no_form_state_left_behind():
    """
    Undo the form context `import ui` leaves in the process.

    ui.py is a Streamlit script, so importing it executes every top-level call
    in bare mode -- including `with st.form("ask")`. That leaves
    FormData(form_id='ask') on the main DeltaGenerator, which is a
    process-wide singleton and is not reset between tests. Any later test that
    renders a button then dies with "st.button() can't be used in an
    st.form()", and whether it does depends purely on test order.

    Measured rather than guessed: main_dg._form_data is None before the import
    and FormData(form_id='ask') after it. That single attribute is the whole
    leak, so clearing it is the fix rather than a workaround for symptoms.
    """
    yield
    clear_leaked_form_state()

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


class TestImportingUiDoesNotBreakLaterTests:
    """
    The suite used to depend on its own ordering, which is the kind of defect
    that makes every other result untrustworthy: a test that passes only
    because of what ran before it is not evidence about the code.
    """

    @staticmethod
    def _import_ui_afresh():
        """
        Force ui.py to execute, rather than be handed back from sys.modules.

        Another test in this file imports ui, so a plain `import ui` here is a
        no-op that leaks nothing -- which would make both of these tests pass
        while demonstrating nothing at all.
        """
        sys.modules.pop("ui", None)
        import ui  # noqa: F401

    def test_importing_ui_leaves_form_state_behind(self):
        """
        Pins the mechanism. If Streamlit renames the attribute this breaks
        loudly here, rather than quietly turning the cleanup into a no-op and
        letting the ordering hazard back in unannounced.
        """
        clear_leaked_form_state()
        assert get_dg_singleton_instance().main_dg._form_data is None

        self._import_ui_afresh()

        leaked = get_dg_singleton_instance().main_dg._form_data
        assert leaked is not None, "import ui no longer leaks; the fixture may be dead code"
        assert leaked.form_id == "ask"

    def test_the_app_still_renders_once_that_state_is_cleared(self):
        """The symptom: st.button() raising because a form is still open."""
        self._import_ui_afresh()

        clear_leaked_form_state()

        app = run({})

        assert not app.exception


class TestProgressIsTextNotOnlyAnimation:
    """
    The spinner beside "Thinking..." is a CSS animation, and a browser stops
    painting frames whenever the tab is hidden, occluded or busy. Measured in
    Chrome with the tab backgrounded: requestAnimationFrame fired zero times in
    1.2s while the animation still reported playState "running". It freezes
    mid-rotation into a static arc that reads as a chevron, and a diagnosis
    that is working becomes indistinguishable from one that has hung.

    The label is a pure function and is tested as one. That was originally
    forced -- importing ui in another test left Streamlit form state in the
    process, so driving the form here failed depending on test order -- but the
    ordering hazard is now cleared between tests, so this is a choice about the
    right level to test at rather than a way around a broken suite.
    """

    def test_label_names_the_tool_the_count_and_the_elapsed_time(self):
        import ui

        label = ui.progress_label("get_pod_logs", 3, 42.4)

        assert "get_pod_logs" in label
        assert "tool 3" in label
        assert "42s" in label

    def test_elapsed_is_whole_seconds(self):
        # Sub-second precision on a minute-long run is noise that changes on
        # every event and makes the label look unstable.
        import ui

        assert "0s" in ui.progress_label("list_pods", 1, 0.4)
        assert "." not in ui.progress_label("list_pods", 1, 12.34)

    def test_a_stalled_run_is_legible_as_a_long_one(self):
        # Ollama has stalled for 1013s in this project. The label has to make
        # that visible rather than looking identical to a fast run.
        import ui

        assert "1013s" in ui.progress_label("get_pod_logs", 2, 1013.0)


class TestSearchCoversTheClusterNotJustThePage:
    """
    A filter that only sees the current page cannot say a thing is absent.

    It used to answer "nothing matching X in the 20 workloads scanned" --
    honest about its scope and useless as an answer, because the workload may
    exist just outside the limit, or be healthy and therefore never scanned at
    all. scan_cluster(workload=...) reports one workload by name whether or
    not anything is wrong with it, which is what distinguishes "not on this
    page" from "not in this cluster".
    """

    @staticmethod
    def scanner(elsewhere=None):
        """Page scan returns FINDINGS; a by-name lookup returns `elsewhere`."""
        def fake(only_unhealthy=True, limit=20, namespaces="", workload=""):
            if workload:
                return elsewhere or {
                    "result": f"no workload named {workload} exists in this cluster"
                }
            return dict(FINDINGS)
        return fake

    def _search(self, scan, term):
        import streamlit as st

        st.cache_data.clear()
        with patch.object(k8s, "scan_cluster", side_effect=scan), patch.object(
            k8s, "list_nodes", return_value={}
        ), patch.object(
            k8s, "describe_pod", return_value={"pod": "x", "containers": {}}
        ), patch.object(
            k8s, "get_pod_events", return_value={"pod": "x", "events": []}
        ), patch.object(
            k8s, "get_pod_logs",
            return_value={"pod": "x", "source": "current", "logs": "boom"},
        ):
            app = AppTest.from_file(UI, default_timeout=30)
            app.run()
            # By label, not index: text_input[0] is the ask panel's
            # "Question" box, so an index here silently types the search term
            # into the wrong widget and asserts against an unfiltered page.
            filter_box = next(t for t in app.text_input if t.label == "Filter")
            filter_box.set_value(term).run()
        return app

    def test_a_name_already_on_the_page_is_filtered_locally(self):
        app = self._search(self.scanner(), "memory-hog")

        workloads = list(app.dataframe[0].value["workload"])
        assert workloads == ["demo/memory-hog"]
        # No fallback caption: it was on the page, so nothing was looked up.
        assert not any("found by asking" in c.value for c in app.caption)

    def test_a_workload_outside_the_page_is_found_in_the_cluster(self):
        elsewhere = {
            "prod/billing": {
                "status": "Running", "pods": 2,
                "example": "billing-7d9f-abcde", "fault": None,
            }
        }
        app = self._search(self.scanner(elsewhere), "billing")

        assert list(app.dataframe[0].value["workload"]) == ["prod/billing"]
        assert any("found by asking" in c.value for c in app.caption)

    def test_a_workload_that_does_not_exist_is_reported_as_absent(self):
        """
        The distinction that matters: absent from the cluster, not merely
        absent from the page.
        """
        app = self._search(self.scanner(), "nosuchthing")

        assert any(
            "no workload named nosuchthing exists in this cluster" in i.value
            for i in app.info
        )
        assert not app.error

    def test_the_lookup_names_the_tool_behind_it(self):
        """Every panel names its tool; the fallback must not be the exception."""
        app = self._search(self.scanner(), "nosuchthing")

        assert any("scan_cluster(workload='nosuchthing')" in c.value for c in app.caption)


# --- the investigation panel -------------------------------------------------
#
# The investigation is the primary object on this page, so what it renders is
# pinned rather than eyeballed. Every field below comes from what agent.stream()
# already returns; the panel is asserted to *display* the backend's verdict, not
# to compute one of its own.

ANSWER = {
    "answer": "memory-hog was OOMKilled after exceeding its 64Mi limit.",
    "question": "why is memory-hog restarting?",
    "confidence": "grounded",
    "checked": 3,
    "unverified": [],
    "contradictions": [],
    "nudges": 0, "policies": 1, "coverage": 0,
    "rewrites": [{"claim": "512Mi", "observed": "64Mi", "action": "corrected"}],
    "tool_calls": [
        {"name": "list_pods", "arguments": {"namespace": "demo"}},
        {"name": "describe_pod", "arguments": {"name": "memory-hog-x",
                                               "namespace": "demo"}},
    ],
    "evidence": [
        {"id": "tool-1", "tool": "list_pods", "result": '{"memory-hog-x": {}}'},
        {"id": "tool-2", "tool": "describe_pod",
         "result": '{"pod": "memory-hog-x", "namespace": "demo",'
                   '"containers": {"hog": {"last_termination":'
                   '{"reason": "OOMKilled", "exit_code": 137}}}}'},
    ],
    "timing": {"wall_ms": 8400, "model_ms": 8000, "tool_ms": 400, "rounds": 3},
    "rca": {
        "observations": [{"claim": "oomkilled", "kind": "status",
                          "evidence": [{"id": "tool-2", "tool": "describe_pod",
                                        "field": "last_termination.reason"}]}],
        "inferences": [{"claim": "memory leak", "kind": "cause"}],
        "unknowns": ["512"],
        "contradictions": [],
        "corrections": [],
    },
}


def render_answer(answer, scan_result=None):
    """Run the app with a finished investigation already in session state."""
    import streamlit as st

    st.cache_data.clear()
    with patch.object(k8s, "scan_cluster",
                      return_value=scan_result or {"result": "no unhealthy workloads"}), \
         patch.object(k8s, "list_nodes", return_value={}):
        app = AppTest.from_file(UI, default_timeout=60)
        app.session_state["answer"] = answer
        app.run()
    # A raised exception blanks the page from that point down, so it is caught
    # here rather than in the one test that happens to assert past it. An
    # invalid `icon=` argument crashed the whole console on every contradiction
    # -- the single verdict most worth reading -- and only this caught it.
    assert not app.exception, [str(e.value) for e in app.exception]
    return app


def _html(app, prefix):
    return [m.value for m in app.markdown if str(m.value).startswith(prefix)]


class TestTheInvestigationIsThePrimaryObject:
    def test_every_section_of_the_workflow_is_on_screen(self):
        """
        Root cause, what the evidence says, and the timeline. An operator has
        to be able to see what was collected and what it supports without
        reading prose.
        """
        app = render_answer(ANSWER)
        headings = [m.value for m in app.markdown
                    if str(m.value).startswith("####")]

        assert "#### Root cause" in headings
        assert "#### What the evidence says" in headings
        assert "#### Timeline" in headings

    def test_the_status_strip_carries_verdict_calls_and_duration(self):
        app = render_answer(ANSWER)
        strip = _html(app, "<div class='kw-strip'>")

        assert strip
        assert "Grounded" in strip[0]
        assert "2 tool calls" in strip[0]
        assert "8.4s" in strip[0]

    def test_observations_carry_the_tool_and_field_they_came_from(self):
        """
        "Traced to a tool result" is only a claim unless the trace is shown.
        """
        app = render_answer(ANSWER)
        claims = _html(app, "<div class='kw-claim")

        cited = [c for c in claims if "oomkilled" in c]
        assert cited, "the observation was not rendered"
        assert "describe_pod.last_termination.reason" in cited[0]

    def test_inference_and_unknown_are_rendered_distinctly(self):
        app = render_answer(ANSWER)
        claims = _html(app, "<div class='kw-claim")

        assert any("memory leak" in c and "kw-warn" in c for c in claims)
        assert any("512" in c and "kw-bad" in c for c in claims)

    def test_a_correction_says_what_was_changed_in_the_text(self):
        """
        verify() rewrites a fabricated value in place. Saying so is the
        difference between a corrected answer and an edited one.
        """
        app = render_answer(ANSWER)
        body = " ".join(str(m.value) for m in app.markdown)

        assert "512Mi" in body and "64Mi" in body

    def test_the_evidence_itself_is_available(self):
        app = render_answer(ANSWER)

        assert any("Evidence" in (e.label or "") for e in app.expander)


class TestTheVerdictIsTheBackendsNotTheViews:
    def test_a_contradiction_is_surfaced_as_an_error(self):
        answer = {**ANSWER, "confidence": "contradicted",
                  "contradictions": [{"claim": "application error",
                                      "measured": "last_termination.reason = oomkilled",
                                      "rule": "imposed_termination_vs_application_cause"}]}
        app = render_answer(answer)

        assert any("application error" in e.value and "oomkilled" in e.value
                   for e in app.error)

    def test_an_answer_with_no_tool_calls_is_flagged(self):
        """
        `grounded` with nothing measured is the failure grounding.py exists to
        prevent, so the panel says so rather than showing a green badge.
        """
        app = render_answer({**ANSWER, "tool_calls": [], "checked": 0})

        assert any("no tools were called" in w.value for w in app.warning)

    def test_a_deadline_termination_is_visible_in_the_strip(self):
        app = render_answer({**ANSWER, "termination": "deadline_exceeded"})
        strip = _html(app, "<div class='kw-strip'>")

        assert "deadline exceeded" in strip[0]


class TestTheNextStepIsBorrowedNotInvented:
    """
    The recommendation comes from agent.evidence_gap() -- the same function the
    loop uses to decide whether to send a run back. A console that wrote its own
    suggestion would be inventing an AI capability this project does not ship.
    """

    def test_a_crashing_pod_whose_logs_are_unread_gets_a_next_step(self):
        answer = {**ANSWER,
                  "question": "why is crasher-x failing?",
                  "tool_calls": [{"name": "describe_pod",
                                  "arguments": {"name": "crasher-x",
                                                "namespace": "demo"}}],
                  "evidence": [{"id": "tool-1", "tool": "describe_pod",
                                "result": '{"pod": "crasher-x", "namespace": "demo",'
                                          '"status": "CrashLoopBackOff",'
                                          '"containers": {"c": {"last_termination":'
                                          '{"reason": "Error", "exit_code": 1}}}}'}]}
        app = render_answer(answer)
        blocks = _html(app, "<div class='kw-next'>")

        assert blocks, "no next step offered for an unread crashing pod"
        assert "get_pod_logs" in blocks[0]
        assert "crasher-x" in blocks[0]

    def test_a_complete_investigation_gets_no_invented_suggestion(self):
        """The panel stays silent when the backend has nothing to insist on."""
        app = render_answer(ANSWER)

        assert not _html(app, "<div class='kw-next'>")


class TestTheHeaderNamesWhatThisDeploymentIsDoing:
    def test_it_names_the_cluster_the_backend_and_where_evidence_goes(self):
        app = render_answer(ANSWER)
        header = _html(app, "<div class='kw-hdr'>")

        assert header, "no header strip"
        assert "cluster" in header[0]
        assert "inference" in header[0]
        # Where inference happens decides what leaves the network, so it is on
        # screen rather than in a settings page nobody opens.
        assert "evidence" in header[0]
