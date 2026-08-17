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
        with mock_chat(side_effect=responses):
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

        assert result["confidence"] in {"grounded", "partial", "ungrounded"}

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
        with mock_chat(side_effect=responses):
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

    def test_ask_matches_the_streams_answer(self):
        """ask() is stream() drained; if these drift, one of them is a lie."""
        def run(func):
            with mock_chat(
                side_effect=[
                    reply(calls=[tool_call("get_system_info", {})]),
                    reply(content="cpu is low"),
                ]
            ):
                return func()

        streamed = run(lambda: list(agent.stream("q"))[-1])
        asked = run(lambda: agent.ask("q"))

        assert "type" not in asked
        # Same fields, both ways. This is the half that catches drift.
        assert set(asked) == set(streamed) - {"type"}

        # Everything except timing must be equal. Timing is measured, so two
        # runs of the same mocked chain legitimately differ by a few
        # microseconds -- comparing it by value would make this test flaky for
        # the one field that is supposed to vary.
        ignore = {"type", "timing"}
        assert {k: v for k, v in asked.items() if k not in ignore} == {
            k: v for k, v in streamed.items() if k not in ignore
        }
        assert set(asked["timing"]) == set(streamed["timing"])


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
