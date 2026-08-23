"""
Tests for the inference gateway.

No model, no network, no cluster: the providers are stubs registered into
`backends._BACKENDS` and removed again. That is the point of the seam -- if
testing "does the fallback fire" needed a real outage, it would never be
tested, which is how failover code comes to be discovered broken during the
first real one.

The assertions here are mostly about refusal. A gateway that sends a request
is easy to verify by watching the stub receive it; a gateway that *declines*
to send one is the behaviour this module exists for, and the only way to know
it declines for the right reason is to check the reason.
"""

import json

import pytest

import backends
import inference
import observability
import redaction
import telemetry


class Recorder:
    """A stub provider that records what it was asked and answers blandly."""

    name = "recorder"
    wire = "ollama"

    calls = []

    def __init__(self, endpoint=None, api_key=None, timeout=None):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout

    def chat(self, model, messages, tools, think):
        Recorder.calls.append({"model": model, "messages": messages,
                               "tools": tools, "think": think,
                               "endpoint": self.endpoint})
        return backends.Reply("ok", [], think, {"role": "assistant"},
                              {"prompt": 11, "completion": 7})

    def assistant_message(self, reply):
        return reply.raw

    def tool_message(self, call, output):
        return {"role": "tool", "tool_name": call.name, "content": output}

    def tools(self, registry):
        return list(registry.values())


class Broken(Recorder):
    """A provider that is down in whichever way the test asks for."""

    name = "broken"
    wire = "ollama"
    raises = ConnectionError("connection refused")

    def chat(self, model, messages, tools, think):
        raise type(self).raises


class OtherWire(Recorder):
    """A provider that speaks the other protocol."""

    name = "otherwire"
    wire = "openai"


@pytest.fixture(autouse=True)
def stubs():
    """Register the stub providers, and take them out again afterwards."""
    Recorder.calls = []
    telemetry.reset()
    inference.reset()
    for stub in (Recorder, Broken, OtherWire):
        backends.register(stub.name, stub)
    try:
        yield
    finally:
        for stub in (Recorder, Broken, OtherWire):
            backends._BACKENDS.pop(stub.name, None)
        inference.reset()
        telemetry.reset()


def target(mode="local", provider="recorder", endpoint="http://localhost:11434",
           model="stub-model", **kwargs):
    return inference.Target(mode=mode, provider=provider, endpoint=endpoint,
                            model=model, **kwargs)


def gateway(primary=None, fallback=None, **policy):
    config = inference.Config(primary or target(), fallback,
                              inference.Policy(**policy))
    return inference.Gateway(config)


class TestDestination:
    """
    Where an endpoint points, decided on the name as written.

    This is the whole security boundary. Everything else in the module trusts
    it, so it is tested against the addresses that actually appear in this
    project's own manifests, compose file and docs rather than against
    invented ones.
    """

    @pytest.mark.parametrize("endpoint", [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
        "http://host.docker.internal:11434",          # docker-compose.yml
        "http://ollama.ollama.svc.cluster.local:11434",  # the chart default
        "http://kubewhy-ollama.default.svc:11434",
        "http://ollama:11434",                        # a Service, no dots
        "ollama:11434",                               # and with no scheme
        "http://10.4.0.7:8000",                       # a pod IP
        "http://192.168.1.50:11434",
        "http://172.16.3.2:11434",
        "http://vllm.inference.svc.cluster.local:8000/v1",
    ])
    def test_on_network_addresses_are_internal(self, endpoint):
        assert inference.destination(endpoint) == "internal"

    @pytest.mark.parametrize("endpoint", [
        "https://api.openai.com/v1",
        "https://generativelanguage.googleapis.com/v1beta",
        "http://8.8.8.8:11434",
        "https://my-llm.example.com/v1",
    ])
    def test_off_network_addresses_are_external(self, endpoint):
        assert inference.destination(endpoint) == "external"

    @pytest.mark.parametrize("endpoint", ["", None, "http://", "::::"])
    def test_an_unreadable_endpoint_is_external(self, endpoint):
        """
        Wrong in the direction that refuses. An endpoint this cannot parse is
        not a reason to assume the safe answer -- the check exists precisely
        for the cases nobody thought about.
        """
        assert inference.destination(endpoint) == "external"

    def test_a_trailing_dot_is_the_same_host(self):
        # `svc.cluster.local.` is the fully-qualified spelling and resolvers
        # emit it. Reading it as a different host would refuse a correct
        # address.
        assert inference.destination(
            "http://ollama.ollama.svc.cluster.local.:11434") == "internal"


class TestConfiguration:
    def test_local_ollama_is_the_default(self):
        config = inference.from_env({})

        assert config.primary.mode == "local"
        assert config.primary.provider == "ollama"
        assert config.primary.destination == "internal"
        assert config.fallback is None
        assert config.policy.allow_external is False

    def test_the_legacy_backend_variable_still_selects_a_mode(self):
        """
        TRIAGE_BACKEND named a protocol, which is narrower than a mode. Anyone
        who set it keeps the behaviour they had.
        """
        assert inference.from_env({"TRIAGE_BACKEND": "ollama"}).primary.mode == "local"
        assert inference.from_env(
            {"TRIAGE_BACKEND": "vllm"}).primary.mode == "cluster"

        api = inference.from_env({"TRIAGE_BACKEND": "openai",
                                  "TRIAGE_ALLOW_EXTERNAL_INFERENCE": "true"})
        assert api.primary.mode == "api"
        assert api.primary.provider == "openai"

    def test_the_mode_variable_wins_over_the_legacy_one(self):
        config = inference.from_env({"TRIAGE_BACKEND": "ollama",
                                     "TRIAGE_INFERENCE_MODE": "cluster"})

        assert config.primary.mode == "cluster"

    def test_ollama_host_is_still_the_endpoint(self):
        """
        The Helm chart sets OLLAMA_HOST and nothing else, so a cluster running
        the published chart configures its endpoint entirely through it.
        Reading it here rather than letting the client pick it up quietly is
        what lets the egress check see the address at all.
        """
        config = inference.from_env({
            "TRIAGE_INFERENCE_MODE": "cluster",
            "OLLAMA_HOST": "http://ollama.ai.svc.cluster.local:11434",
        })

        assert config.primary.endpoint.endswith("ai.svc.cluster.local:11434")
        assert config.primary.destination == "internal"

    def test_an_unknown_mode_fails_loudly(self):
        with pytest.raises(ValueError) as caught:
            inference.from_env({"TRIAGE_INFERENCE_MODE": "onprem"})
        assert "onprem" in str(caught.value)

    def test_an_unknown_provider_fails_loudly(self):
        with pytest.raises(ValueError) as caught:
            inference.Target(mode="cluster", provider="tensorrt")
        assert "tensorrt" in str(caught.value)

    def test_neither_endpoint_nor_key_appears_in_the_description(self):
        """
        describe() is what goes into the startup log. An endpoint can carry a
        token in its userinfo or its query string, and this project redacts
        pod logs precisely so credentials stay out of scrollback.
        """
        described = target(
            endpoint="http://user:hunter2@ollama:11434?api-key=sk-secret",
            api_key="sk-live-abcdef",
        ).describe()

        assert "hunter2" not in str(described)
        assert "sk-" not in str(described)
        assert described["mode"] == "local"


class TestExternalDataPolicy:
    def test_an_on_network_mode_may_not_point_off_network(self):
        """
        The check that makes the mode mean something. Without it,
        `mode: cluster` pointed at a vendor installs cleanly, logs itself as
        in-cluster inference, and ships pod logs anyway.
        """
        with pytest.raises(ValueError) as caught:
            gateway(target(mode="cluster", provider="recorder",
                           endpoint="https://api.openai.com/v1"),
                    allow_external=True)

        assert "api.openai.com" in str(caught.value)
        assert "off it" in str(caught.value)

    def test_external_inference_is_refused_unless_allowed(self):
        with pytest.raises(ValueError) as caught:
            gateway(target(mode="api", provider="recorder",
                           endpoint="https://api.openai.com/v1"))

        assert "TRIAGE_ALLOW_EXTERNAL_INFERENCE" in str(caught.value)

    def test_external_inference_works_once_allowed(self):
        gate = gateway(target(mode="api", provider="recorder",
                              endpoint="https://api.openai.com/v1"),
                       allow_external=True)

        assert gate.chat("m", [{"role": "user", "content": "hi"}], [], False)

    def test_api_mode_pointed_at_a_local_endpoint_is_allowed(self):
        """
        The asymmetry, and it is deliberate. Claiming more egress than occurs
        is never the unsafe direction -- and pointing api mode at a local
        Ollama /v1 is how the OpenAI-protocol backend is validated without a
        key or a bill.
        """
        gate = gateway(target(mode="api", provider="recorder",
                              endpoint="http://localhost:11434/v1"))

        assert gate.chat("m", [], [], False)

    def test_a_request_is_refused_even_if_the_policy_changed_after_startup(self):
        """
        Enforced per request as well as at construction. A policy checked only
        at startup is one a later mutation walks past.
        """
        gate = gateway(target(mode="api", provider="recorder",
                              endpoint="https://api.openai.com/v1"),
                       allow_external=True)
        gate.config.policy.allow_external = False

        with pytest.raises(PermissionError):
            gate.chat("m", [], [], False)

        assert telemetry.EGRESS_DENIED.values

    def test_evidence_is_redacted_again_at_the_boundary(self):
        gate = gateway(target(mode="api", provider="recorder",
                              endpoint="https://api.openai.com/v1"),
                       allow_external=True)
        leak = "FATAL: DB_PASSWORD=hunter2ssss could not connect"

        gate.chat("m", [{"role": "tool", "content": leak}], [], False)

        sent = Recorder.calls[-1]["messages"][0]["content"]
        assert "hunter2ssss" not in sent
        assert "REDACTED" in sent

    def test_the_boundary_pass_does_not_touch_an_internal_request(self):
        """
        Redaction is lossy -- see grounding.py on why a redaction pass that
        corrupts a pod name is worse than what it prevents. Paying that cost
        on a request that never leaves the network buys nothing.
        """
        gate = gateway(target())
        original = [{"role": "tool", "content": "token=abcdefghijkl"}]

        gate.chat("m", original, [], False)

        assert Recorder.calls[-1]["messages"] is original

    def test_redaction_can_be_turned_off_deliberately(self):
        gate = gateway(target(mode="api", provider="recorder",
                              endpoint="https://api.openai.com/v1"),
                       allow_external=True, redact_on_egress=False)
        original = [{"role": "user", "content": "token=abcdefghijkl"}]

        gate.chat("m", original, [], False)

        assert Recorder.calls[-1]["messages"] is original


class TestFailover:
    def test_there_is_no_fallback_unless_it_is_enabled(self):
        """
        A configured fallback that was never enabled is dropped rather than
        carried and skipped, so "is there a fallback?" has one answer.
        """
        config = inference.Config(target(provider="broken"),
                                  target(provider="recorder"),
                                  inference.Policy(fallback_enabled=False))

        assert config.fallback is None
        with pytest.raises(ConnectionError):
            inference.Gateway(config).chat("m", [], [], False)

    def test_an_unavailable_primary_falls_back(self):
        gate = gateway(target(provider="broken"), target(provider="recorder"),
                       fallback_enabled=True)

        assert gate.chat("m", [], [], False).content == "ok"
        assert telemetry.FALLBACKS.values

    def test_the_fallback_is_called_with_its_own_model(self):
        """
        Never the primary's. A fallback is a different provider serving a
        different catalogue, and `qwen3` is not a model a hosted API knows --
        so inheriting it produces a 404 at the one moment the primary is
        already down.
        """
        gate = gateway(target(provider="broken", model="qwen3"),
                       target(provider="recorder", model="gpt-4o-mini"),
                       fallback_enabled=True)

        gate.chat("qwen3", [], [], False)

        assert Recorder.calls[-1]["model"] == "gpt-4o-mini"

    def test_a_fallback_without_its_own_model_is_refused(self):
        with pytest.raises(ValueError) as caught:
            inference.Config(
                target(),
                inference.Target(mode="api", provider="recorder",
                                 endpoint="http://localhost:1/v1"),
                inference.Policy(fallback_enabled=True),
            )
        assert "own model" in str(caught.value)

    @pytest.mark.parametrize("error", [
        ConnectionError("refused"),
        OSError("name resolution failed"),
        TimeoutError("timed out"),
    ])
    def test_being_unable_to_answer_is_what_triggers_it(self, error):
        Broken.raises = error
        try:
            gate = gateway(target(provider="broken"),
                           target(provider="recorder"), fallback_enabled=True)
            assert gate.chat("m", [], [], False).content == "ok"
        finally:
            Broken.raises = ConnectionError("connection refused")

    def test_refusing_to_answer_is_not(self):
        """
        A 400 is a malformed request and a 401 is a wrong key. Both fail
        identically on the fallback, and quietly succeeding elsewhere would
        hide a configuration error an operator has to see.
        """
        import httpx

        Broken.raises = httpx.HTTPStatusError(
            "unauthorized", request=httpx.Request("POST", "http://x"),
            response=httpx.Response(401),
        )
        try:
            gate = gateway(target(provider="broken"),
                           target(provider="recorder"), fallback_enabled=True)
            with pytest.raises(httpx.HTTPStatusError):
                gate.chat("m", [], [], False)
            assert Recorder.calls == []
        finally:
            Broken.raises = ConnectionError("connection refused")

    def test_a_policy_refusal_is_never_failed_over(self):
        """A fallback is not a way around the external-data policy."""
        gate = gateway(
            target(mode="api", provider="recorder",
                   endpoint="https://api.openai.com/v1"),
            target(provider="recorder", model="local"),
            allow_external=True, fallback_enabled=True,
        )
        gate.config.policy.allow_external = False

        with pytest.raises(PermissionError):
            gate.chat("m", [], [], False)
        assert Recorder.calls == []

    def test_a_run_in_progress_does_not_cross_wire_protocols(self):
        """
        Halfway through, the history is Ollama Message objects matched by
        tool_name. Handing that to a provider matching by tool_call_id is a
        400 that reads as the fallback being broken.
        """
        gate = gateway(target(provider="broken"),
                       target(mode="api", provider="otherwire",
                              endpoint="http://localhost:8000/v1",
                              model="other"),
                       fallback_enabled=True)
        started = [{"role": "user", "content": "q"},
                   {"role": "assistant", "content": ""},
                   {"role": "tool", "tool_name": "list_pods", "content": "{}"}]

        with pytest.raises(ConnectionError):
            gate.chat("m", started, [], False)

    def test_a_run_that_has_not_started_may_cross_them(self):
        """
        System and user messages are the same dict on every protocol, so a run
        that has called nothing can move anywhere.
        """
        gate = gateway(target(provider="broken"),
                       target(mode="api", provider="otherwire",
                              endpoint="http://localhost:8000/v1",
                              model="other"),
                       fallback_enabled=True)

        assert gate.chat("m", [{"role": "user", "content": "q"}],
                         [], False).content == "ok"

    def test_a_run_in_progress_may_move_between_providers_sharing_a_wire(self):
        gate = gateway(target(provider="broken"),
                       target(provider="recorder", model="second"),
                       fallback_enabled=True)
        started = [{"role": "assistant", "content": ""}]

        assert gate.chat("m", started, [], False).content == "ok"

    def test_message_shaping_follows_whoever_answered(self):
        """
        The subtle one. After a failover, assistant_message() and
        tool_message() have to be shaped by the provider that replied -- not
        by the configured primary, whose protocol the next message would
        otherwise be built in.
        """
        gate = gateway(target(provider="broken"),
                       target(mode="api", provider="otherwire",
                              endpoint="http://localhost:8000/v1",
                              model="other"),
                       fallback_enabled=True)

        assert gate.name == "broken"
        gate.chat("m", [{"role": "user", "content": "q"}], [], False)
        assert gate.name == "otherwire"

    def test_a_dead_primary_is_not_probed_again_every_round(self):
        """
        Found live on 2026-08-23, not by reading the code. With a dead primary
        and a working fallback, one investigation failed over on every round --
        nothing remembered the primary had just refused. A refused connection
        costs milliseconds and hid it; a primary that times out costs
        OLLAMA_TIMEOUT, and MAX_ROUNDS is 8.
        """
        gate = gateway(target(provider="broken"), target(provider="recorder"),
                       fallback_enabled=True)

        for _ in range(4):
            gate.chat("m", [], [], False)

        # One failover, not four: the rounds after the first went straight to
        # the fallback.
        assert sum(telemetry.FALLBACKS.values.values()) == 1
        assert len(Recorder.calls) == 4

    def test_the_primary_is_tried_again_once_the_window_elapses(self):
        """
        A breaker that never closes is an outage that needs a restart to
        recover from.
        """
        gate = gateway(target(provider="broken"), target(provider="recorder"),
                       fallback_enabled=True)
        gate.chat("m", [], [], False)
        assert gate._primary_down_until > 0

        gate._primary_down_until = 0.0
        gate.chat("m", [], [], False)

        assert sum(telemetry.FALLBACKS.values.values()) == 2

    def test_a_recovered_primary_closes_the_breaker(self):
        gate = gateway(target(provider="recorder"), target(provider="recorder",
                                                           model="second"),
                       fallback_enabled=True)
        gate._primary_down_until = float("inf")

        gate.chat("m", [], [], False)          # goes to the fallback
        gate._primary_down_until = 0.0
        gate.chat("m", [], [], False)          # primary answers

        assert gate._primary_down_until == 0.0

    def test_a_run_already_on_the_fallback_does_not_go_back_across_a_wire(self):
        """
        The mirror of the mid-run rule. Once the fallback has shaped this
        conversation, returning to a primary speaking the other protocol is
        the same 400, arrived at from the other side.
        """
        gate = gateway(target(provider="broken"),
                       target(mode="api", provider="otherwire",
                              endpoint="http://localhost:8000/v1",
                              model="other"),
                       fallback_enabled=True)
        gate.chat("m", [{"role": "user", "content": "q"}], [], False)
        assert gate.active is gate.fallback

        gate._primary_down_until = 0.0
        started = [{"role": "assistant", "content": ""},
                   {"role": "tool", "tool_call_id": "1", "content": "{}"}]

        assert gate.chat("m", started, [], False).content == "ok"
        assert gate.active is gate.fallback

    def test_a_failed_attempt_does_not_become_the_active_provider(self):
        gate = gateway(target(provider="broken"))

        with pytest.raises(ConnectionError):
            gate.chat("m", [], [], False)

        assert gate.active is gate.primary


class TestTelemetry:
    def test_a_call_is_counted_by_mode_provider_and_model(self):
        gateway().chat("stub-model", [], [], False)

        assert telemetry.INFERENCE_REQUESTS.values[
            ("local", "recorder", "stub-model", "ok")] == 1

    def test_a_failure_is_counted_and_still_timed(self):
        """
        A call that spent its whole timeout is the single most useful latency
        observation an operator has. Recording duration only on success drops
        exactly that one.
        """
        gate = gateway(target(provider="broken"))

        with pytest.raises(ConnectionError):
            gate.chat("m", [], [], False)

        assert telemetry.INFERENCE_REQUESTS.values[
            ("local", "broken", "m", "unavailable")] == 1
        assert telemetry.INFERENCE_DURATION.values

    def test_tokens_are_recorded_when_the_provider_reports_them(self):
        gateway().chat("stub-model", [], [], False)

        assert telemetry.INFERENCE_TOKENS.values[
            ("local", "recorder", "stub-model", "prompt")] == 11
        assert telemetry.INFERENCE_TOKENS.values[
            ("local", "recorder", "stub-model", "completion")] == 7

    def test_a_provider_reporting_no_tokens_produces_no_series(self):
        """
        Absent, not zero. A zero reads as "this call used no tokens", which is
        a different and false claim from "this provider does not say".
        """
        class Quiet(Recorder):
            name = "quiet"

            def chat(self, model, messages, tools, think):
                return backends.Reply("ok", [], think, {}, None)

        backends.register("quiet", Quiet)
        try:
            gateway(target(provider="quiet")).chat("m", [], [], False)
            assert telemetry.INFERENCE_TOKENS.values == {}
        finally:
            backends._BACKENDS.pop("quiet", None)

    def test_the_rendered_output_never_carries_an_endpoint(self):
        gateway(target(endpoint="http://user:hunter2@ollama:11434")).chat(
            "m", [], [], False)

        assert "hunter2" not in telemetry.render()
        assert "ollama:11434" not in telemetry.render()


class TestNothingSecretIsEverLogged:
    """
    The whole inference path, run with a credential in every place one can
    hide, with the log captured.

    Written as one sweep rather than per-call-site because that is how this
    kind of leak happens: not from the line someone reviewed, but from the
    exception message a provider returned, quoting the request that carried
    the key.
    """

    SECRETS = ("sk-live-do-not-log", "hunter2ssss", "tok-in-the-query")
    ENDPOINT = ("https://user:hunter2ssss@api.example.com/v1"
                "?api-key=tok-in-the-query")

    @staticmethod
    def _emitted(caplog):
        """
        What the process would actually write, not caplog.text.

        This distinction is the test. `caplog.text` renders only the formatted
        message; everything passed through `extra=` is invisible in it -- and
        `extra=` is precisely where structured logging puts the fields, so a
        secret in one would pass an assertion against caplog.text while being
        written to stdout in production. Rendering each record through the
        formatter that ships is the only version of this check that means
        anything.
        """
        formatter = observability.JsonFormatter()
        return "\n".join(formatter.format(record) for record in caplog.records)

    def _leaks(self, caplog):
        emitted = self._emitted(caplog)
        return [s for s in self.SECRETS if s in emitted]

    def test_configuring_a_gateway_logs_no_credential(self, caplog):
        with caplog.at_level("DEBUG"):
            gateway(target(mode="api", provider="recorder",
                           endpoint=self.ENDPOINT, api_key="sk-live-do-not-log"),
                    allow_external=True)

        assert self._leaks(caplog) == []
        # And the line that IS logged still says the useful things.
        emitted = self._emitted(caplog)
        assert "inference_configured" in emitted
        assert '"mode": "api"' in emitted and '"provider": "recorder"' in emitted

    def test_a_refused_egress_logs_no_credential(self, caplog):
        gate = gateway(target(mode="api", provider="recorder",
                              endpoint=self.ENDPOINT,
                              api_key="sk-live-do-not-log"),
                       allow_external=True)
        gate.config.policy.allow_external = False

        with caplog.at_level("DEBUG"):
            with pytest.raises(PermissionError) as caught:
                gate.chat("m", [], [], False)

        assert self._leaks(caplog) == []
        # The exception reaches an HTTP response body, so it counts as a log.
        assert not [s for s in self.SECRETS if s in str(caught.value)]

    def test_a_failover_logs_the_error_class_and_not_its_message(self, caplog):
        """
        A provider's error text can quote the request, and the request is the
        evidence. So the class is logged and the message is not -- which also
        means a 401 from a hosted API cannot echo the key back into your logs.
        """
        Broken.raises = ConnectionError(
            "POST https://api.example.com/v1 failed: key sk-live-do-not-log")
        try:
            gate = gateway(
                target(mode="api", provider="broken", endpoint=self.ENDPOINT,
                       api_key="sk-live-do-not-log"),
                target(provider="recorder"),
                allow_external=True, fallback_enabled=True)
            with caplog.at_level("DEBUG"):
                gate.chat("m", [], [], False)
        finally:
            Broken.raises = ConnectionError("connection refused")

        assert self._leaks(caplog) == []
        emitted = self._emitted(caplog)
        assert "inference_fallback" in emitted
        assert '"reason": "ConnectionError"' in emitted
        # The class, and none of the message that carried it.
        assert "api.example.com" not in emitted

    def test_a_failed_probe_reports_the_class_and_not_its_message(self):
        """
        probe() feeds /readyz, which is unauthenticated. Anything it returns
        is public to whoever can reach the Service.
        """
        Broken.raises = ConnectionError("bad key sk-live-do-not-log")
        try:
            gate = gateway(target(mode="api", provider="broken",
                                  endpoint=self.ENDPOINT,
                                  api_key="sk-live-do-not-log"),
                           allow_external=True)
            report = gate.probe()
        finally:
            Broken.raises = ConnectionError("connection refused")

        rendered = json.dumps(report)
        assert not [s for s in self.SECRETS if s in rendered]
        assert "api.example.com" not in rendered

    def test_metrics_carry_no_credential_after_a_full_exercise(self):
        gate = gateway(target(mode="api", provider="recorder",
                              endpoint=self.ENDPOINT,
                              api_key="sk-live-do-not-log"),
                       allow_external=True)
        gate.chat("m", [{"role": "user", "content": "q"}], [], False)

        rendered = telemetry.render()
        assert not [s for s in self.SECRETS if s in rendered]
        assert "api.example.com" not in rendered


class TestTheLoopCannotTell:
    """
    The success criterion, stated as a test.

    The gateway presents exactly the interface a backend does. If this ever
    fails, agent.py has to learn something about inference, which is the thing
    the seam exists to prevent.
    """

    def test_it_presents_every_method_the_loop_calls(self):
        gate = gateway()
        raw = backends.get("ollama")

        for method in ("chat", "assistant_message", "tool_message", "tools",
                       "name"):
            assert hasattr(gate, method), method
            assert hasattr(raw, method), method

    def test_the_agent_loop_resolves_one(self):
        import agent

        agent._BACKEND = None
        inference.reset()
        try:
            assert isinstance(agent._backend(), inference.Gateway)
        finally:
            agent._BACKEND = None
            inference.reset()


class TestRedactionAtTheBoundary:
    def test_a_provider_object_is_copied_rather_than_mutated(self):
        """
        Ollama hands back its own pydantic Message and the loop appends it to
        the history. Rewriting that object in place would edit the
        conversation the primary is still holding.
        """
        class Message:
            def __init__(self, content):
                self.content = content
                self.role = "assistant"

            def model_copy(self, update):
                return Message(update["content"])

        original = Message("DB_PASSWORD=hunter2ssss")
        out = inference._redacted([original])

        assert out[0] is not original
        assert original.content == "DB_PASSWORD=hunter2ssss"
        assert "hunter2ssss" not in out[0].content

    def test_a_message_it_cannot_rewrite_is_passed_through_loudly(self, caplog):
        """
        Silently not redacting is the one outcome this must never do quietly.
        """
        class Opaque:
            role = "assistant"
            content = object()

        with caplog.at_level("WARNING"):
            out = inference._redacted([Opaque()])

        assert len(out) == 1
        assert "egress_redaction_skipped" in caplog.text

    def test_it_uses_the_same_filter_the_collectors_use(self):
        """
        One filter, not two. A boundary pass with its own pattern list would
        drift from the collection pass, and the shapes it stopped catching
        would be exactly the ones nobody was watching.
        """
        leak = "postgres://app:hunter2@db:5432/x"

        assert inference._redacted([{"content": leak}])[0]["content"] == \
            redaction.redact(leak)
