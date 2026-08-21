"""
Tests for the tool-calling loop.

No model is involved: the ollama client is mocked, so these assert the
mechanics -- that tools are dispatched, results fed back, failures contained,
and runaway loops stopped.
"""

import contextlib
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent
import grounding


# A cluster with nothing wrong in it. Used wherever a loop test calls a pod
# tool it does not care about the result of -- otherwise the test reads
# whatever cluster is running on the machine, and the evidence policy reacts
# to it.
HEALTHY_STUB = {
    "list_pods": lambda **k: {"web-abc-xyz": {"status": "Running", "ready": "1/1"}},
    "describe_pod": lambda **k: {"pod": "web-abc-xyz", "namespace": "demo",
                                 "status": "Running"},
}


def tool_call(name, arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def reply(content=None, calls=None):
    return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))


@contextlib.contextmanager
def mock_chat(**kwargs):
    """
    Patch the ollama client and yield its .chat mock.

    The agent builds a Client per call so it can set a timeout, so patching
    ollama.chat directly would silently miss and hit a real model -- which is
    exactly the failure this indirection prevents.
    """
    client = MagicMock()
    client.chat = MagicMock(**kwargs)
    with patch("agent.ollama.Client", return_value=client):
        yield client.chat


class TestToolRegistry:
    def test_every_registered_tool_is_callable(self):
        assert all(callable(f) for f in agent.TOOLS.values())

    def test_registry_keys_match_function_names(self):
        """The model dispatches by name; a mismatch silently breaks a tool."""
        for name, func in agent.TOOLS.items():
            assert func.__name__ == name

    def test_every_tool_has_a_docstring(self):
        # Docstrings are the tool descriptions the model reads. A missing one
        # leaves the model guessing when to call it.
        for name, func in agent.TOOLS.items():
            assert func.__doc__ and func.__doc__.strip(), f"{name} has no docstring"


class TestRunTool:
    def test_dispatches_and_serialises(self):
        out = agent._run_tool("get_system_info", {})
        assert set(json.loads(out)) == {"cpu", "memory", "disk", "user"}

    def test_unknown_tool_returns_error_not_raise(self):
        assert "no such tool" in json.loads(agent._run_tool("nope", {}))["error"]

    def test_tool_exception_is_captured(self):
        # A failing tool must not kill the loop; the model can often recover
        # by trying a different one.
        with patch.dict(agent.TOOLS, {"boom": lambda: 1 / 0}):
            result = json.loads(agent._run_tool("boom", {}))

        assert "ZeroDivisionError" in result["error"]

    def test_bad_arguments_are_captured(self):
        result = json.loads(agent._run_tool("get_system_info", {"bogus": 1}))
        assert "error" in result

    def test_non_serialisable_values_do_not_raise(self):
        import datetime as dt

        with patch.dict(agent.TOOLS, {"t": lambda: {"when": dt.datetime.now()}}):
            assert "when" in json.loads(agent._run_tool("t", {}))


class TestAskLoop:
    def test_answers_without_tools(self):
        with mock_chat(return_value=reply(content="all fine")):
            result = agent.ask("how are things?")

        assert result["answer"] == "all fine"
        assert result["tool_calls"] == []

    def test_executes_tool_then_answers(self):
        responses = [
            reply(calls=[tool_call("get_system_info", {})]),
            reply(content="cpu is low"),
        ]
        with mock_chat(side_effect=responses):
            result = agent.ask("is the cpu busy?")

        assert result["answer"] == "cpu is low"
        assert result["tool_calls"] == [{"name": "get_system_info", "arguments": {}}]

    def test_tool_results_are_fed_back(self):
        responses = [
            reply(calls=[tool_call("get_system_info", {})]),
            reply(content="done"),
        ]
        with mock_chat(side_effect=responses) as chat:
            agent.ask("q")

        sent = chat.call_args.kwargs["messages"]
        assert any(m.get("role") == "tool" for m in sent if isinstance(m, dict))

    def test_multi_hop_chain(self):
        responses = [
            reply(calls=[tool_call("list_pods", {"namespace": "demo"})]),
            reply(calls=[tool_call("describe_pod", {"name": "p", "namespace": "demo"})]),
            reply(content="OOMKilled"),
        ]
        # Stubbed, because the real tools reach a cluster if one happens to be
        # running on the machine. These passed either way until the evidence
        # policy started reading tool RESULTS: with a live kind cluster they
        # then found a crashing pod, fired, and asked for a round this list of
        # mocked replies does not have. A loop test must not depend on whether
        # a cluster exists.
        with patch.dict(agent.TOOLS, HEALTHY_STUB), mock_chat(side_effect=responses):
            result = agent.ask("what is broken?")

        assert [c["name"] for c in result["tool_calls"]] == ["list_pods", "describe_pod"]

    def test_runaway_loop_is_stopped(self):
        """A model that never stops calling tools must not hang forever."""
        forever = reply(calls=[tool_call("get_system_info", {})])
        with mock_chat(return_value=forever) as chat:
            result = agent.ask("q")

        assert "Gave up" in result["answer"]
        assert chat.call_count == agent.MAX_ROUNDS

    def test_system_prompt_is_sent_first(self):
        with mock_chat(return_value=reply(content="x")) as chat:
            agent.ask("q")

        assert chat.call_args.kwargs["messages"][0]["role"] == "system"

    def test_answer_carries_a_confidence_verdict(self):
        responses = [
            reply(calls=[tool_call("get_system_info", {})]),
            reply(content="cpu is fine"),
        ]
        with mock_chat(side_effect=responses):
            result = agent.ask("q")

        assert result["confidence"] in {
            "grounded", "partial", "ungrounded", grounding.INSUFFICIENT
        }

    def test_invented_figure_is_flagged(self):
        """
        End to end: a claim no tool produced comes back as unverified.

        The tool is stubbed rather than real. Calling the real one made this
        test depend on the wall clock: on a runner at 18:12 UTC the boot
        timestamp contains "18", which silently grounded the fabricated
        "18 days" and turned a genuine assertion into a coin flip.
        """
        responses = [
            reply(calls=[tool_call("get_platform_info", {})]),
            reply(content="This host has been up for 18 days."),
        ]
        stub = {"get_platform_info": lambda: {"Uptime": "4:36:25"}}

        with patch.dict(agent.TOOLS, stub), mock_chat(side_effect=responses):
            result = agent.ask("how long has it been up?")

        assert result["confidence"] == "partial"
        assert "18" in result["unverified"]

    def test_answer_with_no_tools_is_ungrounded(self):
        with mock_chat(return_value=reply(content="CPU is 42%.")):
            result = agent.ask("q")

        assert result["confidence"] == "ungrounded"

    def test_falls_back_when_model_has_no_thinking_mode(self):
        """llama3.2 rejects think=True with a 400; that must not be fatal."""
        import ollama as ollama_mod

        error = ollama_mod.ResponseError("llama3.2 does not support thinking")
        with mock_chat(side_effect=[error, reply(content="ok")]) as chat:
            result = agent.ask("q", model="llama3.2")

        assert result["answer"] == "ok"
        assert chat.call_args.kwargs["think"] is False

    def test_other_response_errors_still_raise(self):
        import ollama as ollama_mod

        with mock_chat(side_effect=ollama_mod.ResponseError("model not found")):
            with pytest.raises(ollama_mod.ResponseError):
                agent.ask("q")

    def test_thinking_enabled_by_default(self):
        # Without it qwen3 answers multi-part questions from the first tool
        # only and invents the rest.
        with mock_chat(return_value=reply(content="x")) as chat:
            agent.ask("q")

        assert chat.call_args.kwargs["think"] is True


class TestKeepAlive:
    """
    OLLAMA_KEEP_ALIVE has to reach the request, because nothing else reads it.

    It is conventionally a server-side variable and the ollama client library
    ignores it entirely, so the eval command documented in CONTRIBUTING was
    exporting it into a process where no code path looked. Measured against a
    live server: unload the model, run one chat with the variable exported,
    and /api/ps reports the model expiring in five minutes rather than 24
    hours. These assert the wiring that makes that command mean something.
    """

    def test_forwards_the_configured_keep_alive(self):
        with patch.object(agent, "KEEP_ALIVE", "24h"):
            with mock_chat(return_value=reply(content="x")) as chat:
                agent.ask("q")

        assert chat.call_args.kwargs["keep_alive"] == "24h"

    def test_unset_sends_none_so_the_server_default_still_applies(self):
        """None is dropped from the body, so this is not a behaviour change."""
        with patch.object(agent, "KEEP_ALIVE", None):
            with mock_chat(return_value=reply(content="x")) as chat:
                agent.ask("q")

        assert chat.call_args.kwargs["keep_alive"] is None

    def test_survives_the_no_thinking_fallback(self):
        """The retry builds a fresh call, which is where a setting gets lost."""
        import ollama as ollama_mod

        error = ollama_mod.ResponseError("llama3.2 does not support thinking")
        with patch.object(agent, "KEEP_ALIVE", "24h"):
            with mock_chat(side_effect=[error, reply(content="ok")]) as chat:
                agent.ask("q", model="llama3.2")

        assert chat.call_args.kwargs["keep_alive"] == "24h"
        assert chat.call_args.kwargs["think"] is False


class TestStream:
    """
    The event stream ask() is built on.

    These matter because two callers now depend on the ordering: the UI renders
    a step the moment it starts, and any streaming endpoint replays these
    verbatim. A tool_call arriving only after its result would defeat both.
    """

    def test_emits_call_before_result(self):
        responses = [
            reply(calls=[tool_call("get_system_info", {})]),
            reply(content="cpu is low"),
        ]
        with mock_chat(side_effect=responses):
            events = list(agent.stream("q"))

        kinds = [event["type"] for event in events]
        assert kinds == ["tool_call", "tool_result", "answer"]

    def test_answer_is_last_and_unique(self):
        responses = [
            reply(calls=[tool_call("list_pods", {"namespace": "demo"})]),
            reply(calls=[tool_call("get_system_info", {})]),
            reply(content="done"),
        ]
        with patch.dict(agent.TOOLS, HEALTHY_STUB), mock_chat(side_effect=responses):
            events = list(agent.stream("q"))

        assert [event["type"] for event in events].count("answer") == 1
        assert events[-1]["type"] == "answer"

    def test_answer_event_carries_the_full_result(self):
        with mock_chat(return_value=reply(content="all fine")):
            events = list(agent.stream("q"))

        answer = events[-1]
        assert set(answer) >= {"answer", "tool_calls", "confidence", "unverified"}

    def test_result_is_the_serialised_tool_output(self):
        responses = [
            reply(calls=[tool_call("get_system_info", {})]),
            reply(content="ok"),
        ]
        with mock_chat(side_effect=responses):
            events = list(agent.stream("q"))

        result = next(e for e in events if e["type"] == "tool_result")
        # JSON, not a repr: the UI and a streaming endpoint both parse this.
        assert isinstance(json.loads(result["result"]), dict)
        assert result["duration_ms"] >= 0

    def test_failing_tool_still_streams_a_result(self):
        """The loop survives a failing tool, so the stream must show it."""
        responses = [
            reply(calls=[tool_call("no_such_tool", {})]),
            reply(content="recovered"),
        ]
        with mock_chat(side_effect=responses):
            events = list(agent.stream("q"))

        result = next(e for e in events if e["type"] == "tool_result")
        assert "error" in json.loads(result["result"])
        assert events[-1]["answer"] == "recovered"

    def test_runaway_loop_still_terminates_with_an_answer(self):
        with mock_chat(return_value=reply(calls=[tool_call("get_system_info", {})])):
            events = list(agent.stream("q"))

        assert events[-1]["type"] == "answer"
        assert "Gave up" in events[-1]["answer"]

    @staticmethod
    def _drain(func):
        with mock_chat(
            side_effect=[
                reply(calls=[tool_call("get_system_info", {})]),
                reply(content="cpu is low"),
            ]
        ):
            return func()

    def test_ask_matches_the_streams_answer(self):
        """ask() is stream() drained; if these drift, one of them is a lie.

        Asked with evidence=True, because that is the call that returns the
        whole answer event -- the default drops one field deliberately, and
        the test below pins that.
        """
        streamed = self._drain(lambda: list(agent.stream("q"))[-1])
        asked = self._drain(lambda: agent.ask("q", evidence=True))

        assert "type" not in asked
        # Same fields, both ways. This is the half that catches drift.
        assert set(asked) == set(streamed) - {"type"}

        # Everything except the measured fields must be equal. Timing is
        # measured, so two runs of the same mocked chain legitimately differ
        # by a few microseconds. So is evidence: mock_chat replaces the model,
        # not the tools, and get_system_info really does read this host's CPU,
        # which moves between the two drains. Comparing either by value would
        # make this test flaky for the fields that are supposed to vary, and
        # the set comparison above is what actually catches drift. Evidence
        # is pinned by value in TestEvidenceIsReturnedOnRequest.
        ignore = {"type", "timing", "evidence"}
        assert {k: v for k, v in asked.items() if k not in ignore} == {
            k: v for k, v in streamed.items() if k not in ignore
        }
        assert set(asked["timing"]) == set(streamed["timing"])


class TestEvidenceIsReturnedOnRequest:
    """
    The tool results the answer was checked against, kept so a recorded run
    can be re-scored when the checker changes rather than re-run against a
    cluster that has moved on.

    Opt-in: every other caller of ask() puts its return on a wire.
    """

    def test_ask_does_not_carry_evidence_by_default(self):
        asked = TestStream._drain(lambda: agent.ask("q"))

        assert "evidence" not in asked

    def test_ask_carries_it_when_asked(self):
        asked = TestStream._drain(lambda: agent.ask("q", evidence=True))

        assert [e["tool"] for e in asked["evidence"]] == ["get_system_info"]
        assert all(set(e) == {"id", "tool", "result"} for e in asked["evidence"])

    def test_the_ids_are_the_ones_grounding_was_given(self):
        """The record is only a faithful replay if the citations still resolve,
        so the ids must be records() shape and in call order."""
        asked = TestStream._drain(lambda: agent.ask("q", evidence=True))

        assert [e["id"] for e in asked["evidence"]] == ["tool-1"]

    def test_the_result_is_the_string_the_tool_returned(self):
        asked = TestStream._drain(lambda: agent.ask("q", evidence=True))

        assert isinstance(asked["evidence"][0]["result"], str)

    def test_a_recorded_run_re_scores_to_the_verdict_it_was_given(self):
        """The whole point. check(answer, evidence) offline must reproduce the
        confidence the live run recorded -- otherwise the field is decoration
        and a re-scoring built on it would be measuring its own gaps."""
        asked = TestStream._drain(lambda: agent.ask("q", evidence=True))

        replayed = grounding.check(asked["answer"], asked["evidence"])

        assert replayed["confidence"] == asked["confidence"]
        assert replayed["unverified"] == asked["unverified"]


class TestPrefetchedEvidence:
    """
    Evidence gathered before the loop starts, for a subject that may be gone.

    The controller reads a pod's logs at enqueue time because a CronJob pod
    lives about two minutes and a diagnosis takes longer. By the time the model
    asks, every tool returns 404 -- so the only record that will ever exist has
    to be carried in.
    """

    def test_it_reaches_the_model(self):
        item = {"name": "get_pod_logs", "arguments": {"name": "p"},
                "result": '{"logs": "FATAL: upstream returned 503"}'}

        with mock_chat(return_value=reply(content="x")) as chat:
            agent.ask("why did it fail?", prefetched=[item])

        sent = chat.call_args.kwargs["messages"][1]["content"]
        assert "FATAL: upstream returned 503" in sent
        assert "why did it fail?" in sent

    def test_it_counts_as_a_measurement_for_grounding(self):
        """
        Otherwise the one surviving piece of evidence is the one thing an
        answer gets marked unverified for quoting. It IS a measurement -- a
        tool produced it against the live cluster.
        """
        item = {"name": "get_pod_logs", "arguments": {"name": "p"},
                "result": '{"logs": "exited with code 137"}'}

        with mock_chat(return_value=reply(content="It exited with code 137.")):
            result = agent.ask("why?", prefetched=[item])

        assert result["confidence"] == "grounded"
        assert result["unverified"] == []

    def test_it_appears_in_the_trace_marked_as_prefetched(self):
        """A reader has to be able to tell this from what the model fetched."""
        item = {"name": "get_pod_logs", "arguments": {"name": "p"}, "result": "{}"}

        with mock_chat(return_value=reply(content="x")):
            result = agent.ask("why?", prefetched=[item])

        assert result["tool_calls"][0]["name"] == "get_pod_logs"
        assert result["tool_calls"][0]["prefetched"] is True

    def test_no_prefetched_evidence_changes_nothing(self):
        with mock_chat(return_value=reply(content="x")) as chat:
            agent.ask("plain question")

        assert chat.call_args.kwargs["messages"][1]["content"] == "plain question"


class TestSuspendedHost:
    """
    Telling "the model hung" apart from "the laptop went to sleep".

    Measured 2026-08-17: a 725s run against a 62s median, with the model
    accounting for 180s of it. `pmset -g log` put the machine asleep for 548s
    inside that window against 545s unaccounted. Every other timer in the loop
    is monotonic and a monotonic clock does not advance while a host is
    suspended, so the two clocks disagreeing by exactly the nap is the signal.
    """

    def test_a_suspended_host_is_reported_not_hidden(self):
        # A wall clock that has advanced further than the monotonic one is
        # only possible if the host was suspended in between.
        wall = iter([1_000.0, 1_600.0])
        mono = iter([0.0, 0.0, 10.0, 10.0])
        with mock_chat(return_value=reply(content="x")):
            with patch("agent.time.time", lambda: next(wall)), \
                 patch("agent.time.perf_counter", lambda: next(mono)):
                result = agent.ask("q")

        assert result["timing"]["slept_ms"] == pytest.approx(590_000, rel=0.01)
        assert result["timing"]["wall_ms"] == pytest.approx(600_000, rel=0.01)

    def test_an_uninterrupted_run_reports_no_sleep(self):
        with mock_chat(return_value=reply(content="x")):
            result = agent.ask("q")

        assert result["timing"]["slept_ms"] == 0.0
        # Everything the loop did not spend in the model or in a tool.
        assert result["timing"]["unaccounted_ms"] < 1_000

    def test_the_clocks_are_not_allowed_to_report_negative_sleep(self):
        """Clock resolution must not produce a negative nap."""
        wall = iter([1_000.0, 1_000.0])
        mono = iter([0.0, 0.0, 0.5, 0.5])
        with mock_chat(return_value=reply(content="x")):
            with patch("agent.time.time", lambda: next(wall)), \
                 patch("agent.time.perf_counter", lambda: next(mono)):
                result = agent.ask("q")

        assert result["timing"]["slept_ms"] == 0.0


class TestNamedButNotCalled:
    """
    The plan-instead-of-a-diagnosis failure, at the loop level.

    Measured on crashloop_root_cause, n=10, 2026-08-17: two runs read
    describe_pod, saw `exit_code: 1`, and ended with "Next Step: check the
    container logs (get_pod_logs)" -- naming the one tool holding the cause
    rather than calling it. The user of a diagnosis tool cannot act on a
    plan; that is the whole product.
    """

    def test_detects_a_tool_named_but_never_called(self):
        skipped = agent.named_but_not_called(
            "Next step: run get_pod_logs to see the error.", {"describe_pod"}
        )
        assert skipped == ["get_pod_logs"]

    def test_a_tool_already_called_is_not_flagged(self):
        """Citing what you did is not a plan -- it is the evidence trail."""
        skipped = agent.named_but_not_called(
            "get_pod_logs showed a connection refused to db:5432.",
            {"list_pods", "get_pod_logs"},
        )
        assert skipped == []

    def test_prose_about_logs_is_not_enough(self):
        """
        "Check the logs" names no call. Firing on it would mean guessing
        which tool was meant, on answers that are frequently complete.
        """
        assert agent.named_but_not_called("Check the container logs.", set()) == []

    def test_the_run_is_sent_back_and_the_tool_gets_called(self):
        responses = [
            reply(calls=[tool_call("describe_pod", {"name": "p"})]),
            reply(content="Exit code 1. Next step: get_pod_logs."),
            reply(calls=[tool_call("get_pod_logs", {"name": "p"})]),
            reply(content="Connection refused to db:5432."),
        ]
        # The log really carries the port, as it does on the demo cluster.
        # Without it the verify stage correctly marks 5432 as unmeasured and
        # this test stops being about the nudge at all.
        stub = {"get_pod_logs": lambda **k: {
            "pod": "p", "logs": "FATAL: could not connect to db:5432: connection refused"}}
        with patch.dict(agent.TOOLS, stub), mock_chat(side_effect=responses):
            result = agent.ask("why is it crashing?")

        assert [c["name"] for c in result["tool_calls"]] == [
            "describe_pod", "get_pod_logs",
        ]
        # Unchanged and unannotated: the port was measured, so nothing is
        # rewritten and no audit is appended.
        assert result["answer"] == "Connection refused to db:5432."

    def test_the_nudge_names_the_tool_and_prescribes_nothing_else(self):
        """
        It must not hint at where the cause is. That would be this file
        guessing the diagnosis, and every answer after it would be suspect.
        """
        responses = [
            reply(content="Next step: get_pod_logs."),
            reply(content="done"),
        ]
        with mock_chat(side_effect=responses) as chat:
            agent.ask("why is the crasher pod failing?")

        sent = chat.call_args.kwargs["messages"]
        nudge = [m for m in sent if isinstance(m, dict) and m["role"] == "user"][-1]
        assert "get_pod_logs" in nudge["content"]

        # The question is quoted back verbatim, so only what this file adds
        # around it is held to the no-hints rule.
        added = (
            nudge["content"]
            .replace("why is the crasher pod failing?", "")
            .replace("get_pod_logs", "")
        )
        for leading in ("log", "pod", "crash", "database", "connection"):
            assert leading not in added

    def test_the_nudge_puts_the_question_back(self):
        """
        Without it the last thing in context is one pod's detail and an
        order to call a tool, and the model answers that instead. Measured
        on cluster_wide_scan: 3/3 before the guard, 2/4 after, both failures
        answering about the single pod they had just described.
        """
        responses = [
            reply(calls=[tool_call("scan_cluster", {})]),
            reply(content="memory-hog, crasher and bad-image are broken. "
                          "Run describe_pod for detail."),
            reply(content="still all three"),
        ]
        # Stubbed for the same reason as the loop tests: unstubbed,
        # scan_cluster reads whatever cluster is running and the evidence
        # policy then asks for a round these mocked replies do not have.
        stub = {"scan_cluster": lambda **k: {
            "demo/memory-hog": {"status": "OOMKilled", "pods": 1,
                                "example": "memory-hog-abc"}}}
        with patch.dict(agent.TOOLS, stub), mock_chat(side_effect=responses) as chat:
            agent.ask("Is anything broken anywhere in the cluster?")

        sent = chat.call_args.kwargs["messages"]
        nudge = [m for m in sent if isinstance(m, dict) and m["role"] == "user"][-1]
        assert "Is anything broken anywhere in the cluster?" in nudge["content"]
        assert "Keep every finding you have already reported" in nudge["content"]

    def test_it_gives_up_after_one_nudge(self):
        """A model that has decided it is finished is not argued with."""
        stubborn = reply(content="You should run get_pod_logs.")
        with mock_chat(return_value=stubborn) as chat:
            result = agent.ask("q")

        assert chat.call_count == 2
        assert result["answer"] == "You should run get_pod_logs."

    def test_no_nudge_without_rounds_left_to_use_it(self):
        """
        Nudging on the last round can only turn a usable answer into
        "gave up": there is no round left to call the tool in.
        """
        calling = reply(calls=[tool_call("describe_pod", {"name": "p"})])
        planning = reply(content="Next step: get_pod_logs.")
        responses = [calling] * (agent.MAX_ROUNDS - 1) + [planning]
        with mock_chat(side_effect=responses):
            result = agent.ask("q")

        assert result["answer"] == "Next step: get_pod_logs."

    def test_the_count_is_reported_so_the_guard_can_be_measured(self):
        """
        A run that called get_pod_logs after being sent back looks identical
        to one that called it unprompted, unless this is recorded.
        """
        responses = [
            reply(content="Next step: get_pod_logs."),
            reply(calls=[tool_call("get_pod_logs", {"name": "p"})]),
            reply(content="db:5432 refused the connection."),
        ]
        with mock_chat(side_effect=responses):
            nudged = agent.ask("q")

        with mock_chat(side_effect=[reply(content="It is healthy.")]):
            untouched = agent.ask("q")

        assert nudged["nudges"] == 1
        assert untouched["nudges"] == 0

    def test_a_complete_answer_is_not_sent_back(self):
        with mock_chat(side_effect=[reply(content="It is healthy.")]) as chat:
            result = agent.ask("is it ok?")

        assert chat.call_count == 1
        assert result["answer"] == "It is healthy."


class TestCapturePodLogs:
    """
    The shared half of the CronJob race fix.

    Both the controller's watch and the CLI's --explain know which pod they
    are about to ask about, and both are slower than the pod: measured on
    --explain against a real CronJob, get_pod_logs was followed immediately by
    list_pods -- the model going to look elsewhere -- and the answer reached no
    root cause.
    """

    def test_returns_the_shape_ask_expects(self):
        with patch.object(agent, "get_pod_logs", return_value={"logs": "boom"}):
            captured = agent.capture_pod_logs("p", "demo")

        assert captured[0]["name"] == "get_pod_logs"
        assert captured[0]["arguments"] == {"name": "p", "namespace": "demo", "tail": 50}
        assert "boom" in captured[0]["result"]
        assert captured[0]["captured_at"]

    def test_an_error_is_not_captured_as_evidence(self):
        with patch.object(agent, "get_pod_logs", return_value={"error": "404 not found"}):
            assert agent.capture_pod_logs("p", "demo") == []

    def test_a_no_logs_explanation_is_not_captured_as_evidence(self):
        """
        "its container has never started" is a helpful sentence and not a log.
        Handing it over as a measurement would have the model quote it as one.
        """
        with patch.object(agent, "get_pod_logs",
                          return_value={"result": "no logs for pod p: never started"}):
            assert agent.capture_pod_logs("p", "demo") == []

    def test_a_raising_tool_is_not_fatal(self):
        with patch.object(agent, "get_pod_logs", side_effect=RuntimeError("boom")):
            assert agent.capture_pod_logs("p", "demo") == []


class TestTimingAttribution:
    """
    Where a run's wall clock went, split model against tools.

    The eval's run-level timer could only say *that* a run took 2217s against
    a 62s median. Two hypotheses died on that ambiguity -- weights unloading
    and machine contention -- because nothing recorded whether the time was
    spent inside Ollama or inside a tool call.
    """

    def test_a_slow_model_round_is_attributed_to_the_model(self):
        def slow_chat(*args, **kwargs):
            time.sleep(0.20)
            return reply(content="done")

        with mock_chat(side_effect=slow_chat):
            result = agent.ask("q")

        t = result["timing"]
        assert t["rounds"] == 1
        assert t["model_ms"] >= 200
        assert t["tool_ms"] == 0
        assert t["model_share"] == 1.0

    def test_a_slow_tool_is_not_blamed_on_the_model(self):
        """The distinction the whole field exists to make."""
        def slow_tool(**kwargs):
            time.sleep(0.20)
            return {"ok": True}

        with mock_chat(
            side_effect=[
                reply(calls=[tool_call("get_system_info", {})]),
                reply(content="done"),
            ]
        ), patch.dict(agent.TOOLS, {"get_system_info": slow_tool}):
            result = agent.ask("q")

        t = result["timing"]
        assert t["tool_ms"] >= 200
        assert t["model_share"] < 0.5, "slow tool time was attributed to the model"

    def test_one_hung_round_is_visible_among_fast_ones(self):
        """
        The signal that separates a hung request from a uniformly slow run.
        A total cannot tell those apart; slowest_round_ms can.
        """
        calls = []

        def chat(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                time.sleep(0.25)
            if len(calls) >= 3:
                return reply(content="done")
            return reply(calls=[tool_call("get_system_info", {})])

        with mock_chat(side_effect=chat):
            result = agent.ask("q")

        t = result["timing"]
        assert t["rounds"] == 3
        assert len(t["round_ms"]) == 3
        assert t["slowest_round_ms"] >= 250
        assert t["slowest_round_ms"] == max(t["round_ms"])
        # The other rounds were fast, so a total would have hidden this.
        assert sorted(t["round_ms"])[0] < 100

    def test_giving_up_still_reports_timing(self):
        """A run that exhausts MAX_ROUNDS is exactly one worth timing."""
        with mock_chat(
            return_value=reply(calls=[tool_call("get_system_info", {})])
        ):
            result = agent.ask("q")

        assert "Gave up" in result["answer"]
        assert result["timing"]["rounds"] == agent.MAX_ROUNDS


class TestPlaceholderArguments:
    """
    Observed live 2026-08-18: llama3.2 called
    describe_pod(name="bad-image-<random chars>") -- a placeholder out of its
    own reasoning, sent as a literal Kubernetes name. The cluster answers 404,
    which the model reads as "no such pod" rather than "you did not name one".
    """

    def test_a_placeholder_never_reaches_kubernetes(self):
        with patch.dict(agent.TOOLS, {"describe_pod": MagicMock()}) as tools:
            out = json.loads(
                agent._run_tool("describe_pod", {"name": "bad-image-<random chars>"})
            )
            tools["describe_pod"].assert_not_called()

        assert "placeholder" in out["error"]

    def test_the_error_says_what_to_do_instead(self):
        """A 404 does not tell the model it failed to supply a name."""
        out = json.loads(agent._run_tool("describe_pod", {"name": "{pod-name}"}))

        assert "list_pods" in out["error"] or "scan_cluster" in out["error"]

    @pytest.mark.parametrize("value", [
        "bad-image-<random chars>", "<pod-name>", "{name}", "{{ pod }}",
        "your-pod", "the-namespace", "pod_name", "...",
    ])
    def test_placeholder_shapes_are_caught(self, value):
        assert agent.unresolved({"name": value})

    @pytest.mark.parametrize("value", [
        "memory-hog-bc76968c6-87fbc", "demo", "healthy-web", "kube-system",
        "nightly-sync-29784601-4x4b9", "log-shipper-44sbc",
    ])
    def test_real_names_are_untouched(self, value):
        """A guard that rejects real names is worse than no guard."""
        assert agent.unresolved({"name": value}) == []

    def test_an_empty_argument_is_not_a_placeholder(self):
        # Optional arguments are routinely blank; that is a default, not a
        # fabrication.
        assert agent.unresolved({"workload": "", "namespaces": ""}) == []


class TestEvidencePolicy:
    """
    A ConfigMap referenced by a VOLUME leaves the pod in ContainerCreating with
    no waiting message: the name is only in a FailedMount event. Observed live
    2026-08-18 -- the agent read describe_pod, never called get_pod_events, and
    hedged "these ConfigMaps may not exist ... or are misreferenced" when the
    evidence it skipped said `configmap "nginx-conf" not found`.
    """

    STUCK = json.dumps({
        "pod": "missing-configmap-volume",
        "namespace": "config-faults",
        "status": "ContainerCreating",
    })

    def test_a_stuck_pod_without_events_is_a_gap(self):
        assert agent.evidence_gap([], [self.STUCK]) == (
            "events", "missing-configmap-volume", "config-faults",
            "ContainerCreating",
        )

    def test_no_gap_once_events_were_read_for_that_pod(self):
        trace = [{"name": "get_pod_events", "arguments": {
            "name": "missing-configmap-volume", "namespace": "config-faults"}}]

        assert agent.evidence_gap(trace, [self.STUCK]) is None

    def test_events_for_a_different_pod_do_not_close_the_gap(self):
        trace = [{"name": "get_pod_events", "arguments": {
            "name": "some-other-pod", "namespace": "config-faults"}}]

        assert agent.evidence_gap(trace, [self.STUCK]) is not None

    def test_a_healthy_pod_needs_nothing(self):
        pod = json.dumps({"pod": "p", "namespace": "demo", "status": "Running"})

        assert agent.evidence_gap([], [pod]) is None

    def test_an_oomkill_does_not_demand_logs(self):
        """
        The kernel killed it for exceeding a limit describe_pod already
        reports: the status IS the cause, and the logs usually end
        mid-sentence. Requiring them would spend a round on every ordinary
        memory diagnosis.
        """
        pod = json.dumps({"pod": "c", "namespace": "demo", "status": "OOMKilled"})

        assert agent.evidence_gap([], [pod]) is None

    @pytest.mark.parametrize("status", ["CrashLoopBackOff", "Error"])
    def test_a_crashing_pod_whose_logs_were_never_read_is_a_gap(self, status):
        """
        The status is the symptom. Reading it and stopping is how a run ends
        up proposing a cause instead of quoting one.
        """
        pod = json.dumps({"pod": "c", "namespace": "demo", "status": status})

        assert agent.evidence_gap([], [pod])[0] == "logs"

    @pytest.mark.parametrize("tool", ["get_pod_logs", "get_pod_events"])
    def test_reading_either_closes_the_logs_gap(self, tool):
        pod = json.dumps({"pod": "c", "namespace": "demo", "status": "CrashLoopBackOff"})
        trace = [{"name": tool, "arguments": {"name": "c", "namespace": "demo"}}]

        assert agent.evidence_gap(trace, [pod]) is None

    def test_events_win_when_a_pod_wants_both(self):
        """
        An unmountable volume means the container never started, so there are
        no logs to read -- sending the run for logs first would waste its one
        policy on an empty result.
        """
        both = json.dumps({"pod": "v", "namespace": "cf",
                           "status": "CreateContainerConfigError"})

        assert agent.evidence_gap([], [both])[0] == "events"

    def test_the_loop_sends_the_run_back_for_events(self):
        """End to end: the model answers early and is made to collect proof."""
        responses = [
            reply(calls=[tool_call("describe_pod", {"name": "x", "namespace": "config-faults"})]),
            reply(content="It is stuck. The ConfigMap may not exist."),
            reply(calls=[tool_call("get_pod_events", {"name": "missing-configmap-volume",
                                                      "namespace": "config-faults"})]),
            reply(content='configmap "nginx-conf" not found'),
        ]
        stub = {
            "describe_pod": lambda **k: json.loads(TestEvidencePolicy.STUCK),
            "get_pod_events": lambda **k: {"events": ['configmap "nginx-conf" not found']},
        }
        with patch.dict(agent.TOOLS, stub), mock_chat(side_effect=responses):
            result = agent.ask("why is it stuck?")

        assert result["policies"] == 1
        assert "get_pod_events" in [c["name"] for c in result["tool_calls"]]
        assert "nginx-conf" in result["answer"]

    def test_a_run_that_collected_events_itself_is_not_sent_back(self):
        responses = [
            reply(calls=[tool_call("get_pod_events", {"name": "missing-configmap-volume",
                                                      "namespace": "config-faults"})]),
            reply(content="done"),
        ]
        stub = {"get_pod_events": lambda **k: json.loads(TestEvidencePolicy.STUCK)}
        with patch.dict(agent.TOOLS, stub), mock_chat(side_effect=responses):
            result = agent.ask("why is it stuck?")

        assert result["policies"] == 0


class TestEvidenceGapReadsEveryToolShape:
    """
    Observed live 2026-08-19: asked for the crasher pod's status, the model
    called list_pods, answered "Error with 4 restarts" and stopped. The policy
    saw nothing, because the detector only read documents with a top-level
    "pod" key -- and answering straight from a listing is the commonest way to
    stop early.
    """

    def test_a_list_pods_result_is_read(self):
        trace = [{"name": "list_pods", "arguments": {"namespace": "demo"}}]
        out = [json.dumps({"crasher-abc-xyz": {"status": "Error", "ready": "0/1"}})]

        assert agent.evidence_gap(trace, out) == (
            "logs", "crasher-abc-xyz", "demo", "Error",
        )

    def test_the_namespace_comes_from_the_call_that_listed(self):
        """A listing result does not carry one; the call's arguments do."""
        trace = [{"name": "list_pods", "arguments": {"namespace": "payments"}}]
        out = [json.dumps({"api-1": {"status": "CrashLoopBackOff"}})]

        assert agent.evidence_gap(trace, out)[2] == "payments"

    def test_a_scan_cluster_result_points_at_its_example_pod(self):
        trace = [{"name": "scan_cluster", "arguments": {}}]
        out = [json.dumps({"demo/crasher": {
            "status": "CrashLoopBackOff", "pods": 1, "example": "crasher-abc"}})]

        assert agent.evidence_gap(trace, out) == (
            "logs", "crasher-abc", "demo", "CrashLoopBackOff",
        )

    def test_a_healthy_listing_is_not_a_gap(self):
        trace = [{"name": "list_pods", "arguments": {"namespace": "demo"}}]
        out = [json.dumps({"healthy-web-x": {"status": "Running", "ready": "1/1"}})]

        assert agent.evidence_gap(trace, out) is None

    def test_the_truncation_notice_is_not_a_pod(self):
        trace = [{"name": "list_pods", "arguments": {"namespace": "demo"}}]
        out = [json.dumps({
            "crasher-abc": {"status": "Error"},
            "_truncated": "40 more pods not shown",
        })]
        gap = agent.evidence_gap(trace, out)

        assert gap[1] == "crasher-abc"

    def test_reading_logs_for_that_pod_closes_it(self):
        trace = [
            {"name": "list_pods", "arguments": {"namespace": "demo"}},
            {"name": "get_pod_logs", "arguments": {"name": "crasher-abc", "namespace": "demo"}},
        ]
        out = [json.dumps({"crasher-abc": {"status": "Error"}}),
               json.dumps({"pod": "crasher-abc", "logs": "FATAL: db refused"})]

        assert agent.evidence_gap(trace, out) is None


class TestThePolicyTargetsTheRightPod:
    """
    Observed live 2026-08-19: asked about `crasher`, the model listed every
    unhealthy pod in the namespace and the policy pointed at the first one --
    log-shipper. The run collected evidence about a workload nobody had
    mentioned and answered "The crasher pod log-shipper-8gnqk", which is the
    wrong-entity failure this project has spent months removing, reintroduced
    by its own safety net.
    """

    TRACE = [{"name": "list_pods", "arguments": {"namespace": "demo"}}]
    OUT = [json.dumps({
        "log-shipper-8gnqk": {"status": "Error"},
        "crasher-5964d99948-9g8vg": {"status": "Error"},
    })]

    def test_it_picks_the_pod_the_question_named(self):
        gap = agent.evidence_gap(self.TRACE, self.OUT, "why is crasher failing?")

        assert gap[1] == "crasher-5964d99948-9g8vg"

    def test_a_question_naming_nothing_still_gets_a_gap(self):
        gap = agent.evidence_gap(self.TRACE, self.OUT, "what is broken in demo?")

        assert gap is not None

    def test_the_full_pod_name_matches_too(self):
        gap = agent.evidence_gap(
            self.TRACE, self.OUT, "describe crasher-5964d99948-9g8vg please"
        )

        assert gap[1] == "crasher-5964d99948-9g8vg"

    def test_both_suffix_shapes_are_candidates(self):
        """
        A Deployment pod carries two generated suffixes and a DaemonSet pod
        one, and nothing in the name says which. Guessing a single trim turned
        `log-shipper-8gnqk` into `log`, which matches nothing a person types.
        """
        assert "crasher" in agent.workload_prefix("crasher-5964d99948-9g8vg")
        assert "log-shipper" in agent.workload_prefix("log-shipper-8gnqk")
        assert "sidecar-app" in agent.workload_prefix("sidecar-app")

    def test_a_daemonset_pod_is_matched_by_its_workload_name(self):
        trace = [{"name": "list_pods", "arguments": {"namespace": "demo"}}]
        out = [json.dumps({"crasher-59-9g8": {"status": "Error"},
                           "log-shipper-8gnqk": {"status": "Error"}})]
        gap = agent.evidence_gap(trace, out, "why is log-shipper crashing?")

        assert gap[1] == "log-shipper-8gnqk"


class TestThinkingIsConfigurable:
    """
    Exposed so the latency trade-off can be measured rather than argued about.
    The default must not move: without thinking, qwen3 answers multi-part
    questions from the first tool it calls and invents the rest.
    """

    def test_thinking_is_on_by_default(self):
        with mock_chat(return_value=reply(content="x")) as chat:
            agent.ask("q")

        assert chat.call_args.kwargs["think"] is True

    def test_the_environment_can_turn_it_off_for_a_benchmark(self):
        with patch.object(agent, "THINK", False):
            with mock_chat(return_value=reply(content="x")) as chat:
                agent.ask("q")

        assert chat.call_args.kwargs["think"] is False

    def test_an_explicit_argument_still_wins(self):
        with patch.object(agent, "THINK", False):
            with mock_chat(return_value=reply(content="x")) as chat:
                agent.ask("q", think=True)

        assert chat.call_args.kwargs["think"] is True


class TestThePolicyStaysOutOfTheWay:
    """
    The policy caused the wrong-entity failure it was hardened against.
    Measured 2026-08-19: "Is the correctly-configured pod unhealthy?" -- the
    model listed only unhealthy pods, which excludes the healthy one asked
    about, the policy fell back to the first pod listed, and 2 of 3 runs came
    back diagnosing missing-configmap-key instead. 1/3 on a case that had been
    2/2.
    """

    TRACE = [{"name": "list_pods", "arguments": {"namespace": "config-faults"}}]
    OUT = [json.dumps({"missing-configmap-key": {"status": "CreateContainerConfigError"}})]

    def test_it_does_not_fire_on_a_pod_nobody_asked_about(self):
        gap = agent.evidence_gap(
            self.TRACE, self.OUT,
            "Is the correctly-configured pod in config-faults unhealthy?",
        )

        assert gap is None

    def test_it_still_fires_for_the_pod_that_was_asked_about(self):
        gap = agent.evidence_gap(
            self.TRACE, self.OUT, "why is missing-configmap-key stuck?"
        )

        assert gap is not None and gap[1] == "missing-configmap-key"

    def test_a_question_naming_no_object_still_gets_help(self):
        """"What is broken here?" has no target to miss."""
        assert agent.evidence_gap(self.TRACE, self.OUT, "what is broken here?")

    def test_ordinary_words_are_not_mistaken_for_targets(self):
        assert not agent._looks_like_a_target("the")
        assert not agent._looks_like_a_target("unhealthy")
        assert agent._looks_like_a_target("correctly-configured")
        assert agent._looks_like_a_target("crasher-abc123")
