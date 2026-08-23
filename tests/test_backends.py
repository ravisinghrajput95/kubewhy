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


def openai_reply(content=None, calls=()):
    """A response shaped the way an OpenAI-protocol endpoint returns one."""
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {"id": id, "type": "function",
                     "function": {"name": name, "arguments": arguments}}
                    for id, name, arguments in calls
                ] or None,
            }
        }]
    }


class TestOpenAIProtocol:
    """
    The second protocol, validated against a local Ollama /v1 endpoint.

    Ollama serves chat-completions alongside its native API, which is how this
    backend gets exercised against a real tool-calling model with no API key
    and no bill. These unit tests pin the translation; the live check is in
    the commit that added it.
    """

    def backend(self):
        return backends.get("openai")

    def post(self, payload):
        response = MagicMock()
        response.json.return_value = payload
        response.raise_for_status = MagicMock()
        return patch("backends.httpx.post", return_value=response)

    def test_arguments_arrive_as_a_json_string_and_are_parsed(self):
        # The difference a naive port gets wrong. Ollama's native API returns
        # arguments already parsed; this protocol sends a string. Passing it
        # on would hand every tool one positional blob, failing in a way that
        # looks like the model calling tools wrongly.
        payload = openai_reply(calls=[
            ("call_1", "get_pod_logs", '{"name": "crasher", "tail": 5}')
        ])
        with self.post(payload):
            reply = self.backend().chat("qwen3", [], [], think=False)

        assert reply.tool_calls[0].arguments == {"name": "crasher", "tail": 5}
        assert reply.tool_calls[0].id == "call_1"

    def test_arguments_already_parsed_are_accepted_too(self):
        # Ollama's /v1 endpoint has been seen returning a dict.
        payload = openai_reply(calls=[("call_1", "list_pods", {"namespace": "demo"})])
        with self.post(payload):
            reply = self.backend().chat("qwen3", [], [], think=False)
        assert reply.tool_calls[0].arguments == {"namespace": "demo"}

    def test_unparseable_arguments_become_empty_rather_than_raising(self):
        # A malformed blob is the model's mistake, and this loop is built to
        # survive those as data rather than as an exception three frames away.
        payload = openai_reply(calls=[("call_1", "list_pods", "{not json")])
        with self.post(payload):
            reply = self.backend().chat("qwen3", [], [], think=False)
        assert reply.tool_calls[0].arguments == {}

    def test_tool_results_are_matched_by_id_not_name(self):
        # Omitting tool_call_id is a 400 from a hosted API, not a worse answer.
        call = backends.ToolCall("get_pod_logs", {}, id="call_9")
        assert self.backend().tool_message(call, "{}") == {
            "role": "tool", "tool_call_id": "call_9", "content": "{}",
        }

    def test_no_api_key_sends_no_authorization_header(self):
        """
        The keyless case, which is the local-Ollama case.

        "Bearer " with an empty value is an illegal HTTP header value: httpx
        refuses to send it and fails with LocalProtocolError before reaching
        the provider. Found on 2026-08-22 by pointing this backend at a local
        Ollama -- a mocked client would never have shown it.
        """
        with self.post(openai_reply(content="ok")) as post:
            backends.OpenAICompatBackend(api_key="").chat("qwen3", [], [], False)
        assert "Authorization" not in post.call_args.kwargs["headers"]

    def test_a_key_is_sent_when_present(self):
        with self.post(openai_reply(content="ok")) as post:
            backends.OpenAICompatBackend(api_key="sk-test").chat("qwen3", [], [], False)
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-test"

    def test_think_is_reported_false_because_it_does_not_port(self):
        # think is Ollama's flag. Echoing the request would let a set record an
        # arm it never ran.
        with self.post(openai_reply(content="ok")):
            reply = self.backend().chat("qwen3", [], [], think=True)
        assert reply.think_used is False

    def test_tools_are_sent_as_json_schema(self):
        import agent

        schemas = self.backend().tools(agent.TOOLS)
        assert len(schemas) == len(agent.TOOLS)
        assert schemas[0]["type"] == "function"
        assert "parameters" in schemas[0]["function"]

    def test_no_tools_means_no_tools_key(self):
        # Some endpoints reject an empty tools array.
        with self.post(openai_reply(content="ok")) as post:
            self.backend().chat("qwen3", [], [], think=False)
        assert "tools" not in post.call_args.kwargs["json"]
