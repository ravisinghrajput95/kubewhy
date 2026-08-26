"""
One record per investigation: who asked what, what was collected, where
inference happened.

telemetry.py already counts things -- which provider answered, how long runs
take, how often a tool errored. Those are operator questions, asked while
something is wrong, and they are deliberately aggregate: a label taken from a
cluster object would mint a time series per pod per restart.

This answers a different question, asked afterwards and about one run: *who
read that namespace's logs, and did any of it leave the network?* A counter
cannot answer it and neither can a metrics endpoint, because the answer is
per-question and the question is the thing being audited.

## What is deliberately not in a record

**The evidence itself.** A record names the tools a run called and the
arguments it called them with; it does not carry what came back. An audit log
is shipped somewhere central, kept for a long time, and read by more people
than the console is -- writing pod logs into it would create a second copy of
the most sensitive asset this project handles, under weaker controls than the
original, in the name of protecting it.

**The inference endpoint.** For the reason telemetry.py gives: an endpoint can
carry a token in its userinfo or its query string. The mode, the provider and
whether the destination was on-network are the parts an auditor can act on.

**The answer.** For the same reason as the evidence: it quotes it.

## The one field that must never be wrong

`evidence_left_network`. It is derived from policy rather than observed,
because policy is provable and observation is not. `inference.Gateway` holds
one `active` target for the whole process, so reading it at the end of a run
tells you where *some* run went -- fine with one investigation in flight and
wrong with two. Whereas `allow_external=False` makes egress impossible: the
gateway raises PermissionError in `_attempt` before any request is built. So:

- policy forbids external inference        -> `false`, and that is a proof
- policy allows it and every configured
  destination is external                  -> `true`, also a proof
- policy allows it and the destinations
  are mixed                                -> `"possible"`

Three values rather than a boolean that is sometimes a guess. An auditor can
act on "possible"; they cannot act on a `false` that meant "probably not".
"""

import contextvars
import json
import logging
import os
import time

import redaction

log = logging.getLogger("triage.audit")

# Off is a deliberate choice someone has to make, not the default. A tool that
# reads pod logs should record that it did.
_DISABLED = ("0", "false", "no", "off")

# A question is free text and a person can paste anything into it, including a
# credential they were debugging. Capped and redacted rather than trusted.
_MAX_QUESTION = 2000

# Which tools read something a person would want an audit trail of. Not all of
# them: list_pods returns names and phases, get_pod_logs returns whatever the
# application wrote, and conflating the two makes the trail useless for the
# question it exists to answer.
SENSITIVE_TOOLS = frozenset({"get_pod_logs"})

# What the person typed, when a surface wrapped it in scaffolding before
# handing it to the loop. See asked().
_ASKED = contextvars.ContextVar("audit_asked", default="")

_PRINCIPAL = contextvars.ContextVar("audit_principal", default="anonymous")
_AUTH = contextvars.ContextVar("audit_auth", default="anonymous")
_SURFACE = contextvars.ContextVar("audit_surface", default="unknown")


def enabled():
    return (os.getenv("TRIAGE_AUDIT", "1") or "").strip().lower() not in _DISABLED


def actor(principal=None, surface=None, auth=None):
    """
    Who is asking, and through what, for investigations on this context.

    A ContextVar rather than an argument on `stream()` because five surfaces
    reach the loop and four of them would have to grow a parameter they then
    pass through unchanged. Same mechanism `use_context` already uses for the
    caller's cluster, and the same warning applies: a ContextVar belongs to
    the context that set it. Streamlit reruns its script on a fresh thread and
    `threading.Thread` does not copy context, so a surface that hands work to
    another thread must set this on that thread rather than before it starts.

    `principal` takes an identity.Principal or a plain string -- the surfaces
    without an authenticated user still have an actor worth recording, and a
    record whose principal field is absent reads as "nobody asked", which is
    never what happened.
    """
    if principal is not None:
        label = getattr(principal, "label", None)
        _PRINCIPAL.set(label() if callable(label) else str(principal))
        _AUTH.set(auth or getattr(principal, "source", None) or "unknown")
    elif auth is not None:
        _AUTH.set(auth)
    if surface is not None:
        _SURFACE.set(surface)


def asked(question):
    """
    The question as a person typed it, before a surface scaffolded it.

    `agent.scoped_question()` wraps a question in four sentences of direction
    -- naming the workload, naming the first tool to call, forbidding the
    others -- and the loop only ever sees the result. Auditing that is
    auditing the prompt engineering: measured on a real console run, the field
    was 430 characters of boilerplate with "why is demo/nightly-sync failing?"
    at the end of it. "Who asked what" means the what a person would recognise.

    Consumed by the next Record rather than left set, so a scoped_question()
    whose investigation never ran cannot label the following one.
    """
    _ASKED.set(question or "")


def _take_asked():
    question = _ASKED.get()
    _ASKED.set("")
    return question


def current():
    """The actor as it stands, for a caller that wants to log it itself."""
    return {"principal": _PRINCIPAL.get(), "auth": _AUTH.get(),
            "surface": _SURFACE.get()}


def _egress(config):
    """
    Whether cluster evidence could have left the network. See the module
    docstring for why this is three-valued and derived rather than observed.
    """
    if not config.policy.allow_external:
        return False

    targets = [config.primary] + ([config.fallback] if config.fallback else [])
    if all(target.external for target in targets):
        return True
    if not any(target.external for target in targets):
        # Permission granted and unused: nothing configured points off-network.
        return False
    return "possible"


def _inference():
    """
    Where inference was configured to happen, and whether evidence may leave.

    Configuration rather than observation, and it reads the gateway's config
    rather than the environment so that a process configured programmatically
    -- the eval harness does -- is audited as what it is running, not as what
    its environment variables say.
    """
    try:
        import inference

        config = inference.gateway().config
    except Exception as exc:
        # Errors are data here as everywhere else in this project. An audit
        # record that raises takes the investigation down with it, and a
        # missing record is a smaller problem than a missing diagnosis --
        # provided the record says it is missing rather than omitting the
        # field and reading as "no inference happened".
        return {"error": type(exc).__name__}

    described = dict(config.primary.describe())
    described["fallback"] = (config.fallback.describe()
                             if config.fallback else None)
    described["allow_external"] = config.policy.allow_external
    described["evidence_left_network"] = _egress(config)
    return described


class Record:
    """
    One investigation, accumulated as it runs and emitted exactly once.

    Accumulated rather than built at the end because a run that raises, or one
    whose caller walks away mid-stream, is precisely the run worth having a
    record of -- and at that point there is no answer event to build one from.
    """

    __slots__ = ("run_id", "question", "prompted", "model", "started", "target",
                 "tools", "namespaces", "sensitive", "verdict", "termination",
                 "outcome", "error", "rounds", "_emitted")

    def __init__(self, question, model):
        self.run_id = ""
        asked_directly = _take_asked()
        # `prompted` records that the loop was handed something other than
        # what the person typed -- not the scaffolding itself, which is
        # generated, identical every time and would triple the size of every
        # record for a string that is in scoped_question().
        self.prompted = bool(asked_directly and asked_directly != question)
        self.question = redaction.redact(
            (asked_directly or question or "")[:_MAX_QUESTION])
        self.model = model
        self.started = time.perf_counter()
        self.target = None
        self.tools = []
        self.namespaces = set()
        self.sensitive = []
        self.verdict = ""
        self.termination = None
        self.outcome = "incomplete"
        self.error = ""
        self.rounds = 0
        self._emitted = False

    def observe(self, event):
        """One event from the loop. Never stores a tool's output."""
        kind = event.get("type")
        self.run_id = event.get("run_id") or self.run_id

        if kind == "tool_call":
            name = event.get("name", "")
            arguments = event.get("arguments") or {}
            self.tools.append({"tool": name, "arguments": arguments})
            namespace = arguments.get("namespace")
            if namespace:
                self.namespaces.add(str(namespace))
            if name in SENSITIVE_TOOLS:
                # Named separately from the tool list so the query an auditor
                # actually runs -- "whose logs were read" -- does not require
                # knowing which of thirteen tools returns application output.
                self.sensitive.append({
                    "tool": name,
                    "pod": arguments.get("name") or arguments.get("pod"),
                    "namespace": namespace,
                })

        elif kind == "tool_result":
            # Size, not content. That the run read 40KB of logs is auditable;
            # what was in them is the thing being protected.
            if self.tools:
                self.tools[-1]["result_chars"] = len(event.get("result") or "")
                self.tools[-1]["duration_ms"] = event.get("duration_ms")

        elif kind == "answer":
            self.target = event.get("target")
            self.termination = event.get("termination")
            self.outcome = "terminated" if self.termination else "answered"
            # Top level, not inside "rca". grounding.check()'s verdict is
            # spread onto the answer event with **verdict, and contract()
            # returns observations and unknowns with no confidence in it at
            # all -- a record built from rca["confidence"] would have been
            # blank on every real run while passing against a fixture that
            # invented the field.
            self.verdict = event.get("confidence") or ""

    def abandoned(self):
        """
        The caller stopped reading. Not a failure, and not a completed run
        either -- distinguishing the two is the whole reason this is separate
        from failed().
        """
        if self.outcome == "incomplete":
            self.outcome = "abandoned"

    def failed(self, exc):
        self.outcome = "error"
        # The class, never the message. A provider's error text can quote the
        # request, and the request carries the evidence -- the same rule
        # inference.py applies when it logs a failover.
        self.error = type(exc).__name__

    def emit(self):
        """
        Write the record. Idempotent, because `finally` can run more than once
        on a generator that is closed after it has already completed.
        """
        if self._emitted or not enabled():
            return
        self._emitted = True

        payload = {
            "run_id": self.run_id,
            **current(),
            "question": self.question,
            "scaffolded": self.prompted,
            "target": self.target,
            "model": self.model,
            "outcome": self.outcome,
            "termination": self.termination,
            "verdict": self.verdict,
            "tool_calls": len(self.tools),
            "tools": self.tools,
            # Sorted so two records of the same run compare equal, and so a
            # human reading a page of these is not re-reading a set in a new
            # order each time.
            "namespaces": sorted(self.namespaces),
            "sensitive_reads": self.sensitive,
            "inference": _inference(),
            "duration_ms": round((time.perf_counter() - self.started) * 1000, 1),
        }
        if self.error:
            payload["error"] = self.error

        log.info("investigation", extra=payload)
        _sink(payload)


def _sink(payload):
    """
    An optional second copy, appended to TRIAGE_AUDIT_LOG.

    Separate from the application log on purpose: an audit trail interleaved
    with debug output is one that gets rotated away by whoever is trying to
    quieten the debug output. A path rather than a shipper because a shipper
    is a dependency and a file is a mount.

    Failure to write is logged and swallowed. This project's rule is that
    errors are data and never raised, and the version of this that takes the
    investigation down when a volume fills is worse than the version that
    loses a record and says so.
    """
    path = (os.getenv("TRIAGE_AUDIT_LOG") or "").strip()
    if not path:
        return
    try:
        line = json.dumps({"event": "investigation", **payload}, default=str)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception as exc:
        log.warning("audit_sink_failed",
                    extra={"path": path, "error": type(exc).__name__})


def begin(question, model=""):
    """A record for one investigation, to be observed into and then emitted."""
    return Record(question, model)
