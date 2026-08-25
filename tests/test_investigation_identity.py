"""
The target of an investigation, held identical from selection to RCA.

    selected == requested == tool target == evidence target == RCA target

This file exists because that chain broke in the middle, silently, and the
break looked like a model failure. `scoped_question()` writes a prompt; the
loop then re-derived the target by *parsing that prompt back*. The prompt says
"(for example pod nightly-sync-abc)", which is the same name-before-kind shape
as "the crasher deployment", so `targeting.target_of()` returned a workload
called `example` -- and `enforce()` rewrote every tool call to it, including
calls the model had already got right. Every scoped run died on

    {"result": "no workload named example exists in this cluster"}

Deterministic code, so it reproduced identically on gpt-4o-mini and on qwen3,
which is exactly why it read as "the model is being stupid" for a whole day.

Two workloads throughout, from different namespaces with different faults, so
that a stale-state bug cannot pass by coincidence.
"""
import json
import os
import sys
from unittest.mock import patch

import pytest

pytest.importorskip("streamlit", reason="UI extra not installed (requirements-ui.txt)")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streamlit.testing.v1 import AppTest  # noqa: E402

import agent  # noqa: E402
import routers.k8s_pods_info as k8s  # noqa: E402
import targeting  # noqa: E402

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui.py")

A = {"key": "config-faults/missing-configmap-key", "ns": "config-faults",
     "name": "missing-configmap-key", "pod": "missing-configmap-key"}
B = {"key": "demo/crasher", "ns": "demo", "name": "crasher",
     "pod": "crasher-5964d99948-d7qtz"}

SCAN = {
    A["key"]: {"status": "CreateContainerConfigError", "pods": 1,
               "example": A["pod"], "fault": "config"},
    B["key"]: {"status": "CrashLoopBackOff", "pods": 1,
               "example": B["pod"], "fault": "crash"},
}


def answer_for(target, run_id):
    """An answer event shaped like the real one, carrying its own identity."""
    workload = target["name"]
    return {
        "type": "answer", "run_id": run_id, "target": target,
        "answer": f"{workload} is failing.",
        "confidence": "grounded", "checked": 1, "unverified": [],
        "contradictions": [], "rewrites": [], "nudges": 0, "policies": 0,
        "coverage": 0,
        "tool_calls": [{"name": "scan_cluster",
                        "arguments": {"workload": workload}}],
        "evidence": [{"id": "tool-1", "tool": "scan_cluster",
                      "result": json.dumps({"workload": workload,
                                            "namespace": target["namespace"]})}],
        "timing": {"wall_ms": 100, "model_ms": 90, "tool_ms": 10, "rounds": 1},
        "rca": {"observations": [{"claim": workload, "kind": "status",
                                  "evidence": [{"id": "tool-1",
                                                "tool": "scan_cluster",
                                                "field": "workload"}]}],
                "inferences": [], "unknowns": [], "contradictions": [],
                "corrections": []},
    }


class Recorder:
    """Stands in for agent.stream, recording exactly what each run was given."""

    def __init__(self):
        self.runs = []

    def __call__(self, question, *args, target=None, **kwargs):
        run_id = f"run-{len(self.runs) + 1}"
        self.runs.append({"question": question, "target": target,
                          "run_id": run_id})
        yield answer_for(target or {"kind": "workload", "name": "?",
                                    "namespace": "?"}, run_id)

    @property
    def last(self):
        return self.runs[-1]


def app_with(recorder, scan=None):
    import streamlit as st

    st.cache_data.clear()
    return patch.multiple(
        k8s,
        scan_cluster=lambda *a, **k: scan or SCAN,
        list_nodes=lambda *a, **k: {},
        describe_pod=lambda *a, **k: {"pod": "x", "containers": {}},
        get_pod_events=lambda *a, **k: {"pod": "x", "events": []},
        get_pod_logs=lambda *a, **k: {"pod": "x", "source": "c", "logs": "b"},
    ), patch.object(agent, "stream", recorder)


def investigate(app, workload_key):
    """Select a workload and run it, the way a person does."""
    app.selectbox[0].set_value(workload_key).run()
    app.button[0].click().run()
    return app


class TestTheTargetSurvivesTheWholeChain:
    """Requirements 1, 2, 4, 5 -- selection through to tool arguments."""

    def test_selecting_a_workload_investigates_that_workload(self):
        rec = Recorder()
        scan, stream = app_with(rec)
        with scan, stream:
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            investigate(app, A["key"])

        assert rec.last["target"]["name"] == A["name"]
        assert rec.last["target"]["namespace"] == A["ns"]

    def test_selecting_a_second_workload_investigates_the_second(self):
        """
        Requirement 2, and the one a stale-state bug fails. The workloads are
        in different namespaces so that a leaked namespace shows up too.
        """
        rec = Recorder()
        scan, stream = app_with(rec)
        with scan, stream:
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            investigate(app, A["key"])
            investigate(app, B["key"])

        first, second = rec.runs[0], rec.runs[1]
        assert first["target"]["name"] == A["name"]
        assert second["target"]["name"] == B["name"]
        assert second["target"]["namespace"] == B["ns"]
        assert A["name"] not in second["question"]
        assert A["ns"] not in second["question"]

    def test_the_prompt_names_the_selected_workload(self):
        """Requirement 5."""
        rec = Recorder()
        scan, stream = app_with(rec)
        with scan, stream:
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            investigate(app, B["key"])

        assert B["key"] in rec.last["question"]
        assert B["ns"] in rec.last["question"]

    def test_an_explicit_target_is_not_re_derived_from_the_prompt(self):
        """
        Requirement 4, at the seam where it actually broke. The prompt is a
        sentence; the target is data. Parsing the sentence gives the wrong
        answer, so the sentence must not be what decides.
        """
        prompt = agent.scoped_question(
            f"why is {B['key']} failing?", B["key"], B["ns"], B["pod"])
        given = agent.scoped_target(B["key"], B["ns"], B["pod"])

        # Whatever parsing the prompt yields today, enforcement uses the
        # target it was handed. That is the property; the parse is not it.
        targeting.target_of(prompt)

        arguments, _ = targeting.enforce(given, "scan_cluster", {})
        assert arguments["workload"] == B["name"]
        assert given["name"] == B["name"]
        assert given["namespace"] == B["ns"]


class TestTheExampleTrap:
    """
    The defect itself, pinned so it cannot come back through either door:
    the phrase, or the parser.
    """

    def test_the_scoped_prompt_does_not_name_a_workload_called_example(self):
        prompt = agent.scoped_question(
            "why is it failing?", B["key"], B["ns"], B["pod"])
        parsed = targeting.target_of(prompt) or {}

        assert parsed.get("name") != "example"

    def test_example_is_never_taken_as_a_name(self):
        assert (targeting.target_of("for example pod crasher-abc") or {}) \
            .get("name") != "example"

    def test_a_correct_call_is_not_rewritten(self):
        """
        The symptom that made this look like a model failure: enforce() took
        scan_cluster(workload='demo/crasher') -- correct -- and rewrote it.
        """
        target = agent.scoped_target(B["key"], B["ns"], B["pod"])

        arguments, violation = targeting.enforce(
            target, "scan_cluster", {"workload": B["key"]})

        assert arguments["workload"] == B["key"]
        assert violation is None

    def test_a_garbled_name_whose_tail_matches_is_still_corrected(self):
        """
        Observed live, qwen3 on 2026-08-25:
        scan_cluster(workload='config-faults/missing-configfaults/missing-configmap-key').
        Its last path segment is exactly the target, so a comparison that
        strips to the last segment accepts a workload the cluster has never
        heard of.
        """
        target = agent.scoped_target(A["key"], A["ns"], A["pod"])
        garbled = "config-faults/missing-configfaults/missing-configmap-key"

        arguments, violation = targeting.enforce(
            target, "scan_cluster", {"workload": garbled})

        assert arguments["workload"] == A["name"]
        assert violation["action"] == "retargeted"

    def test_a_wrong_call_is_still_corrected(self):
        """The protection this layer exists for must not be lost to the fix."""
        target = agent.scoped_target(B["key"], B["ns"], B["pod"])

        arguments, violation = targeting.enforce(
            target, "scan_cluster", {"workload": "example"})

        assert arguments["workload"] == B["name"]
        assert violation["action"] == "retargeted"


class TestArtifactsBelongToTheirRun:
    """Requirements 3, 6, 7, 8 -- nothing on screen is from a previous run."""

    def test_evidence_from_the_first_run_cannot_appear_in_the_second(self):
        rec = Recorder()
        scan, stream = app_with(rec)
        with scan, stream:
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            investigate(app, A["key"])
            investigate(app, B["key"])
            shown = app.session_state["answer"]

        blob = json.dumps(shown["evidence"])
        assert B["name"] in blob
        assert A["name"] not in blob

    def test_every_artifact_carries_the_same_run_id(self):
        rec = Recorder()
        scan, stream = app_with(rec)
        with scan, stream:
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            investigate(app, A["key"])
            investigate(app, B["key"])
            shown = app.session_state["answer"]

        assert shown["run_id"] == rec.runs[-1]["run_id"]
        assert shown["run_id"] != rec.runs[0]["run_id"]

    def test_the_timeline_shows_the_arguments_that_were_executed(self):
        rec = Recorder()
        scan, stream = app_with(rec)
        with scan, stream:
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            investigate(app, B["key"])
            shown = app.session_state["answer"]

        assert shown["tool_calls"][0]["arguments"]["workload"] == B["name"]

    def test_the_rca_is_about_the_run_that_produced_it(self):
        rec = Recorder()
        scan, stream = app_with(rec)
        with scan, stream:
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            investigate(app, A["key"])
            investigate(app, B["key"])
            shown = app.session_state["answer"]

        claims = json.dumps(shown["rca"])
        assert B["name"] in claims
        assert A["name"] not in claims

    def test_the_acceptance_criterion_holds_end_to_end(self):
        """
        selected == requested == tool target == evidence target == RCA target,
        asserted as one identity rather than four separate near-misses.
        """
        rec = Recorder()
        scan, stream = app_with(rec)
        with scan, stream:
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            investigate(app, B["key"])
            shown = app.session_state["answer"]

        selected = app.session_state["subject"]["workload"]
        requested = rec.last["target"]["name"]
        tool = shown["tool_calls"][0]["arguments"]["workload"]
        evidence = json.loads(shown["evidence"][0]["result"])["workload"]
        rca = shown["rca"]["observations"][0]["claim"]

        assert selected.split("/")[-1] == requested == tool == evidence == rca


class TestTheClusterCannotMoveTheTarget:
    """Requirements 9 and 10 -- a background read must not retarget the run."""

    def test_a_rescan_that_reorders_workloads_keeps_the_selection(self):
        """
        The scan is rebuilt on a TTL and this cluster's CronJob workloads come
        and go between reads, so the option list genuinely changes underneath.
        An unkeyed selectbox is positional, and index 0 became a different
        workload without anyone touching the page.
        """
        rec = Recorder()
        import streamlit as st

        st.cache_data.clear()
        # A workload that sorts ahead of both, appearing between reads -- which
        # is what a CronJob does. It takes index 0 away from the selection.
        later = {"aaa-new/appeared": {"status": "Error", "pods": 1,
                                      "example": "appeared-1", "fault": "crash"},
                 **SCAN}
        state = {"scan": SCAN}
        with patch.multiple(
            k8s,
            scan_cluster=lambda *a, **k: state["scan"],
            list_nodes=lambda *a, **k: {},
            describe_pod=lambda *a, **k: {"pod": "x", "containers": {}},
            get_pod_events=lambda *a, **k: {"pod": "x", "events": []},
            get_pod_logs=lambda *a, **k: {"pod": "x", "source": "c", "logs": "b"},
        ), patch.object(agent, "stream", rec):
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            app.selectbox[0].set_value(B["key"]).run()
            assert app.session_state["subject"]["workload"] == B["key"]

            state["scan"] = later
            st.cache_data.clear()
            app.run()

            assert app.selectbox[0].options[0] == "aaa-new/appeared", \
                "the option list did not actually change -- test proves nothing"
            assert app.session_state["subject"]["workload"] == B["key"], \
                "a background re-scan moved the investigation target"

    def test_a_workload_leaving_the_scan_does_not_move_the_target(self):
        """
        The other half of requirement 9, and the half the first fix missed.
        `only_unhealthy` hides a workload the moment it recovers, and a
        CronJob's workload leaves the scan every time its pods complete -- so
        this fires on its own, repeatedly, with nobody touching the page.

        Measured before the fix: demo/nightly-sync -> demo/bad-image, silently.
        The next Diagnose would then have investigated a workload nobody chose,
        which is the reported bug arriving without the stray click that was
        blamed for it.
        """
        rec = Recorder()
        import streamlit as st

        gone = {k: v for k, v in SCAN.items() if k != B["key"]}
        state = {"scan": SCAN}
        st.cache_data.clear()
        with patch.multiple(
            k8s,
            scan_cluster=lambda *a, **k: state["scan"],
            list_nodes=lambda *a, **k: {},
            describe_pod=lambda *a, **k: {"pod": "x", "containers": {}},
            get_pod_events=lambda *a, **k: {"pod": "x", "events": []},
            get_pod_logs=lambda *a, **k: {"pod": "x", "source": "c", "logs": "b"},
        ), patch.object(agent, "stream", rec):
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            app.selectbox[0].set_value(B["key"]).run()
            assert app.session_state["subject"]["workload"] == B["key"]

            state["scan"] = gone
            st.cache_data.clear()
            app.run()

            assert app.session_state["subject"]["workload"] == B["key"], \
                "the target moved when its workload left the scan"
            assert any(B["key"] in str(w.value) for w in app.warning), \
                "the target was kept but the user was never told why"

            # and a run started now still investigates what is on screen
            app.button[0].click().run()

        assert rec.last["target"]["name"] == B["name"]

    def test_narrowing_the_namespace_filter_does_not_retarget(self):
        """
        The sidebar's namespace filter changes what the scan returns, which
        changes the option list -- the same mechanism as a TTL re-read, reached
        by a different door. Filtering to one namespace must not silently move
        an investigation that was aimed at another.
        """
        rec = Recorder()
        import streamlit as st

        st.cache_data.clear()
        # scan_cluster is called with the chosen namespaces; honour them, so the
        # option list genuinely shrinks the way it does live.
        def scan(*args, **kwargs):
            # ui.py calls scan_cluster(only_unhealthy, limit, namespaces,
            # workload) POSITIONALLY. Reading a namespaces= kwarg here returned
            # None every time, the option list never narrowed, and the
            # assertion below passed for the wrong reason until the setup was
            # made to prove itself.
            ns = kwargs.get("namespaces")
            if ns is None and len(args) >= 3:
                ns = args[2]
            ns = ns or ""
            if not ns:
                return SCAN
            wanted = {n.strip() for n in str(ns).split(",") if n.strip()}
            return {k: v for k, v in SCAN.items()
                    if k.split("/")[0] in wanted}

        with patch.multiple(
            k8s,
            scan_cluster=scan,
            list_nodes=lambda *a, **k: {},
            list_namespaces=lambda *a, **k: [A["ns"], B["ns"]],
            describe_pod=lambda *a, **k: {"pod": "x", "containers": {}},
            get_pod_events=lambda *a, **k: {"pod": "x", "events": []},
            get_pod_logs=lambda *a, **k: {"pod": "x", "source": "c", "logs": "b"},
        ), patch.object(agent, "stream", rec):
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            app.selectbox[0].set_value(B["key"]).run()
            assert app.session_state["subject"]["workload"] == B["key"]

            # Narrow to the OTHER namespace, which removes B from the scan.
            app.multiselect[0].set_value([A["ns"]]).run()

            # Prove the setup did what it claims before asserting on it: if the
            # filter never reached scan_cluster the option list is unchanged and
            # everything below passes for the wrong reason.
            assert any(B["key"] in str(w.value) for w in app.warning), \
                "the namespace filter did not actually narrow the scan"
            assert app.session_state["subject"]["workload"] == B["key"], \
                "narrowing the namespace filter moved the investigation target"

    def test_a_reload_loses_the_panel_but_not_the_history(self):
        """
        session_state dies with the browser tab; the store does not. After a
        refresh the answer is gone from the page and recoverable from the
        sidebar -- which is what _history() exists for and what nothing had
        exercised.
        """
        rec = Recorder()
        scan, stream = app_with(rec)
        with scan, stream:
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            investigate(app, B["key"])
            assert "answer" in app.session_state

            # A reload is a fresh session against the same server process.
            fresh = AppTest.from_file(UI, default_timeout=60)
            fresh.run()

            assert "answer" not in fresh.session_state or \
                not fresh.session_state["answer"], \
                "an answer survived into a new session"
            labels = [b.label for b in fresh.button]
            assert any(B["name"] in str(label) for label in labels), \
                "the finished investigation was not recoverable after a reload"

    def test_a_cached_scan_cannot_overwrite_the_chosen_target(self):
        rec = Recorder()
        scan, stream = app_with(rec)
        with scan, stream:
            app = AppTest.from_file(UI, default_timeout=60)
            app.run()
            app.selectbox[0].set_value(B["key"]).run()
            app.run()          # a rerun serving the cached scan
            investigate(app, B["key"])

        assert rec.last["target"]["name"] == B["name"]


class TestTheLoopMintsTheIdentity:
    """
    The UI tests above stub agent.stream, so they prove the console carries a
    run_id through -- not that the loop emits one. This asserts the source.
    """

    def test_every_event_carries_a_run_id_and_the_answer_carries_the_target(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        reply = SimpleNamespace(message=SimpleNamespace(
            content="crasher is failing.", tool_calls=None))
        client = MagicMock()
        client.chat = MagicMock(return_value=reply)
        target = agent.scoped_target(B["key"], B["ns"], B["pod"])

        with patch("backends.ollama.Client", return_value=client):
            agent._BACKEND = None
            events = list(agent.stream("why is it failing?", target=target))

        assert events, "the loop produced no events"
        assert all(e.get("run_id") for e in events), \
            "an event reached a consumer with no run identity"
        assert len({e["run_id"] for e in events}) == 1, \
            "one investigation emitted more than one identity"

        answer = events[-1]
        assert answer["type"] == "answer"
        assert answer["target"] == target
