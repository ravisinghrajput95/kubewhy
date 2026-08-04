"""
Tests for the tool-calling loop.

No model is involved: ollama.chat is mocked, so these assert the mechanics --
that tools are dispatched, results fed back, failures contained, and runaway
loops stopped.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agent


def tool_call(name, arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def reply(content=None, calls=None):
    return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))


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
        with patch("agent.ollama.chat", return_value=reply(content="all fine")):
            result = agent.ask("how are things?")

        assert result["answer"] == "all fine"
        assert result["tool_calls"] == []

    def test_executes_tool_then_answers(self):
        responses = [
            reply(calls=[tool_call("get_system_info", {})]),
            reply(content="cpu is low"),
        ]
        with patch("agent.ollama.chat", side_effect=responses):
            result = agent.ask("is the cpu busy?")

        assert result["answer"] == "cpu is low"
        assert result["tool_calls"] == [{"name": "get_system_info", "arguments": {}}]

    def test_tool_results_are_fed_back(self):
        responses = [
            reply(calls=[tool_call("get_system_info", {})]),
            reply(content="done"),
        ]
        with patch("agent.ollama.chat", side_effect=responses) as chat:
            agent.ask("q")

        sent = chat.call_args.kwargs["messages"]
        assert any(m.get("role") == "tool" for m in sent if isinstance(m, dict))

    def test_multi_hop_chain(self):
        responses = [
            reply(calls=[tool_call("list_pods", {"namespace": "demo"})]),
            reply(calls=[tool_call("describe_pod", {"name": "p", "namespace": "demo"})]),
            reply(content="OOMKilled"),
        ]
        with patch("agent.ollama.chat", side_effect=responses):
            result = agent.ask("what is broken?")

        assert [c["name"] for c in result["tool_calls"]] == ["list_pods", "describe_pod"]

    def test_runaway_loop_is_stopped(self):
        """A model that never stops calling tools must not hang forever."""
        forever = reply(calls=[tool_call("get_system_info", {})])
        with patch("agent.ollama.chat", return_value=forever) as chat:
            result = agent.ask("q")

        assert "Gave up" in result["answer"]
        assert chat.call_count == agent.MAX_ROUNDS

    def test_system_prompt_is_sent_first(self):
        with patch("agent.ollama.chat", return_value=reply(content="x")) as chat:
            agent.ask("q")

        assert chat.call_args.kwargs["messages"][0]["role"] == "system"

    def test_answer_carries_a_confidence_verdict(self):
        responses = [
            reply(calls=[tool_call("get_system_info", {})]),
            reply(content="cpu is fine"),
        ]
        with patch("agent.ollama.chat", side_effect=responses):
            result = agent.ask("q")

        assert result["confidence"] in {"grounded", "partial", "ungrounded"}

    def test_invented_figure_is_flagged(self):
        """End to end: a claim no tool produced comes back as unverified."""
        responses = [
            reply(calls=[tool_call("get_platform_info", {})]),
            reply(content="This host has been up for 18 days."),
        ]
        with patch("agent.ollama.chat", side_effect=responses):
            result = agent.ask("how long has it been up?")

        assert result["confidence"] == "partial"
        assert "18" in result["unverified"]

    def test_answer_with_no_tools_is_ungrounded(self):
        with patch("agent.ollama.chat", return_value=reply(content="CPU is 42%.")):
            result = agent.ask("q")

        assert result["confidence"] == "ungrounded"

    def test_falls_back_when_model_has_no_thinking_mode(self):
        """llama3.2 rejects think=True with a 400; that must not be fatal."""
        import ollama as ollama_mod

        error = ollama_mod.ResponseError("llama3.2 does not support thinking")
        with patch("agent.ollama.chat", side_effect=[error, reply(content="ok")]) as chat:
            result = agent.ask("q", model="llama3.2")

        assert result["answer"] == "ok"
        assert chat.call_args.kwargs["think"] is False

    def test_other_response_errors_still_raise(self):
        import ollama as ollama_mod

        with patch("agent.ollama.chat", side_effect=ollama_mod.ResponseError("model not found")):
            with pytest.raises(ollama_mod.ResponseError):
                agent.ask("q")

    def test_thinking_enabled_by_default(self):
        # Without it qwen3 answers multi-part questions from the first tool
        # only and invents the rest.
        with patch("agent.ollama.chat", return_value=reply(content="x")) as chat:
            agent.ask("q")

        assert chat.call_args.kwargs["think"] is True
