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
import re
import sys
from types import SimpleNamespace
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
        """
        Page scan returns FINDINGS; a by-name lookup returns `elsewhere`.

        The fake honours only_unhealthy, because that flag is the whole
        mechanism here. The real scan_cluster(only_unhealthy=True) never
        reports a healthy workload, so a lookup that forgot to pass False
        would answer "no workload named billing exists in this cluster" about
        a workload that is running fine -- the exact confusion this class
        exists to prevent. A fake that ignored the flag could not tell the
        two apart, and did not: the flipped flag survived mutation testing.
        """
        def fake(only_unhealthy=True, limit=20, namespaces="", workload=""):
            if workload:
                found = dict(elsewhere or {})
                if only_unhealthy:
                    found = {k: v for k, v in found.items() if v.get("fault")}
                return found or {
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

    def test_a_namespace_matches_even_though_no_pod_name_does(self):
        """
        Either half of the row matches: the workload key or the example pod.
        Requiring both would drop every search for a namespace, since a pod
        name does not contain the namespace it runs in.
        """
        app = self._search(self.scanner(), "demo")

        assert list(app.dataframe[0].value["workload"]) == ["demo/memory-hog"]
        assert not any("found by asking" in c.value for c in app.caption), \
            "a row that was on the page was not matched locally"

    def test_a_name_already_on_the_page_is_filtered_locally(self):
        app = self._search(self.scanner(), "memory-hog")

        workloads = list(app.dataframe[0].value["workload"])
        assert workloads == ["demo/memory-hog"]
        # No fallback caption: it was on the page, so nothing was looked up.
        assert not any("found by asking" in c.value for c in app.caption)

    def test_a_workload_outside_the_page_is_found_in_the_cluster(self):
        """
        And it is healthy, deliberately. A workload with something wrong with
        it would come back from either kind of scan; only a healthy one proves
        the lookup asked for every workload rather than the unhealthy ones.
        """
        elsewhere = {
            "prod/billing": {
                "status": "Running", "pods": 2,
                "example": "billing-7d9f-abcde", "fault": None,
            }
        }
        app = self._search(self.scanner(elsewhere), "billing")

        assert list(app.dataframe[0].value["workload"]) == ["prod/billing"]
        assert any("found by asking" in c.value for c in app.caption)

    def test_the_by_name_lookup_asks_for_healthy_workloads_too(self):
        """
        The flag itself, at the call. The test above asserts the consequence;
        this one names the cause, so a failure says which of the two broke.
        """
        elsewhere = {"prod/billing": {"status": "Running", "pods": 2,
                                      "example": "billing-7d9f-abcde",
                                      "fault": None}}
        calls = []

        def recording(only_unhealthy=True, limit=20, namespaces="", workload=""):
            calls.append({"only_unhealthy": only_unhealthy, "workload": workload})
            return self.scanner(elsewhere)(only_unhealthy, limit, namespaces, workload)

        self._search(recording, "billing")
        by_name = [c for c in calls if c["workload"]]

        assert by_name, "no by-name lookup happened"
        assert all(c["only_unhealthy"] is False for c in by_name)
        # The page scan is the other way round, and stays that way.
        assert all(c["only_unhealthy"] is True for c in calls if not c["workload"])

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


# --- submitting the form -----------------------------------------------------

def run_form(seen, question=None, scoped=True, scan=None):
    """
    Drive the Ask form for real, with the agent loop stubbed.

    `seen` collects the question string the agent was actually handed, which is
    the only way to tell "the button did nothing" apart from "the button ran
    something else".
    """
    import streamlit as st
    import agent as agent_mod

    st.cache_data.clear()

    def fake_stream(asked, *args, **kwargs):
        seen.append(asked)
        yield {"type": "answer", **ANSWER}

    with patch.object(k8s, "scan_cluster",
                      return_value=scan or {"demo/memory-hog": {
                          "status": "OOMKilled", "pods": 1,
                          "example": "memory-hog-bc76968c6-s24kn",
                          "fault": "crash"}}), \
         patch.object(k8s, "list_nodes", return_value={}), \
         patch.object(k8s, "describe_pod",
                      return_value={"pod": "x", "containers": {}}), \
         patch.object(k8s, "get_pod_events",
                      return_value={"pod": "x", "events": []}), \
         patch.object(k8s, "get_pod_logs",
                      return_value={"pod": "x", "source": "current", "logs": "b"}), \
         patch.object(agent_mod, "stream", fake_stream):
        app = AppTest.from_file(UI, default_timeout=60)
        app.run()
        if question is not None:
            app.text_input[0].set_value(question)
        if not scoped:
            app.checkbox[0].set_value(False)
        app.button[0].click().run()
    return app


class TestTheDiagnoseButtonAlwaysDoesSomething:
    def test_an_empty_box_asks_the_question_the_box_is_showing(self):
        """
        The placeholder is grey text *inside* the field, which is what a
        filled-in field looks like. Clicking Diagnose used to do nothing at
        all -- no run, no message, an `and question` guard failing in silence.
        Reported as "the Diagnose button seems not responding", and that is
        precisely what it was.
        """
        seen = []
        run_form(seen, question=None)

        assert seen, "Diagnose with an empty box did nothing at all"
        assert "demo/memory-hog" in seen[0]

    def test_a_typed_question_is_still_the_one_asked(self):
        seen = []
        run_form(seen, question="why does it restart?")

        assert seen and "why does it restart?" in seen[0]

    def test_with_nothing_selected_it_says_so_rather_than_going_quiet(self):
        seen = []
        app = run_form(seen, question=None, scan={"result": "no unhealthy workloads"})

        assert not seen
        assert any("placeholder" in w.value for w in app.warning), \
            "a click that cannot run must say why"


class TestTheHistoryRecordsWhatAPersonTyped:
    def test_the_sidebar_shows_the_question_not_the_scoping_directive(self):
        """
        `question` is rebound to agent.scoped_question() before the run, and
        recording that put "Answer only about the workload demo/memory-hog..."
        in the sidebar where the operator's own sentence belonged. A history
        list is for finding the question you asked.
        """
        seen = []
        app = run_form(seen, question="why does it restart?", scoped=True)

        # The scoped prompt is what reached the model...
        assert "Answer only about" in seen[0]
        # ...and the typed question is what the sidebar offers back.
        labels = [b.label for b in app.button]
        assert any("why does it restart?" in label for label in labels)
        assert not any("Answer only about" in label for label in labels)

    def test_a_finished_investigation_appears_without_a_reload(self):
        """
        The sidebar is built near the top of the script, so before this fix the
        run you just finished was missing from it until something else redrew
        the page -- which read as history not being kept at all.

        Asserted as a *change* across the click, with a marker no other test
        can have written. The first version of this test looked for the "no
        investigations yet" caption being gone, and passed on the broken code:
        the store is a cache_resource shared for the whole process, so an
        earlier test had already put a row in it.
        """
        import streamlit as st
        import agent as agent_mod

        st.cache_data.clear()
        marker = "why did zzz-marker-9137 restart?"

        def fake_stream(asked, *args, **kwargs):
            yield {"type": "answer", **ANSWER}

        with patch.object(k8s, "scan_cluster",
                          return_value={"demo/memory-hog": {
                              "status": "OOMKilled", "pods": 1,
                              "example": "memory-hog-bc76968c6-s24kn",
                              "fault": "crash"}}), \
             patch.object(k8s, "list_nodes", return_value={}), \
             patch.object(k8s, "describe_pod",
                          return_value={"pod": "x", "containers": {}}), \
             patch.object(k8s, "get_pod_events",
                          return_value={"pod": "x", "events": []}), \
             patch.object(k8s, "get_pod_logs",
                          return_value={"pod": "x", "source": "c", "logs": "b"}), \
             patch.object(agent_mod, "stream", fake_stream):
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            before = [b.label for b in app.button]
            app.text_input[0].set_value(marker)
            app.button[0].click().run()
            after = [b.label for b in app.button]

        assert not any(marker in label for label in before)
        assert any(marker in label for label in after), \
            "the finished investigation is not in the sidebar until a reload"


class TestTheRewriteStaysDisclosed:
    def test_the_prompt_actually_sent_is_shown_with_the_answer(self):
        app = render_answer({**ANSWER,
                             "question": "why does it restart?",
                             "prompt": "Answer only about the workload "
                                       "demo/memory-hog. why does it restart?"})

        assert any("what was actually sent" in (e.label or "")
                   for e in app.expander)

    def test_an_unscoped_question_gets_no_disclosure_panel(self):
        app = render_answer(ANSWER)

        assert not any("what was actually sent" in (e.label or "")
                       for e in app.expander)


# --- states an investigation can end in --------------------------------------
#
# Section 2 of the demo-validation brief: a console is judged on what it does
# when a run does NOT go well, and those paths are the ones a demo never
# exercises. Each of these is a state the loop can genuinely produce.

class TestAnInvestigationThatDoesNotSucceed:
    def test_a_backend_failure_is_shown_and_the_page_stays_usable(self):
        """
        Anything reaching this handler is the model backend -- the loop hands
        tool failures back as data. The operator has to see it, and has to be
        able to try again without reloading.
        """
        import streamlit as st
        import agent as agent_mod

        st.cache_data.clear()

        def explode(*args, **kwargs):
            raise ConnectionError("model backend unreachable")
            yield  # pragma: no cover -- makes this a generator

        with patch.object(k8s, "scan_cluster", return_value={
                "demo/crasher": {"status": "CrashLoopBackOff", "pods": 1,
                                 "example": "crasher-1", "fault": "crash"}}), \
             patch.object(k8s, "list_nodes", return_value={}), \
             patch.object(k8s, "describe_pod",
                          return_value={"pod": "x", "containers": {}}), \
             patch.object(k8s, "get_pod_events",
                          return_value={"pod": "x", "events": []}), \
             patch.object(k8s, "get_pod_logs",
                          return_value={"pod": "x", "source": "c", "logs": "b"}), \
             patch.object(agent_mod, "stream", explode):
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            app.button[0].click().run()

        assert any("unreachable" in e.value for e in app.error), \
            "a backend failure was swallowed"
        # Still usable: the form is on the page, not replaced by a stack trace.
        assert app.button, "the page lost its controls"
        assert not app.exception

    def test_a_timed_out_investigation_says_so_and_invents_nothing(self):
        """
        `deadline_exceeded` is an operational outcome, not an answer. The strip
        must name it, and the panel must not dress the partial evidence up as a
        conclusion.
        """
        app = render_answer({**ANSWER,
                             "termination": "deadline_exceeded",
                             "confidence": "insufficient_evidence",
                             "answer": "Ran out of time before reaching a "
                                       "conclusion.",
                             "rca": {"observations": [], "inferences": [],
                                     "unknowns": [], "contradictions": [],
                                     "corrections": []}})
        strip = _html(app, "<div class='kw-strip'>")

        assert "deadline exceeded" in strip[0]
        assert "Insufficient evidence" in strip[0]
        # The evidence it did collect is still there to read.
        assert any("Evidence" in (e.label or "") for e in app.expander)

    def test_a_run_that_hit_max_rounds_is_labelled_as_such(self):
        app = render_answer({**ANSWER, "termination": "max_rounds"})

        assert "max rounds" in _html(app, "<div class='kw-strip'>")[0]


class TestTheUiDisplaysTheBackendsGroundingNotItsOwn:
    """
    The rule the console is built on. A second implementation of the checker in
    the view is how a console comes to disagree with its own backend, and then
    neither one is obviously right.
    """

    def test_the_verdict_shown_is_the_verdict_supplied(self):
        """
        Deliberately inconsistent input: the backend says `grounded` while the
        contract carries an unknown. A view that recomputed would downgrade it.
        The view must show what it was given.
        """
        app = render_answer({**ANSWER,
                             "confidence": "grounded",
                             "rca": {"observations": [],
                                     "inferences": [],
                                     "unknowns": ["something unsupported"],
                                     "contradictions": [], "corrections": []}})
        strip = _html(app, "<div class='kw-strip'>")

        assert "Grounded" in strip[0]
        assert "Partial" not in strip[0] and "Ungrounded" not in strip[0]

    def test_an_unrecognised_verdict_is_shown_rather_than_guessed_at(self):
        """
        If grounding.py grows a sixth verdict, the console must render it, not
        silently map it onto one of the five it knows.
        """
        app = render_answer({**ANSWER, "confidence": "provisional"})

        assert "provisional" in _html(app, "<div class='kw-strip'>")[0]

    def test_claim_counts_come_from_the_contract(self):
        app = render_answer({**ANSWER,
                             "rca": {"observations": [{"claim": "a", "kind": "s"},
                                                      {"claim": "b", "kind": "s"}],
                                     "inferences": [{"claim": "c"}],
                                     "unknowns": ["d", "e", "f"],
                                     "contradictions": [], "corrections": []}})
        body = " ".join(str(m.value) for m in app.markdown)

        assert "**Observed** · 2" in body
        assert "**Inferred** · 1" in body
        assert "**Unknown** · 3" in body


# --- markup reaches the reader as markup --------------------------------------

MARKUP = re.compile(r"<[a-zA-Z/][^>]*>")


def painted(app):
    """Every rendered markdown element that carries a tag."""
    return [m for m in app.markdown if MARKUP.search(str(m.value))]


class TestRenderedMarkupIsRenderedAsMarkup:
    """
    The console writes its own HTML for the strip, the header, the claim
    columns and the next step. `st.markdown` renders that only because the
    call passes `unsafe_allow_html=True`; without it the reader gets visible
    angle brackets, which is defect 16 exactly -- it shipped once, in the red
    box announcing a contradiction.

    `test_ui_markup.py` says AppTest reads the string submitted rather than the
    text painted, and for `st.error` that is true and unfixable: it has no such
    flag, and the escaping happens in the browser. For `st.markdown` it is not.
    `allow_html` is a field on the markdown proto, so the element tree records
    which of the two a call asked for, and these assert on it. Verified on
    streamlit 1.61.1 against a two-line app: `proto.allow_html` came back True
    for the call that passed the flag and False for the one that did not.

    Mutation testing is why these exist. All eleven `unsafe_allow_html=True`
    in ui.py were flipped to False and the suite stayed green -- 43 tests over
    this element tree, none of which looked at the flag.
    """

    def _assert_nothing_is_escaped(self, app):
        rendered = painted(app)
        # The counter. A run that painted no markup would satisfy the real
        # assertion below vacuously, and so would a walk that stopped finding
        # markdown elements.
        assert rendered, "this run rendered no markup, so it proves nothing"

        escaped = [str(m.value)[:70] for m in rendered if not m.proto.allow_html]

        assert not escaped, (
            "rendered as visible angle brackets rather than as markup:\n  "
            + "\n  ".join(escaped)
        )

    def test_the_findings_page(self):
        self._assert_nothing_is_escaped(run(FINDINGS))

    def test_a_finished_investigation(self):
        self._assert_nothing_is_escaped(render_answer(ANSWER))

    def test_a_contradicted_answer(self):
        """The verdict the defect shipped on."""
        self._assert_nothing_is_escaped(render_answer(
            {**ANSWER, "confidence": "contradicted",
             "contradictions": [{"claim": "application error",
                                 "measured": "last_termination.reason = oomkilled",
                                 "rule": "imposed_termination_vs_application_cause"}]}))

    def test_an_answer_with_a_next_step(self):
        """A panel the other runs deliberately do not render."""
        self._assert_nothing_is_escaped(render_answer(
            {**ANSWER,
             "question": "why is crasher-x failing?",
             "tool_calls": [{"name": "describe_pod",
                             "arguments": {"name": "crasher-x",
                                           "namespace": "demo"}}],
             "evidence": [{"id": "tool-1", "tool": "describe_pod",
                           "result": '{"pod": "crasher-x", "namespace": "demo",'
                                     '"status": "CrashLoopBackOff",'
                                     '"containers": {"c": {"last_termination":'
                                     '{"reason": "Error", "exit_code": 1}}}}'}]}))

    def test_the_instrument_can_tell_the_two_apart(self, tmp_path):
        """
        The four tests above are worth having only if `allow_html` really
        distinguishes a rendered tag from an escaped one. Pinned here against
        a two-line app rather than against ui.py, so a Streamlit release that
        stopped recording the flag fails this one test instead of turning the
        other four silently green.
        """
        app_file = tmp_path / "two_markdowns.py"
        app_file.write_text(
            "import streamlit as st\n"
            "st.markdown(\"<span class='a'>rendered</span>\", "
            "unsafe_allow_html=True)\n"
            "st.markdown(\"<span class='b'>escaped</span>\")\n",
            encoding="utf-8",
        )
        app = AppTest.from_file(str(app_file), default_timeout=30).run()

        assert [m.proto.allow_html for m in app.markdown] == [True, False]
        # Both submitted the same shape of string, which is the point: the
        # value cannot tell them apart and the flag can.
        assert all(MARKUP.search(m.value) for m in app.markdown)


# --- choosing a pod within a workload ----------------------------------------
#
# A Deployment is its replicas, and everything below the workload selector is
# about one pod: the detail, the events, the logs and the container inside
# them. Nothing in this file exercised any of it before 2026-09-01 -- mutation
# testing flipped the pod comparison, the replica-count guard, the container
# guard and the vanished-workload guard, and the suite stayed green through
# all of them.

REPLICAS = [
    {"pod": "payments-api-1", "status": "ImagePullBackOff", "ready": False,
     "containers": ["api", "proxy"]},
    {"pod": "payments-api-2", "status": "Running", "ready": True,
     "containers": ["api", "proxy"]},
    {"pod": "payments-api-3", "status": "CrashLoopBackOff", "ready": False,
     "containers": ["api"]},
]


def run_workload(pods, choice="staging/payments-api", findings=None, pick=None):
    """
    The app with a workload selected and `workload_pods` stubbed.

    `pick` selects a pod from the replica radio by label and reruns, which is
    what a person does; the selection has to survive that rerun to be worth
    anything.
    """
    import streamlit as st

    st.cache_data.clear()
    with patch.object(k8s, "scan_cluster",
                      return_value=dict(FINDINGS if findings is None else findings)), \
         patch.object(k8s, "list_nodes", return_value={}), \
         patch.object(k8s, "workload_pods", return_value=pods), \
         patch.object(k8s, "describe_pod", return_value={"pod": "x", "containers": {}}), \
         patch.object(k8s, "get_pod_events", return_value={"pod": "x", "events": []}), \
         patch.object(k8s, "get_pod_logs",
                      return_value={"pod": "x", "source": "current", "logs": "boom"}):
        app = AppTest.from_file(UI, default_timeout=60)
        app.session_state["workload_choice"] = choice
        app.run()
        if pick is not None:
            next(r for r in app.radio if "pods in this workload" in r.label) \
                .set_value(pick).run()
    assert not app.exception, [str(e.value) for e in app.exception]
    return app


def pod_picker(app):
    return next((r for r in app.radio if "pods in this workload" in r.label), None)


def container_picker(app):
    return next((r for r in app.radio if r.label == "Container"), None)


def health_note(app):
    return [i.value for i in app.info if "Running and ready" in i.value]


class TestChoosingBetweenReplicas:
    """
    Three replicas can fail for three reasons, so the page offers all of them
    rather than the one the scan named as an example.
    """

    def test_every_pod_is_offered(self):
        picker = pod_picker(run_workload(REPLICAS))

        assert picker is not None, "no pod picker for a three-replica workload"
        assert len(picker.options) == 3
        assert all(pod["pod"] in " ".join(picker.options) for pod in REPLICAS)

    def test_two_replicas_still_get_a_choice(self):
        """Two is the smallest number that is not one, and the boundary the
        guard is written at."""
        assert pod_picker(run_workload(REPLICAS[:2])) is not None

    def test_one_replica_is_not_a_choice(self):
        """A radio with a single option is a decision nobody has to make."""
        assert pod_picker(run_workload(REPLICAS[:1])) is None

    def test_the_picked_pod_is_the_one_handed_to_the_ask_panel(self):
        """
        The Ask panel has no other idea what "the selected workload" means, so
        a pod chosen here and a pod investigated there must be the same one.
        """
        app = run_workload(REPLICAS,
                           pick="payments-api-3  —  CrashLoopBackOff  (not ready)")

        assert app.session_state["subject"]["pod"] == "payments-api-3"
        assert app.session_state["subject"]["workload"] == "staging/payments-api"

    def test_a_pod_that_is_not_ready_says_so_in_its_label(self):
        picker = pod_picker(run_workload(REPLICAS))

        assert "payments-api-2  —  Running" in picker.options
        assert "payments-api-1  —  ImagePullBackOff  (not ready)" in picker.options

    def test_a_workload_whose_pods_cannot_be_read_still_renders(self):
        """
        The collectors return {"error": ...} rather than raising, and the page
        has to survive that: no picker, no crash, and the example pod from the
        scan is still the subject.
        """
        app = run_workload({"error": "kubernetes API error 403: forbidden"})

        assert pod_picker(app) is None
        assert app.session_state["subject"]["pod"] == \
            FINDINGS["staging/payments-api"]["example"]


class TestTheContainerPickerFollowsThePod:
    """
    Sidecars make "the pod's logs" ambiguous, and picking silently shows the
    proxy while the app is what broke.
    """

    def test_a_multi_container_pod_offers_a_choice(self):
        picker = container_picker(run_workload(REPLICAS))

        assert picker is not None
        assert list(picker.options) == ["api", "proxy"]

    def test_a_single_container_pod_does_not(self):
        app = run_workload(REPLICAS,
                           pick="payments-api-3  —  CrashLoopBackOff  (not ready)")

        assert container_picker(app) is None

    def test_the_containers_are_the_selected_pods_own(self):
        """
        Not another pod's. With one replica the containers come from the entry
        that matches the example pod by name, and matching the wrong one shows
        a container list belonging to something else.
        """
        pods = [{"pod": FINDINGS["demo/memory-hog"]["example"], "status": "OOMKilled",
                 "ready": False, "containers": ["hog", "logshipper"]}]
        picker = container_picker(run_workload(pods, choice="demo/memory-hog",
                                               findings=FINDINGS))

        assert picker is not None, "the matching pod's containers were not found"
        assert list(picker.options) == ["hog", "logshipper"]

    def test_a_pod_the_scan_named_but_the_lookup_does_not_know(self):
        """No match, so no container list -- rather than the first pod's."""
        pods = [{"pod": "someone-elses-pod", "status": "Running", "ready": True,
                 "containers": ["a", "b"]}]

        assert container_picker(run_workload(pods, choice="demo/memory-hog")) is None


class TestEventsAreDatedAgainstTheCurrentState:
    """
    Events are history. A pod that waited on a taint keeps that warning for
    life, and a page that shows it without saying so presents a resolved
    problem as a current one.
    """

    def test_a_running_ready_pod_is_marked_as_healthy_now(self):
        app = run_workload(REPLICAS, pick="payments-api-2  —  Running")

        assert health_note(app), "no note on a pod that is healthy right now"
        assert "payments-api-2" in health_note(app)[0]

    def test_a_pod_that_is_still_broken_gets_no_such_note(self):
        assert not health_note(run_workload(REPLICAS))

    def test_running_but_not_ready_is_not_healthy(self):
        """
        Both halves are required. A pod stuck in a readiness probe failure is
        Running and is not serving traffic.
        """
        pods = [{"pod": "payments-api-1", "status": "Running", "ready": False,
                 "containers": ["api", "proxy"]},
                {"pod": "payments-api-2", "status": "Running", "ready": True,
                 "containers": ["api"]}]

        assert not health_note(run_workload(pods, pick="payments-api-1  —  Running  (not ready)"))

    def test_pods_that_cannot_be_read_are_not_assumed_healthy(self):
        assert not health_note(run_workload({"error": "cluster unreachable"}))


class TestAWorkloadThatLeavesTheScan:
    """
    `only_unhealthy` hides a workload the moment it recovers, and a CronJob's
    workload disappears every time its pods complete. Falling back to index 0
    then retargets the investigation to an unrelated workload without saying a
    word -- measured, demo/nightly-sync -> demo/bad-image, and the next
    Diagnose would have investigated a workload nobody chose.
    """

    GONE = "demo/nightly-sync"

    def test_it_stays_selected(self):
        app = run_workload(REPLICAS, choice=self.GONE)
        workload = next(s for s in app.selectbox if s.label == "Workload")

        assert workload.value == self.GONE
        assert workload.options[0] == self.GONE

    def test_it_does_not_silently_become_another_workload(self):
        """The failure this guard was written for: a target nobody chose."""
        app = run_workload(REPLICAS, choice=self.GONE)

        assert "subject" not in app.session_state or \
            app.session_state["subject"]["workload"] != "staging/payments-api"

    def test_the_page_says_what_happened(self):
        app = run_workload(REPLICAS, choice=self.GONE)

        assert any(self.GONE in w.value and "no longer in the scan" in w.value
                   for w in app.warning)

    def test_a_workload_still_in_the_scan_is_not_announced_as_gone(self):
        """The counter. A warning on the ordinary case is a warning nobody
        reads by the second incident."""
        app = run_workload(REPLICAS)

        assert not any("no longer in the scan" in w.value for w in app.warning)


class TestTheClaimColumnsSayWhenTheyAreEmpty:
    """
    Three columns, and an empty one has to read as empty rather than as a
    column that failed to render. The inverse matters as much: "— none —"
    under a list of claims is a page contradicting itself.
    """

    EMPTY = {"observations": [], "inferences": [], "unknowns": [],
             "contradictions": [], "corrections": []}

    def test_an_empty_run_says_so_in_every_column(self):
        app = render_answer({**ANSWER, "rca": self.EMPTY})

        assert len([c for c in app.caption if c.value == "— none —"]) == 3

    def test_a_full_run_says_it_in_none_of_them(self):
        app = render_answer(ANSWER)

        assert not [c for c in app.caption if c.value == "— none —"]


class TestTheTimelineIsTheCallsInOrder:
    """
    The timeline is the run's audit trail on screen: which tool, with which
    arguments, in which order. Nothing asserted on it before 2026-09-01.
    """

    def test_the_calls_are_numbered_from_one(self):
        rows = render_answer(ANSWER).dataframe[0].value

        assert list(rows["#"]) == [1, 2]
        assert list(rows["tool"]) == ["list_pods", "describe_pod"]

    def test_the_arguments_are_the_ones_the_tool_was_called_with(self):
        """
        A timeline that showed the tool but not what it was pointed at cannot
        answer the question it exists for -- which pod was actually read.
        """
        rows = render_answer(ANSWER).dataframe[0].value

        assert rows["arguments"][0] == "namespace=demo"
        assert "memory-hog-x" in rows["arguments"][1]

    def test_a_call_with_no_arguments_is_marked_rather_than_blank(self):
        answer = {**ANSWER,
                  "tool_calls": [{"name": "get_system_info", "arguments": {}}]}
        rows = render_answer(answer).dataframe[0].value

        assert list(rows["arguments"]) == ["—"]


class TestAGroundedAnswerWithNothingToCheck:
    """
    `grounded` means no claim contradicted the evidence, which is also what it
    means when there was no claim to check. Saying so is the difference
    between a verdict and a green badge that means nothing.
    """

    def test_it_says_there_was_nothing_to_verify(self):
        app = render_answer({**ANSWER, "confidence": "grounded", "checked": 0})

        assert any("nothing to verify" in i.value for i in app.info)

    def test_an_answer_that_was_checked_does_not(self):
        """The counter: the note is about the absence of checkable claims,
        not about the verdict."""
        app = render_answer({**ANSWER, "confidence": "grounded", "checked": 3})

        assert not any("nothing to verify" in i.value for i in app.info)

    def test_a_contradicted_answer_does_not_get_it_either(self):
        app = render_answer({**ANSWER, "confidence": "contradicted", "checked": 0,
                             "contradictions": [{"claim": "a", "measured": "b",
                                                 "rule": "r"}]})

        assert not any("nothing to verify" in i.value for i in app.info)


class TestTheHistoryListIsReadableAtAnyQuestionLength:
    """
    The sidebar buttons are the history. A question longer than the button is
    the ordinary case -- people type sentences -- and the label has to stay a
    label rather than taking the sidebar or the page down with it.
    """

    LONG = ("why did the payments-api deployment in staging start failing its "
            "readiness probe after the config change this morning?")

    def test_a_long_question_is_shortened_rather_than_dropped(self):
        seen = []
        app = run_form(seen, question=self.LONG)
        labels = [b.label for b in app.button]

        assert any("…" in label for label in labels), "nothing was truncated"
        # Still the question that was asked, just less of it: the label is a
        # timestamp, two spaces, then a prefix of what was typed.
        shortened = next(label for label in labels if "…" in label)
        question_part = shortened.split("  ", 1)[-1]

        assert question_part.endswith("…")
        assert self.LONG.startswith(question_part[:-1])
        assert len(question_part) < len(self.LONG)

    def test_a_short_question_is_shown_whole(self):
        seen = []
        app = run_form(seen, question="why is it restarting?")
        labels = [b.label for b in app.button]

        assert any(label.endswith("why is it restarting?") for label in labels)


class TestTheAskPanelBeforeAnythingIsAsked:
    """
    The form is on screen from the first render, and everything it says has to
    be about what the reader just did rather than about what they have not
    done yet.
    """

    def test_the_placeholder_warning_waits_for_a_click(self):
        """
        "Type a question" on a page nobody has submitted is an error message
        for something that has not happened. It is shown when Diagnose is
        clicked with an empty box and no workload selected, and only then.
        """
        app = run(FINDINGS)

        assert not any("grey text is a placeholder" in w.value for w in app.warning)

    def test_an_unscoped_question_runs_with_nothing_selected(self):
        """
        No workload selected is a legitimate state -- it is the whole cluster
        -- and the question still has to reach the agent. The scoping branch
        must not be entered without a subject to scope to.
        """
        seen = []
        app = run_form(seen, question="what is wrong with this cluster?",
                       scan={"result": "no unhealthy workloads"})

        assert seen == ["what is wrong with this cluster?"], \
            "the question did not reach the agent unscoped"
        assert not app.exception, [str(e.value) for e in app.exception]


# --- the header strip, chip by chip ------------------------------------------
#
# One line above everything, and the only thing on the page that says which
# cluster the findings below are about and where the evidence goes. The
# existing test asserts the three labels are present; these assert the values,
# which is what a reader acts on.

class FakeGateway:
    """inference.gateway() with a chosen destination."""

    def __init__(self, destination="on-network", provider="ollama",
                 mode="local", model="qwen3"):
        described = {"mode": mode, "provider": provider, "model": model,
                     "destination": destination}
        self.config = SimpleNamespace(describe=lambda: {"primary": described})


def header_of(app):
    return _html(app, "<div class='kw-hdr'>")[0]


class TestTheHeaderSaysWhereEvidenceGoes:
    """
    Where inference happens decides what leaves your network. The chip is the
    one place the console states it, so it has to state it correctly: a
    deployment sending pod logs to a hosted API must not read as on-network.
    """

    def _header(self, gateway, answer=None):
        import inference

        with patch.object(inference, "gateway", lambda: gateway):
            return header_of(render_answer(answer or ANSWER))

    def test_a_local_backend_reads_as_on_network(self):
        header = self._header(FakeGateway(destination="on-network"))

        assert "on-network" in header
        assert "external" not in header

    def test_a_hosted_backend_reads_as_external(self):
        header = self._header(FakeGateway(destination="external",
                                          provider="anthropic", mode="api"))

        assert "external" in header
        # And in the warning tone, which is the half a reader sees first.
        assert "kw-warn" in header

    def test_the_backend_is_named_not_just_classified(self):
        header = self._header(FakeGateway(provider="vllm", mode="cluster",
                                          model="qwen3-32b"))

        assert "vllm" in header and "qwen3-32b" in header

    def test_a_backend_that_cannot_be_described_says_so(self):
        """
        The header never raises: one that took the page down when the model is
        unreachable would hide the one fact worth showing.
        """
        import inference

        def explode():
            raise ConnectionError("no backend")

        with patch.object(inference, "gateway", explode):
            header = header_of(render_answer(ANSWER))

        assert "ConnectionError" in header


class TestTheHealthChip:
    """Rendered only once something has probed, so that "not ready" means a
    failed probe rather than a page that has not asked yet."""

    def test_a_ready_backend(self):
        import streamlit as st

        st.cache_data.clear()
        with patch.object(k8s, "scan_cluster", return_value={"result": "ok"}), \
             patch.object(k8s, "list_nodes", return_value={}):
            app = AppTest.from_file(UI, default_timeout=60)
            app.session_state["health"] = True
            app.run()

        assert "ready" in header_of(app)
        assert "not ready" not in header_of(app)

    def test_a_backend_that_failed_its_probe(self):
        import streamlit as st

        st.cache_data.clear()
        with patch.object(k8s, "scan_cluster", return_value={"result": "ok"}), \
             patch.object(k8s, "list_nodes", return_value={}):
            app = AppTest.from_file(UI, default_timeout=60)
            app.session_state["health"] = False
            app.run()

        assert "not ready" in header_of(app)

    def test_nothing_probed_yet_shows_no_chip(self):
        """The counter: "health" absent is not "health false"."""
        assert "health" not in header_of(render_answer(ANSWER))


class TestTheTimingCaptionIsTheRunsOwnNumbers:
    """
    Seconds, rounds and re-asks under the timeline. Nine of ui.py's survivors
    were in this one f-string -- both divisors, both defaults and all three
    re-ask counts -- because nothing asserted the sentence it produces.
    """

    def test_the_numbers_are_the_ones_the_run_reported(self):
        app = render_answer(ANSWER)
        caption = next(c.value for c in app.caption if "model rounds" in c.value)

        assert caption == ("3 model rounds · model 8.0s · tools 0.4s · "
                           "re-asks: 0 named-tool, 1 evidence, 0 coverage")

    def test_a_run_that_reported_less_reads_as_zero_not_as_missing(self):
        """
        The defaults, which the case above cannot see because it supplies
        every key. A run from an older record has no re-ask counts at all.
        """
        answer = {k: v for k, v in ANSWER.items()
                  if k not in {"nudges", "policies", "coverage"}}
        answer["timing"] = {"rounds": 2}
        app = render_answer(answer)
        caption = next(c.value for c in app.caption if "model rounds" in c.value)

        assert caption == ("2 model rounds · model 0.0s · tools 0.0s · "
                           "re-asks: 0 named-tool, 0 evidence, 0 coverage")


class TestTheEvidencePanelSaysWhenThereIsNone:
    def test_a_run_that_kept_nothing(self):
        app = render_answer({**ANSWER, "evidence": []})

        assert any("evidence was not retained" in c.value for c in app.caption)

    def test_a_run_that_kept_its_evidence_does_not_say_that(self):
        app = render_answer(ANSWER)

        assert not any("evidence was not retained" in c.value for c in app.caption)


class TestTheDisclosureIsAboutARewrite:
    """
    The panel exists because the checkbox defaults to on, so a question gets
    silently rewritten. A run whose prompt is the question was not rewritten,
    and a panel there says a rewrite happened when none did.
    """

    def test_a_prompt_identical_to_the_question_is_not_a_rewrite(self):
        app = render_answer({**ANSWER,
                             "question": "why does it restart?",
                             "prompt": "why does it restart?"})

        assert not any("what was actually sent" in (e.label or "")
                       for e in app.expander)
        assert not any("Scoped to the selected workload" in c.value
                       for c in app.caption)


class TestTheNextStepNeedsEvidenceToReasonFrom:
    """
    `next_step` is agent.evidence_gap() borrowed, and evidence_gap decides what
    is missing by reading tool *results*. A trace with no results behind it
    cannot support a recommendation.
    """

    CRASHING = {"id": "tool-1", "tool": "describe_pod",
                "result": '{"pod": "crasher-x", "namespace": "demo",'
                          '"status": "CrashLoopBackOff",'
                          '"containers": {"c": {"last_termination":'
                          '{"reason": "Error", "exit_code": 1}}}}'}
    CALL = {"name": "describe_pod",
            "arguments": {"name": "crasher-x", "namespace": "demo"}}

    def test_calls_with_no_results_produce_no_recommendation(self):
        app = render_answer({**ANSWER, "question": "why is crasher-x failing?",
                             "tool_calls": [self.CALL], "evidence": []})

        assert not _html(app, "<div class='kw-next'>")

    def test_the_prompt_is_what_names_the_pod(self):
        """
        The typed question often does not name anything -- "why is this
        broken?" -- and the scoped prompt always does. Reading the question
        instead loses the target and with it the recommendation.
        """
        app = render_answer({**ANSWER,
                             "question": "why is this broken?",
                             "prompt": "Answer only about the workload "
                                       "demo/crasher-x (pod: crasher-x). "
                                       "why is this broken?",
                             "tool_calls": [self.CALL],
                             "evidence": [self.CRASHING]})
        blocks = _html(app, "<div class='kw-next'>")

        assert blocks, "no recommendation: the prompt's target was not used"
        assert "crasher-x" in blocks[0]
