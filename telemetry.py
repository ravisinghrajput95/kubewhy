"""
Counters and histograms, exposed in Prometheus text format.

Hand-rolled, and for the same reason observability.py hand-rolls its JSON
formatter rather than taking structlog: the surface actually used here is a
counter, a histogram and one exposition endpoint, and requirements.txt pins to
minor versions on the argument that this process holds cluster credentials and
reads pod logs. A transitive upgrade is a real risk in that position, so a
dependency has to earn its place with more than convenience.

**What this is for, and it is narrower than it looks.** The eval harness in
`evals/` measures whether answers are right. Nothing measured whether the
*system* is healthy: which provider answered, how long it took, whether a
fallback fired, how often a tool errored. Those are operator questions, they
are asked while something is wrong, and they cannot be answered from a set of
JSON files written by a benchmark that is not running.

**Labels are bounded on purpose.** Every label value here comes from
configuration (mode, provider, model) or from a closed set (outcome, tool
name). None comes from a cluster object: a `pod` label would mint a new time
series per pod per restart, and a metrics endpoint that grows with the
incident is one more thing to page about during the incident.

**Never a secret, and the type system will not save you.** Endpoints are not
labels, because an endpoint can carry a token in its userinfo or its query
string. The provider name is enough to say where a request went, and it is the
part an operator can act on.
"""

import threading
import time

# One lock for the whole registry rather than one per metric. Contention is
# irrelevant at this volume -- a diagnosis takes tens of seconds and emits a
# handful of observations -- and a single lock is the version that cannot
# deadlock when a caller updates two metrics together.
_LOCK = threading.Lock()

# Seconds. A model call is not an HTTP request and the usual web buckets are
# useless here: qwen3's measured median is 63.5s with thinking on and 9.0s
# with it off, and its p95 is 183.9s. Buckets that stop at 10 would put every
# thinking-on run in +Inf and report nothing. These span the range this
# project has actually recorded, from a warm 1-second reply to a run about to
# hit the 300s timeout.
DURATION_BUCKETS = (0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)


class Counter:
    """A monotonically increasing count, per label set."""

    kind = "counter"

    def __init__(self, name, help, labels=()):
        self.name = name
        self.help = help
        self.labels = tuple(labels)
        self.values = {}

    def inc(self, amount=1, **labels):
        key = self._key(labels)
        with _LOCK:
            self.values[key] = self.values.get(key, 0) + amount

    def _key(self, labels):
        # Positional by declared label order, so the same label set always
        # produces the same key whatever order the caller passed it in.
        missing = set(self.labels) - set(labels)
        if missing:
            raise KeyError(
                f"{self.name} needs labels {sorted(missing)}; a metric emitted "
                "with a partial label set produces a second time series that "
                "looks like a different thing"
            )
        return tuple(str(labels[name]) for name in self.labels)

    def samples(self):
        with _LOCK:
            return [(self.name, dict(zip(self.labels, key)), value)
                    for key, value in sorted(self.values.items())]


class Histogram:
    """Cumulative buckets, a sum and a count, per label set."""

    kind = "histogram"

    def __init__(self, name, help, labels=(), buckets=DURATION_BUCKETS):
        self.name = name
        self.help = help
        self.labels = tuple(labels)
        self.buckets = tuple(buckets)
        self.values = {}

    def observe(self, value, **labels):
        key = Counter._key(self, labels)
        with _LOCK:
            counts, total, seen = self.values.get(
                key, ([0] * len(self.buckets), 0.0, 0)
            )
            for index, edge in enumerate(self.buckets):
                if value <= edge:
                    counts[index] += 1
            self.values[key] = (counts, total + value, seen + 1)

    def samples(self):
        """
        Prometheus histogram exposition: cumulative le buckets, then +Inf,
        then _sum and _count.

        Cumulative is the part that is easy to get wrong. `le="5"` means "at
        most 5 seconds", not "between 2.5 and 5", so each bucket has to count
        every observation below it -- which is why observe() increments every
        edge the value fits under rather than just the first.
        """
        out = []
        with _LOCK:
            items = sorted(self.values.items())
        for key, (counts, total, seen) in items:
            labels = dict(zip(self.labels, key))
            running = 0
            for index, edge in enumerate(self.buckets):
                running = counts[index]
                out.append((f"{self.name}_bucket",
                            {**labels, "le": _edge(edge)}, running))
            out.append((f"{self.name}_bucket", {**labels, "le": "+Inf"}, seen))
            out.append((f"{self.name}_sum", labels, total))
            out.append((f"{self.name}_count", labels, seen))
        return out


def _edge(value):
    """Bucket edges as Prometheus writes them: 0.5, 1, 30 -- not 1.0."""
    return str(int(value)) if float(value).is_integer() else str(value)


# --- the metrics themselves -------------------------------------------------
#
# One place, so "what does this expose?" is answerable by reading a list rather
# than by grepping for .inc(.

INFERENCE_REQUESTS = Counter(
    "kubewhy_inference_requests_total",
    "Model calls, by where inference happened and how it ended.",
    ("mode", "provider", "model", "outcome"),
)

INFERENCE_DURATION = Histogram(
    "kubewhy_inference_duration_seconds",
    "Wall time of one model call, including tool-schema serialisation.",
    ("mode", "provider", "model"),
)

INFERENCE_TOKENS = Counter(
    "kubewhy_inference_tokens_total",
    "Tokens reported by the provider. Absent for providers that report none.",
    ("mode", "provider", "model", "kind"),
)

FALLBACKS = Counter(
    "kubewhy_inference_fallbacks_total",
    "Times the primary was unavailable and the fallback answered instead.",
    ("from_provider", "to_provider", "reason"),
)

EGRESS_DENIED = Counter(
    "kubewhy_inference_egress_denied_total",
    "Model calls refused because policy forbids sending evidence off-network.",
    ("mode", "provider"),
)

TOOL_CALLS = Counter(
    "kubewhy_tool_calls_total",
    "Tool executions, by tool and outcome. `error` counts tools that returned "
    "an {\"error\": ...} document, which is a normal result here, not a crash.",
    ("tool", "outcome"),
)

INVESTIGATIONS = Counter(
    "kubewhy_investigations_total",
    "Completed investigations, by the grounding verdict they ended on.",
    ("outcome",),
)

INVESTIGATION_DURATION = Histogram(
    "kubewhy_investigation_duration_seconds",
    "Wall time of a whole investigation: every round, every tool, end to end.",
    (),
)

REGISTRY = [
    INFERENCE_REQUESTS,
    INFERENCE_DURATION,
    INFERENCE_TOKENS,
    FALLBACKS,
    EGRESS_DENIED,
    TOOL_CALLS,
    INVESTIGATIONS,
    INVESTIGATION_DURATION,
]


def render():
    """
    The whole registry, in Prometheus text exposition format.

    A metric with no observations is rendered as its HELP and TYPE lines and
    nothing else. That is deliberate: an absent series and a zero series mean
    different things to an alert, and inventing a zero for every label
    combination would require knowing every combination in advance.
    """
    lines = []
    for metric in REGISTRY:
        lines.append(f"# HELP {metric.name} {metric.help}")
        lines.append(f"# TYPE {metric.name} {metric.kind}")
        for name, labels, value in metric.samples():
            lines.append(f"{name}{_labels(labels)} {_number(value)}")
    return "\n".join(lines) + "\n"


def _labels(labels):
    if not labels:
        return ""
    inner = ",".join(
        f'{key}="{_escape(str(value))}"' for key, value in labels.items()
    )
    return "{" + inner + "}"


def _escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value):
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def reset():
    """Drop every recorded value. For tests, which must not see each other."""
    with _LOCK:
        for metric in REGISTRY:
            metric.values.clear()


class timer:
    """
    Context manager yielding elapsed seconds, on a monotonic clock.

    perf_counter rather than time.time, and this project has the scar: a
    laptop that suspends mid-run makes wall clock report minutes of model
    latency that never happened. agent.py keeps both clocks precisely to tell
    those apart; a metric only wants the honest one.
    """

    __slots__ = ("started", "seconds")

    def __enter__(self):
        self.started = time.perf_counter()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc):
        self.seconds = time.perf_counter() - self.started
        return False
