"""
The inference gateway: where inference happens, and whether evidence may go there.

`backends.py` answers "which protocol". This answers the three questions above
it, which are the ones an organisation actually argues about:

- **Where does the model run?** On the laptop, inside the cluster, or at a
  vendor. `TRIAGE_INFERENCE_MODE` is local | cluster | api.
- **May this evidence leave the network?** Pod logs are the highest-value
  input this agent has and the highest-risk payload it holds. Default: no.
- **What happens when the model is down?** Nothing, unless someone said
  otherwise. Default: no fallback.

The agent loop is unchanged and knows none of it. Gateway implements the same
four methods a backend does -- `chat`, `assistant_message`, `tool_message`,
`tools` -- so `agent._backend()` returns one of these instead of a raw backend
and the loop cannot tell.

**The mode is not decoration, and this is the part worth reading.** It would
be easy to make `mode` a label that selects an endpoint and means nothing
else. Then `mode: cluster` with an endpoint pointing at api.openai.com would
install cleanly, report itself as in-cluster inference in every log line, and
ship pod logs to a vendor. So the mode is *checked against the endpoint*: a
mode claiming inference stays on-network is refused at construction if its
endpoint is off-network.

The asymmetry is deliberate. `api` mode pointed at a local endpoint is
allowed, because that is how the OpenAI-protocol backend gets validated
without a key or a bill -- claiming more egress than occurs is never the
unsafe direction.

**Classification is on the name as written, and never resolves it.** A DNS
lookup would give an answer that can change between the check and the request,
and a policy decision that depends on the current answer cannot be audited
after the fact. The consequence is stated rather than hidden: a public name
that resolves to a private address reads as external here, and a private
name that resolves publicly reads as internal. The first is a false alarm the
operator can override; the second is the real limitation, and it is why
`allow_external` is a separate switch rather than something inferred.

**Failover is a wire-protocol problem before it is an availability problem.**
Halfway through a run the message history is in the primary's shape -- Ollama
Message objects with results matched by `tool_name`, or dicts matched by
`tool_call_id`. Handing that history to a provider speaking the other protocol
is a 400 at best. So a mid-run failover is permitted only between backends
that share a wire; across wires, the fallback can only take a run that has not
started. That is a narrower guarantee than "we fail over", and it is the true
one.
"""

import ipaddress
import logging
import os
import re
import socket
import time
import urllib.parse

import httpx

import backends
import redaction
import telemetry

log = logging.getLogger("triage.inference")

MODES = ("local", "cluster", "api")

# Which provider a mode means when nobody says. Ollama for both on-network
# modes because it is the one this project has measured; OpenAI for api mode
# because that is the protocol the hosted services speak.
DEFAULT_PROVIDER = {"local": "ollama", "cluster": "ollama", "api": "openai"}

# Where a mode looks when nobody says. The cluster default names a Service in
# an `ollama` namespace rather than the release namespace, matching the chart's
# model.ollamaHost -- the two have to agree or the default is a broken address
# in one of the two places it appears.
DEFAULT_ENDPOINT = {
    "local": "http://localhost:11434",
    "cluster": "http://ollama.ollama.svc.cluster.local:11434",
    "api": "https://api.openai.com/v1",
}

# Modes whose whole claim is that inference stays on your network. An endpoint
# that leaves it contradicts the mode, and is refused rather than logged.
ON_NETWORK_MODES = ("local", "cluster")

# How long the gateway stops trying a primary that just failed, in seconds.
#
# Measured live on 2026-08-23 and this number exists because of it: with a dead
# primary and a working fallback, a run failed over on *every round*, because
# nothing remembered that the primary had just refused. A refused connection
# costs milliseconds and made that invisible. A primary that times out instead
# of refusing costs OLLAMA_TIMEOUT -- 300s by default -- and MAX_ROUNDS is 8,
# so the same run would have spent forty minutes discovering eight times that
# the model was still down.
#
# Sixty seconds: long enough that one investigation never pays the probe twice,
# short enough that a primary coming back is picked up within a minute rather
# than needing a restart.
PRIMARY_RETRY_SECONDS = int(os.getenv("TRIAGE_PRIMARY_RETRY_SECONDS", "60"))

# Hostnames that are this machine or this cluster, whatever DNS thinks.
# host.docker.internal is here because it is the documented way a container in
# this project's own docker-compose reaches the Ollama on the host, and reading
# it as external would refuse the developer default.
_LOCAL_HOSTS = frozenset({
    "localhost", "ip6-localhost", "ip6-loopback", "host.docker.internal",
    "kubernetes.default", "host.containers.internal",
})

# Suffixes the cluster resolves internally. `.local` covers both mDNS and the
# tail of `.cluster.local`; a cluster configured with a non-default DNS domain
# needs its suffix added, and that is what INTERNAL_SUFFIXES being a module
# constant is for.
INTERNAL_SUFFIXES = (".svc", ".svc.cluster.local", ".cluster.local", ".local",
                     ".internal", ".localdomain")

# A syntactically valid DNS label, which is what a single-label host has to be
# before it may be read as a Service name. Without this, any unparseable
# garbage with no dot in it -- "not a url", a lone NUL byte -- took the
# single-label branch and was classified internal, which is the opposite of
# what this module documents and of what is safe.
_DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def destination(endpoint):
    """
    "internal" or "external" for an endpoint, decided on the name as written.

    Internal means loopback, a private or link-local address, a Kubernetes
    Service name, or a single-label host -- a bare `ollama` is a Service in
    this namespace or a compose service, never a public host, because a public
    host needs a dot.

    Everything else is external, including anything unparseable. An endpoint
    this cannot read is not a reason to assume the safe answer: the whole
    point of the check is to be wrong in the direction that refuses.
    """
    host = _host(endpoint)
    if not host:
        return "external"

    if host in _LOCAL_HOSTS:
        return "internal"

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "internal" if (
            address.is_private or address.is_loopback or address.is_link_local
        ) else "external"

    # An integer-form IPv4, which the resolver accepts and a human does not
    # read as an address at all. inet_aton takes "0x08080808" and "134744072"
    # -- both of which are 8.8.8.8 -- and the shorthand "127.1". Before this
    # check the first two took the single-label branch below and were
    # classified internal while getaddrinfo returned a public address.
    # Measured 2026-08-23; see the test of the same name.
    numeric = _numeric_address(host)
    if numeric is not None:
        return "internal" if (
            numeric.is_private or numeric.is_loopback or numeric.is_link_local
        ) else "external"

    if "." not in host:
        # A single label cannot be a public name: in a cluster it is a Service
        # in this namespace, in compose a service alias. Only when it is
        # actually a label, though -- anything else reaching here is a string
        # this could not parse, and those are refused rather than trusted.
        return "internal" if _DNS_LABEL.match(host) else "external"

    if any(host.endswith(suffix) for suffix in INTERNAL_SUFFIXES):
        return "internal"

    return "external"


def _numeric_address(host):
    """
    The address an integer-form host denotes, or None if it is not one.

    `inet_aton` is what the C resolver ultimately uses, and it accepts forms
    that contain no dot and look nothing like an address: hexadecimal,
    decimal, and dotted shorthand. A classifier that only understands the
    presentation format sees a name; the resolver sees 8.8.8.8.
    """
    try:
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
    except (OSError, ValueError):
        return None


def _host(endpoint):
    """
    The hostname, with the brackets stripped off an IPv6 literal.

    A bare `ollama:11434` with no scheme parses as scheme `ollama` and path
    `11434`, giving an empty hostname -- which would then read as external and
    refuse a perfectly ordinary Service address. So a string with no `//` gets
    one before parsing.
    """
    if not endpoint:
        return ""
    text = endpoint.strip()
    if "//" not in text:
        text = "//" + text
    try:
        parsed = urllib.parse.urlsplit(text)
        host = (parsed.hostname or "").strip("[]").rstrip(".").lower()
    except ValueError:
        return ""
    if not host:
        return ""

    # An IP literal is already canonical: IDNA does not apply to one, and
    # handing a bracket-stripped IPv6 literal to the normaliser below rejects
    # it outright -- which classified ::1, fd00::1 and fe80::1 as external the
    # first time this fix was written.
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    # Normalise through the same parser the HTTP clients use, so the classifier
    # and the request agree *by construction* rather than by coincidence. Both
    # backends reach the network through httpx, and httpx applies IDNA -- which
    # maps U+3002, U+FF0E and U+FF61 onto "." . Before this, `evil<U+3002>com`
    # contained no ASCII dot, took the single-label branch, was classified
    # internal, and was then dialled by httpx as `evil.com`. Measured
    # 2026-08-23 with a captured request carrying pod-log secrets.
    try:
        host = str(httpx.URL(f"http://{host}").host)
    except Exception:
        # A host httpx will not accept is one no request can be made to, and
        # refusing is the safe direction. See the module docstring.
        return ""
    return host.strip("[]").rstrip(".").lower()


class Target:
    """One place inference can happen: mode, provider, endpoint, model."""

    __slots__ = ("mode", "provider", "endpoint", "model", "api_key", "timeout")

    def __init__(self, mode, provider=None, endpoint=None, model=None,
                 api_key="", timeout=None):
        if mode not in MODES:
            raise ValueError(
                f"unknown inference mode {mode!r}; expected one of "
                f"{', '.join(MODES)}"
            )
        self.mode = mode
        self.provider = provider or DEFAULT_PROVIDER[mode]
        self.endpoint = endpoint or DEFAULT_ENDPOINT[mode]
        self.model = model
        self.api_key = api_key or ""
        self.timeout = timeout

        if self.provider not in backends._BACKENDS:
            raise ValueError(
                f"unknown inference provider {self.provider!r}; available: "
                f"{', '.join(sorted(backends._BACKENDS))}"
            )

    @property
    def destination(self):
        return destination(self.endpoint)

    @property
    def external(self):
        return self.destination == "external"

    def build(self):
        return backends.get(self.provider, endpoint=self.endpoint,
                            api_key=self.api_key or None, timeout=self.timeout)

    def describe(self):
        """
        The safe fields, for a log line or an error message.

        The endpoint is not among them and neither is the key. An endpoint can
        carry a token in its userinfo or its query string, and this project
        redacts pod logs precisely so that credentials do not reach a
        scrollback buffer -- putting one in its own startup log would be a
        strange place to stop caring.
        """
        return {"mode": self.mode, "provider": self.provider,
                "model": self.model, "destination": self.destination}

    def __repr__(self):
        return (f"Target(mode={self.mode!r}, provider={self.provider!r}, "
                f"host={_host(self.endpoint)!r}, model={self.model!r})")


class Policy:
    """
    What may leave the network, and what may be tried when the model is down.

    Both default to off. That is not caution for its own sake: this project's
    README claims that nothing leaves your network, and a default that made
    that false for anyone who upgraded without reading a changelog would be a
    lie told by omission.
    """

    __slots__ = ("allow_external", "fallback_enabled", "redact_on_egress")

    def __init__(self, allow_external=False, fallback_enabled=False,
                 redact_on_egress=True):
        self.allow_external = allow_external
        self.fallback_enabled = fallback_enabled
        self.redact_on_egress = redact_on_egress


class Config:
    """A primary, an optional fallback, and the policy governing both."""

    __slots__ = ("primary", "fallback", "policy")

    def __init__(self, primary, fallback=None, policy=None):
        self.primary = primary
        self.policy = policy or Policy()
        # A fallback that was configured but never enabled is dropped here
        # rather than carried and skipped later, so that "is there a fallback?"
        # has one answer and every reader gets the same one.
        self.fallback = fallback if (fallback and
                                     self.policy.fallback_enabled) else None
        self.validate()

    def validate(self):
        """
        Refuse a configuration that would send evidence somewhere it may not go.

        At construction rather than at the first request. A gateway that
        accepts an illegal configuration and fails when a diagnosis is
        finally asked for has moved a deployment error into an incident.
        """
        for role, target in (("primary", self.primary),
                             ("fallback", self.fallback)):
            if target is None:
                continue

            if target.mode in ON_NETWORK_MODES and target.external:
                raise ValueError(
                    f"{role} inference mode {target.mode!r} says inference "
                    f"stays on your network, but its endpoint host "
                    f"{_host(target.endpoint)!r} is off it. Either point it "
                    f"at an in-cluster or local address, or set the mode to "
                    f"'api' and say so."
                )

            if target.external and not self.policy.allow_external:
                raise ValueError(
                    f"{role} inference would send cluster evidence to "
                    f"{_host(target.endpoint)!r}, which is off your network, "
                    f"and TRIAGE_ALLOW_EXTERNAL_INFERENCE is not set. Pod "
                    f"logs are the input this agent depends on and the "
                    f"payload it holds; sending them to a vendor is a "
                    f"decision, so it has to be made explicitly."
                )

        if self.fallback and self.fallback.model is None:
            # Inheriting the model would be convenient and wrong: the fallback
            # is a different provider serving a different catalogue, and
            # "qwen3" is not a model name a hosted API knows.
            raise ValueError(
                "a fallback needs its own model name. It is a different "
                "provider serving a different catalogue, and inheriting the "
                "primary's model would produce a 404 at the worst possible "
                "moment -- when the primary is already down."
            )

    def describe(self):
        return {
            "primary": self.primary.describe(),
            "fallback": self.fallback.describe() if self.fallback else None,
            "allow_external": self.policy.allow_external,
            "redact_on_egress": self.policy.redact_on_egress,
        }


def _flag(env, name, default=False):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def from_env(env=None):
    """
    The configuration, from environment variables.

    **TRIAGE_BACKEND still works and still means what it meant.** It named a
    protocol, which is a narrower thing than a mode, so it is read as one:
    `ollama` is local inference, `openai` is api inference, `vllm` is
    in-cluster. Anyone who set it gets the behaviour they had. Setting
    TRIAGE_INFERENCE_MODE takes precedence, because it is the more specific
    statement.

    **OLLAMA_HOST still works too**, and that matters more than it looks: the
    Helm chart sets it and nothing else, so a cluster running the published
    chart configures its endpoint entirely through that variable. Reading it
    here rather than letting the ollama client pick it up quietly is what lets
    the egress check see the address at all.
    """
    env = os.environ if env is None else env

    mode = (env.get("TRIAGE_INFERENCE_MODE") or "").strip().lower()
    provider = (env.get("TRIAGE_INFERENCE_PROVIDER") or "").strip().lower()

    if not mode:
        legacy = (env.get("TRIAGE_BACKEND") or "").strip().lower()
        mode = {"ollama": "local", "openai": "api", "vllm": "cluster"}.get(
            legacy, "local")
        provider = provider or legacy

    if mode not in MODES:
        raise ValueError(
            f"unknown TRIAGE_INFERENCE_MODE {mode!r}; expected one of "
            f"{', '.join(MODES)}"
        )

    provider = provider or DEFAULT_PROVIDER[mode]

    endpoint = (env.get("TRIAGE_INFERENCE_ENDPOINT") or "").strip()
    if not endpoint:
        # Per-provider legacy variables, each still the documented way to
        # point that provider somewhere.
        endpoint = (env.get("OLLAMA_HOST") if provider == "ollama"
                    else env.get("TRIAGE_OPENAI_BASE_URL")) or ""
    endpoint = endpoint.strip() or DEFAULT_ENDPOINT[mode]

    policy = Policy(
        allow_external=_flag(env, "TRIAGE_ALLOW_EXTERNAL_INFERENCE"),
        fallback_enabled=_flag(env, "TRIAGE_FALLBACK_ENABLED"),
        redact_on_egress=_flag(env, "TRIAGE_REDACT_ON_EGRESS", default=True),
    )

    primary = Target(
        mode=mode,
        provider=provider,
        endpoint=endpoint,
        model=(env.get("TRIAGE_MODEL") or "qwen3").strip(),
        api_key=env.get("OPENAI_API_KEY", ""),
        timeout=_int(env.get("OLLAMA_TIMEOUT")),
    )

    fallback = None
    fallback_mode = (env.get("TRIAGE_FALLBACK_MODE") or "").strip().lower()
    if fallback_mode:
        fallback_provider = (
            env.get("TRIAGE_FALLBACK_PROVIDER") or "").strip().lower() or None
        fallback = Target(
            mode=fallback_mode,
            provider=fallback_provider,
            endpoint=(env.get("TRIAGE_FALLBACK_ENDPOINT") or "").strip() or None,
            model=(env.get("TRIAGE_FALLBACK_MODEL") or "").strip() or None,
            api_key=env.get("TRIAGE_FALLBACK_API_KEY")
                    or env.get("OPENAI_API_KEY", ""),
            timeout=_int(env.get("OLLAMA_TIMEOUT")),
        )

    return Config(primary, fallback, policy)


def _int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def unavailable(exc):
    """
    Whether the provider could not answer, as opposed to refused to.

    The distinction decides whether the fallback is tried, and getting it
    backwards is expensive in both directions. A timeout, a refused
    connection, a 502 or a 429 are the provider being unable, and are what a
    fallback exists for. A 400 is a malformed request and a 401 is a wrong
    key: both will fail identically on the fallback, and quietly succeeding
    somewhere else would hide a configuration error that an operator has to
    see. So those are not failed over, they are raised.
    """
    import httpx
    import ollama

    if isinstance(exc, backends.MalformedResponse):
        # A body that is not this protocol is nearly always an intermediary
        # rather than the model: an HTML error page from a proxy, a truncated
        # response, an empty `choices` array. That is an availability failure,
        # which is what a fallback is for -- and before 2026-08-23 every one of
        # them raised a bare JSONDecodeError or IndexError that read as "the
        # provider refused" and never failed over.
        return True
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError,
                        httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    if isinstance(exc, ollama.ResponseError):
        status = getattr(exc, "status_code", 0) or 0
        return status >= 500 or status == 429 or status == 0
    # ConnectionError covers the ollama client's own socket failures, and
    # OSError catches DNS resolution failing before a request is even made.
    return isinstance(exc, (ConnectionError, OSError))


def _redacted(messages):
    """
    Every message with its text run through the secret filter, once more.

    Pod logs are already redacted where they are collected, so this is a
    second pass at the network boundary rather than the only one. It is here
    because the boundary is the place where being wrong is unrecoverable: a
    credential in a scrollback buffer is a bad afternoon, and the same
    credential in a vendor's request log is an incident with someone else's
    retention policy attached.

    Messages arrive as plain dicts on every wire that can reach an external
    destination. A provider object is copied rather than mutated where it
    supports it, and passed through untouched where it does not -- with a
    warning, because silently not redacting is the one outcome this must never
    do quietly.
    """
    out = []
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                out.append({**message, "content": redaction.redact(content)})
            else:
                out.append(message)
            continue

        content = getattr(message, "content", None)
        copy = getattr(message, "model_copy", None)
        if isinstance(content, str) and callable(copy):
            out.append(copy(update={"content": redaction.redact(content)}))
            continue

        log.warning(
            "egress_redaction_skipped",
            extra={"message_type": type(message).__name__},
        )
        out.append(message)
    return out


def _started(messages):
    """
    Whether this conversation already carries turns in a provider's own shape.

    Only assistant and tool messages are wire-shaped; system and user messages
    are the same dict on every protocol. So a run that has called nothing can
    move to a provider speaking a different wire, and a run that has cannot.
    """
    return any(
        (message.get("role") if isinstance(message, dict)
         else getattr(message, "role", None)) in ("assistant", "tool")
        for message in messages
    )


class Gateway:
    """
    The seam the agent loop actually holds.

    Presents a backend's four methods, adds three things behind them: the
    egress decision, the fallback, and the telemetry. The loop is unchanged
    and none of this is visible from it.
    """

    def __init__(self, config):
        self.config = config
        self.primary = config.primary
        self.fallback = config.fallback
        self._backends = {}
        # Which target answered the most recent chat(). assistant_message()
        # and tool_message() have to be shaped by *that* provider rather than
        # by the configured primary, or a run that failed over would build its
        # next message in the wrong protocol -- which is a 400 from the
        # provider that just rescued it.
        self.active = config.primary
        # Monotonic deadline before which the primary is not tried again. Zero
        # means "try it". See PRIMARY_RETRY_SECONDS.
        self._primary_down_until = 0.0
        log.info("inference_configured", extra=config.describe())

    # -- the backend interface ---------------------------------------------

    @property
    def name(self):
        return self.active.provider

    def tools(self, registry):
        return self._backend(self.active).tools(registry)

    def assistant_message(self, reply):
        return self._backend(self.active).assistant_message(reply)

    def tool_message(self, call, output):
        return self._backend(self.active).tool_message(call, output)

    def chat(self, model, messages, tools, think):
        """
        One model call, on the primary if it can answer and the fallback if not.

        The caller's `model` overrides the primary's configured one -- the CLI
        can ask for a different model and the eval harness does -- but it is
        never passed to the fallback. The fallback is a different provider
        serving a different catalogue, and calling it with the primary's model
        name is a 404 at the one moment the primary is already down.
        """
        first = self._preferred(messages)
        try:
            return self._attempt(
                first,
                model or first.model if first is self.primary else first.model,
                messages, tools, think)
        except Exception as exc:
            # Already on the fallback: there is nowhere further to go, and
            # trying the primary now would be a failover in the wrong
            # direction into a conversation the fallback has been shaping.
            if first is not self.primary or not self._may_fail_over(
                    exc, messages):
                raise
            self._primary_down_until = time.monotonic() + PRIMARY_RETRY_SECONDS
            telemetry.FALLBACKS.inc(
                from_provider=self.primary.provider,
                to_provider=self.fallback.provider,
                reason=type(exc).__name__,
            )
            log.warning(
                "inference_fallback",
                extra={
                    "from_provider": self.primary.provider,
                    "from_mode": self.primary.mode,
                    "to_provider": self.fallback.provider,
                    "to_mode": self.fallback.mode,
                    "to_destination": self.fallback.destination,
                    # The class, never the message. A provider's error text
                    # can quote the request, and the request is the evidence.
                    "reason": type(exc).__name__,
                },
            )
            return self._attempt(self.fallback, self.fallback.model,
                                 messages, tools, think)

    def probe(self):
        """
        Whether inference is available, and where from.

        Reports the primary and the fallback separately rather than reducing
        them to one boolean. "Ready, on the fallback" and "ready, on the
        primary" are different states of the world, and a readiness endpoint
        that renders them identically hides an ongoing outage behind a green
        check -- which is how a primary stays down for a week.

        Never raises. A probe is a question, and the answer "no" is data.
        """
        report = {"ready": False, "primary": None, "fallback": None}
        for role, target in (("primary", self.primary),
                             ("fallback", self.fallback)):
            if target is None:
                continue
            entry = dict(target.describe())
            try:
                probed = self._backend(target).probe(model=target.model) or {}
            except Exception as exc:
                entry["ready"] = False
                # The class, never the message: a provider's error text can
                # quote the request, and the request is the evidence.
                entry["error"] = type(exc).__name__
            else:
                # Reachable is not the same as able to answer. A provider that
                # lists its models and does not list this one will 404 every
                # request -- which this project shipped once, as a pod
                # reporting 1/1 Ready with an empty model directory.
                check = probed.get("model_check", "unsupported")
                entry["model_check"] = check
                entry["models_listed"] = probed.get("models_listed", 0)
                entry["ready"] = check != "absent"
                if check == "absent":
                    entry["error"] = "model_not_served"
                else:
                    report["ready"] = True
            report[role] = entry
        return report

    # -- internals ----------------------------------------------------------

    def _attempt(self, target, model, messages, tools, think):
        outbound = messages
        if target.external:
            # Refused here as well as at construction. The construction check
            # is about configuration; this one is about the request, and a
            # policy that is only enforced at startup is a policy that a later
            # mutation can walk past.
            if not self.config.policy.allow_external:
                telemetry.EGRESS_DENIED.inc(mode=target.mode,
                                            provider=target.provider)
                raise PermissionError(
                    f"refusing to send cluster evidence to "
                    f"{_host(target.endpoint)!r}: external inference is not "
                    f"allowed by policy"
                )
            if self.config.policy.redact_on_egress:
                outbound = _redacted(messages)

        backend = self._backend(target)
        labels = {"mode": target.mode, "provider": target.provider,
                  "model": model}

        started = time.perf_counter()
        try:
            reply = backend.chat(model, outbound, tools, think)
        except Exception as exc:
            # Timed on the way out as well as the way through. A call that
            # spent 300s hitting its timeout is the single most useful latency
            # observation an operator can have, and recording it only on
            # success would drop exactly that one.
            telemetry.INFERENCE_DURATION.observe(
                time.perf_counter() - started, **labels)
            telemetry.INFERENCE_REQUESTS.inc(
                outcome="unavailable" if unavailable(exc) else "error",
                **labels)
            raise
        telemetry.INFERENCE_DURATION.observe(
            time.perf_counter() - started, **labels)
        telemetry.INFERENCE_REQUESTS.inc(outcome="ok", **labels)
        _count_tokens(reply, labels)

        # Recorded only on success: a failed attempt must not leave the
        # gateway shaping its next message in the protocol of a provider that
        # did not answer.
        self.active = target
        if target is self.primary:
            self._primary_down_until = 0.0
        return reply

    def _preferred(self, messages):
        """
        Which target to try first this round.

        Two reasons to skip the primary. It failed recently and the retry
        window has not elapsed -- see PRIMARY_RETRY_SECONDS, which exists
        because a run without it failed over on every round. Or the fallback
        has already shaped this conversation and speaks a different wire, in
        which case going back to the primary is the same 400 that
        _may_fail_over declines in the other direction.
        """
        if self.fallback is None:
            return self.primary

        if (self.active is self.fallback and _started(messages)
                and self._wire(self.primary) != self._wire(self.fallback)):
            return self.fallback

        if time.monotonic() < self._primary_down_until:
            return self.fallback

        return self.primary

    def _may_fail_over(self, exc, messages):
        if self.fallback is None:
            return False
        if isinstance(exc, PermissionError):
            # Policy said no. A fallback is not a way around policy.
            return False
        if not unavailable(exc):
            return False
        if _started(messages) and self._wire(self.primary) != self._wire(
                self.fallback):
            # The history is in the primary's shape and the fallback speaks a
            # different one. Handing it over produces a 400 that reads as the
            # fallback being broken, so the honest outcome is the original
            # failure.
            log.warning(
                "fallback_declined_mid_run",
                extra={"from_wire": self._wire(self.primary),
                       "to_wire": self._wire(self.fallback),
                       "reason": "conversation already shaped by the primary"},
            )
            return False
        return True

    def _wire(self, target):
        return getattr(self._backend(target), "wire", target.provider)

    def _backend(self, target):
        # Cached per target: building an Ollama client is cheap, but the
        # gateway holds two of these for the life of the process and there is
        # no reason to rebuild either per round.
        key = (target.provider, target.endpoint)
        if key not in self._backends:
            self._backends[key] = target.build()
        return self._backends[key]


def _count_tokens(reply, labels):
    """
    Token counts, where the provider reports them.

    Absent rather than zero when it does not: Ollama's native API reports
    prompt_eval_count and eval_count, the OpenAI protocol reports a usage
    object, and a provider reporting neither should produce no series at all.
    A zero would read as "this call used no tokens", which is a different and
    false claim.
    """
    usage = getattr(reply, "usage", None) or {}
    for kind in ("prompt", "completion"):
        value = usage.get(kind)
        if value:
            telemetry.INFERENCE_TOKENS.inc(value, kind=kind, **labels)


_GATEWAY = None


def gateway(config=None):
    """
    The configured gateway, resolved once.

    Cached like agent._backend() was, and reset by the same means: assigning
    None. A process holds one configuration.
    """
    global _GATEWAY
    if config is not None:
        _GATEWAY = Gateway(config)
    elif _GATEWAY is None:
        _GATEWAY = Gateway(from_env())
    return _GATEWAY


def reset():
    """Forget the resolved gateway. For tests and for a config reload."""
    global _GATEWAY
    _GATEWAY = None
