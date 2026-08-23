"""
The model backend, behind one seam.

kubewhy's claim is that nothing leaves your network, and a local model is how
it keeps that. But "local" costs a GPU per cluster: measured 2026-08-22 on a
GKE node with eight CPU cores and no accelerator, qwen3 exceeded the 300s
timeout without producing its first token, and needed ~128s per diagnosis even
with thinking off. That is a deployment problem, not a modelling one, and it
should be a choice rather than a property of the product.

So the provider lives here and the agent loop does not know which one it has.
`TRIAGE_BACKEND` selects it; Ollama stays the default, because changing that
default would change what this project claims about your data.

**Three things a backend owns, and the third is the one that bites.** It makes
the call, it normalises the reply -- and it owns the *wire shape of messages*,
because providers disagree there in ways the loop must not care about. Ollama
takes back its own assistant object and identifies a tool result by
`tool_name`; OpenAI wants a dict and matches results to calls by
`tool_call_id`. A seam that normalised only the reply would push that
difference into the loop, which is where it would rot.

**What a new backend must satisfy, beyond the method signatures:**

- Tool schemas. Ollama introspects the Python callables in `agent.TOOLS`
  directly, including their docstrings, which are written as prompt
  engineering rather than documentation. A provider needing JSON Schema has to
  derive it from the same source, or the model reads different descriptions
  than the ones this project's eval results were measured against.
- `think` is not portable. It is Ollama's flag; reasoning models elsewhere
  express it differently and most models not at all. `chat()` returns the
  value actually used so the caller can record what it got rather than what it
  asked for.
- Errors are data. A backend raises only for genuinely unrecoverable
  conditions; anything the loop can survive should come back as a reply.

**And a new backend is not done when it runs.** The suite in `evals/` is the
only check on a model change: 16 cases, the controller, and grounding. A
backend added without those numbers is unverified, whatever its tests say.
"""

import json
import os

import httpx
import ollama

import tool_schema

# Where the seam is chosen. Ollama by default: a different default would make
# the README's "nothing leaves your network" false for anyone who upgraded
# without reading a changelog.
BACKEND = os.getenv("TRIAGE_BACKEND", "ollama")

TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "300"))

# For the OpenAI-protocol backend. The default is the hosted service, but
# pointing this at http://localhost:11434/v1 runs the same protocol against a
# local Ollama -- which is how the seam is validated without a key.
BASE_URL = os.getenv("TRIAGE_OPENAI_BASE_URL", "https://api.openai.com/v1")
API_KEY = os.getenv("OPENAI_API_KEY", "")
KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE") or None


class ToolCall:
    """One tool the model asked for, in provider-neutral form."""

    __slots__ = ("name", "arguments", "id", "raw")

    def __init__(self, name, arguments, id=None, raw=None):
        self.name = name
        # Always a dict, never None: every caller does dict(...) on it and a
        # provider returning null arguments should not become a TypeError
        # three frames away.
        self.arguments = dict(arguments or {})
        self.id = id
        self.raw = raw


class Reply:
    """One model turn: what it said, what it wants to call, and what it was."""

    __slots__ = ("content", "tool_calls", "think_used", "raw")

    def __init__(self, content, tool_calls, think_used, raw):
        self.content = content or ""
        self.tool_calls = list(tool_calls or [])
        # What the provider actually did, not what was requested -- a model
        # without a thinking mode silently answers without one, and a run that
        # records the request rather than the outcome cannot say which arm it
        # measured.
        self.think_used = think_used
        # The provider's own object, kept so a backend can hand it straight
        # back in assistant_message() without a lossy round trip.
        self.raw = raw


class OllamaBackend:
    """The default, and the only one whose numbers this project has measured."""

    name = "ollama"

    def chat(self, model, messages, tools, think):
        """
        One model call, degrading gracefully when the model has no thinking mode.

        Only some models support it -- llama3.2 rejects the request outright
        with a 400 -- so rather than making thinking a hard requirement, fall
        back and let the caller decide whether the answers are good enough.

        A Client is built per call so the timeout applies; module-level
        ollama.chat() ignores it.
        """
        client = ollama.Client(timeout=TIMEOUT)
        try:
            response = client.chat(
                model=model, messages=messages, tools=tools, think=think,
                keep_alive=KEEP_ALIVE,
            )
            return self._reply(response, think)
        except ollama.ResponseError as exc:
            if think and "does not support thinking" in str(exc):
                response = client.chat(
                    model=model, messages=messages, tools=tools, think=False,
                    keep_alive=KEEP_ALIVE,
                )
                return self._reply(response, False)
            raise

    @staticmethod
    def _reply(response, think_used):
        message = response.message
        calls = [
            ToolCall(call.function.name, call.function.arguments, raw=call)
            for call in (message.tool_calls or [])
        ]
        return Reply(message.content, calls, think_used, message)

    @staticmethod
    def assistant_message(reply):
        # Ollama's own Message object, handed straight back. Rebuilding it as a
        # dict drops the thinking field the server round-trips.
        return reply.raw

    @staticmethod
    def tool_message(call, output):
        # Ollama matches a result to its call by tool name. There is no
        # tool_call_id in this protocol, which is exactly why this method
        # belongs to the backend.
        return {"role": "tool", "tool_name": call.name, "content": output}

    @staticmethod
    def tools(registry):
        # The callables themselves: Ollama builds the schema by introspecting
        # name, signature and docstring. functools.wraps on any wrapper is
        # load-bearing here -- an unwrapped one once handed the model a tool
        # called "wrapper".
        return list(registry.values())


class OpenAICompatBackend:
    """
    Anything speaking the OpenAI chat-completions protocol.

    Including Ollama itself, which serves `/v1/chat/completions` alongside its
    native API -- and that is how this gets validated without an API key or a
    bill. Same model, same cluster, two wire formats: any difference between
    the arms is the seam rather than the model.

    Written with httpx rather than the openai SDK on purpose. The protocol
    surface used here is one POST, and taking the SDK would add a dependency
    to a project whose default path never speaks to a hosted service at all.

    **Three differences from the native protocol, and the first is the one a
    naive port gets wrong:**

    - Arguments arrive as a JSON *string*, not a dict. Ollama's native API
      returns them parsed. Passing the string on would have every tool receive
      one positional blob and fail in a way that looks like the model calling
      tools wrongly.
    - Tool results are matched by `tool_call_id`, not by tool name. Omitting it
      is a 400 from the API, not a degraded answer.
    - Assistant messages go back as plain dicts. There is no provider object
      to hand back, so the raw dict is what gets appended.
    """

    name = "openai"

    def __init__(self, base_url=None, api_key=None, timeout=None):
        self.base_url = (base_url or BASE_URL).rstrip("/")
        # Ollama needs no key at all; a hosted API requires one. The header is
        # omitted entirely when there is none, because "Bearer " with an empty
        # value is an illegal HTTP header value and httpx refuses to send it --
        # failing locally with LocalProtocolError rather than reaching the
        # provider. Measured against a local Ollama on 2026-08-22, which is
        # precisely the keyless case this backend has to support.
        self.api_key = api_key if api_key is not None else API_KEY
        self.timeout = timeout or TIMEOUT

    def chat(self, model, messages, tools, think):
        payload = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        response = httpx.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]

        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            calls.append(ToolCall(
                function.get("name"),
                self._arguments(function.get("arguments")),
                id=call.get("id"),
                raw=call,
            ))

        # think is not portable: it is Ollama's flag, reasoning models express
        # it differently and most models not at all. Reported as False rather
        # than echoed, so a set records what happened rather than what was
        # asked for.
        return Reply(message.get("content"), calls, False, message)

    @staticmethod
    def _arguments(raw):
        """
        Arguments, whatever shape they arrived in.

        The protocol says JSON string. Ollama's /v1 endpoint has been seen
        returning a dict already parsed, so accept both -- and treat
        unparseable JSON as empty rather than raising, because a malformed
        argument blob is the model's mistake and the loop is built to survive
        those as data.
        """
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def assistant_message(reply):
        return reply.raw

    @staticmethod
    def tool_message(call, output):
        # tool_call_id, not tool name. Omitting it is a 400.
        return {"role": "tool", "tool_call_id": call.id, "content": output}

    @staticmethod
    def tools(registry):
        return tool_schema.schemas_for(registry)


_BACKENDS = {"ollama": OllamaBackend, "openai": OpenAICompatBackend}


def get(name=None):
    """
    The configured backend.

    Unknown names fail loudly at startup rather than falling back to the
    default: a typo in TRIAGE_BACKEND that silently ran Ollama would produce a
    set of results labelled as something else, and this project has already
    published one set of numbers that measured the wrong thing.
    """
    chosen = name or BACKEND
    try:
        return _BACKENDS[chosen]()
    except KeyError:
        raise ValueError(
            f"unknown TRIAGE_BACKEND {chosen!r}; available: "
            f"{', '.join(sorted(_BACKENDS))}"
        ) from None


def register(name, factory):
    """Add a backend. Kept small on purpose -- see the module docstring."""
    _BACKENDS[name] = factory
