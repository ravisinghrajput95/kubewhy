"""
Tests for the model-backend seam.

The seam exists so the provider can change without the agent loop knowing.
That means the interesting assertions are not "does it call ollama" but "does
the loop get the same shape whatever is behind it" -- and, in the one place
providers genuinely disagree, that the *backend* owns the difference rather
than the loop.

No model and no network: the client is mocked.
"""

from unittest.mock import MagicMock, patch

import pytest

import backends


def ollama_reply(content="", calls=()):
    """A response shaped the way the ollama client returns one."""
    message = MagicMock()
    message.content = content
    message.tool_calls = [
        MagicMock(function=MagicMock(name_=name, arguments=arguments))
        for name, arguments in calls
    ]
    # MagicMock treats `name` as its own constructor kwarg, so it cannot be set
    # positionally -- the attribute has to be assigned afterwards or every tool
    # comes back named "<MagicMock id=...>".
    for mock_call, (name, _) in zip(message.tool_calls, calls):
        mock_call.function.name = name
    response = MagicMock()
    response.message = message
    return response


class TestSelection:
    def test_ollama_is_the_default(self):
        assert backends.get().name == "ollama"

    def test_an_unknown_backend_fails_loudly(self):
        # Never a silent fallback to the default: a typo that quietly ran
        # Ollama would produce results labelled as something else, and this
        # project has already published one set of numbers that measured the
        # wrong thing.
        with pytest.raises(ValueError) as caught:
            backends.get("gpt-fantasy")
        assert "unknown TRIAGE_BACKEND" in str(caught.value)
        assert "ollama" in str(caught.value)

    def test_a_registered_backend_is_selectable(self):
        class Stub:
            name = "stub"

        backends.register("stub", Stub)
        try:
            assert backends.get("stub").name == "stub"
        finally:
            backends._BACKENDS.pop("stub", None)


class TestNormalisation:
    def test_a_plain_answer_becomes_a_reply(self):
        client = MagicMock()
        client.chat.return_value = ollama_reply(content="it is fine")
        with patch("backends.ollama.Client", return_value=client):
            reply = backends.get().chat("qwen3", [], [], True)

        assert reply.content == "it is fine"
        assert reply.tool_calls == []
        assert reply.think_used is True

    def test_tool_calls_are_normalised_off_the_provider_shape(self):
        client = MagicMock()
        client.chat.return_value = ollama_reply(
            calls=[("get_pod_logs", {"name": "crasher", "namespace": "demo"})]
        )
        with patch("backends.ollama.Client", return_value=client):
            reply = backends.get().chat("qwen3", [], [], False)

        assert len(reply.tool_calls) == 1
        call = reply.tool_calls[0]
        # The loop reads .name and .arguments. It must never have to know that
        # ollama nests them under .function.
        assert call.name == "get_pod_logs"
        assert call.arguments == {"name": "crasher", "namespace": "demo"}

    def test_null_arguments_become_an_empty_dict(self):
        # Every caller does dict(...) on this. A provider returning null should
        # not become a TypeError three frames away in the tool dispatcher.
        client = MagicMock()
        client.chat.return_value = ollama_reply(calls=[("list_pods", None)])
        with patch("backends.ollama.Client", return_value=client):
            reply = backends.get().chat("qwen3", [], [], False)

        assert reply.tool_calls[0].arguments == {}


class TestThinkingIsReportedNotAssumed:
    def test_a_model_without_thinking_falls_back_and_says_so(self):
        import ollama as ollama_mod

        client = MagicMock()
        client.chat.side_effect = [
            ollama_mod.ResponseError("llama3.2 does not support thinking"),
            ollama_reply(content="ok"),
        ]
        with patch("backends.ollama.Client", return_value=client):
            reply = backends.get().chat("llama3.2", [], [], True)

        # Asked for thinking, did not get it, and says which -- a run that
        # recorded the request rather than the outcome could not say which arm
        # it measured.
        assert reply.think_used is False
        assert client.chat.call_args.kwargs["think"] is False

    def test_an_unrelated_error_is_not_swallowed(self):
        import ollama as ollama_mod

        client = MagicMock()
        client.chat.side_effect = ollama_mod.ResponseError("model not found")
        with patch("backends.ollama.Client", return_value=client):
            with pytest.raises(ollama_mod.ResponseError):
                backends.get().chat("nope", [], [], True)


class TestTheBackendOwnsTheWireShape:
    """
    The reason this is a seam and not just a function.

    Ollama matches a tool result to its call by tool *name*; OpenAI matches by
    tool_call_id and wants a dict rather than its own message object. A seam
    that normalised only the reply would leave that difference in the loop,
    which is where it would rot.
    """

    def test_tool_results_carry_the_name_ollama_matches_on(self):
        call = backends.ToolCall("get_pod_logs", {"name": "crasher"})
        message = backends.get().tool_message(call, '{"logs": "boom"}')
        assert message == {
            "role": "tool",
            "tool_name": "get_pod_logs",
            "content": '{"logs": "boom"}',
        }

    def test_the_assistant_message_is_handed_back_unchanged(self):
        # Rebuilding it as a dict drops the thinking field the server
        # round-trips, and the next request then carries less than it did.
        raw = MagicMock()
        reply = backends.Reply("hi", [], True, raw)
        assert backends.get().assistant_message(reply) is raw

    def test_tools_are_passed_as_callables_for_introspection(self):
        # Ollama builds the schema from name, signature and docstring. A
        # provider needing JSON Schema derives it from the same source -- see
        # the module docstring.
        registry = {"a": lambda: None, "b": lambda: None}
        passed = backends.get().tools(registry)
        assert passed == list(registry.values())
