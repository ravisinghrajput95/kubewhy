"""
Did the answer engage with the log that was already in its prompt?

`capture_evidence()` reads a pod's logs at enqueue time and `ask(prefetched=)`
puts them in the user message, so by the time the model writes a diagnosis for
`crasher` the line `FATAL: could not connect to db:5432: connection refused`
has been in front of it since round one. On 2026-08-20 a controller run on
that pod produced a 52-character `ungrounded` diagnosis anyway, after one
wrong tool call. `LOGS_POLICY` was satisfied -- correctly, prefetched logs
*were* gathered -- and nothing downstream asks the next question. **The loop
verifies evidence was gathered, never that it was read.**

This is the instrument for the rate. It is the third attempt; the first two
were wrong in ways worth keeping written down, because both looked right while
producing numbers:

1. **Token overlap between the log and the answer.** It failed on punctuation
   boundaries -- the log says `db:5432:` and the answer says `db:5432`, which
   are different tokens -- and on the JSON escape in `capture_pod_logs`, whose
   `json.dumps` leaves a literal `\\n` glued to the front of `FATAL`, making
   `\\nfatal` a token that no answer can contain.
2. **The same thing with the punctuation stripped first.** Stripping `:` and
   `.` before testing removes the very boundary the test was about, and turns
   `db:5432` into `db5432`, which no answer contains either.

Overlap is the wrong family of instrument regardless, because **the model
paraphrases**: `db:5432` comes back as `db-service:5432`, and `upstream
returned 503` as "the 503 error". What is needed is per-workload ground-truth
phrases with their accepted paraphrases enumerated, matched as bounded
substrings of normalised text.

**Only two demo workloads qualify.** A workload belongs here when its root
cause lives in the log and nowhere else, so an answer that ignores the log
cannot reach it:

- `crasher` -- the status says `CrashLoopBackOff`, which is a symptom. Only
  the log names the dependency and the refusal.
- `nightly-sync` -- the status says `Error` and exit 1. Only the log says 503.

`memory-hog` is deliberately excluded: its log is `stress` output about
allocating memory and its cause (`OOMKilled`, a 64Mi limit) is in the status,
so an answer that ignores that log is *right* to. Scoring it here would
measure obedience, not reading. `bad-image` has no logs at all.

Two things this does not claim. A matched phrase says the answer carries
something only the log supplies; it does not say the reasoning was sound.
And an unmatched fact on a run whose evidence was empty means nothing at all,
which is what `evidence_carries()` is for -- check it first and void the run,
or you are scoring the model for failing to read a log it was never given.
"""

import re
import unicodedata

# Neighbours that make a match a different token. Alphanumerics only: `-` and
# `_` are NOT included, so `db:5432` still matches inside `to db:5432:` and
# `503` inside `HTTP-503`, while `5432` inside `54321` and `503` inside `1503`
# do not. This is the boundary rule the token-overlap attempts got wrong from
# both directions -- once by splitting on the punctuation, once by deleting it.
# The cost of leaving `-` out is that a phrase can match inside a hyphenated
# Kubernetes name (`db-service` inside `needs-db-service`). Accepted: adding
# `-` would break `HTTP-503`, and no diagnosis of `crasher` has yet named an
# unrelated workload whose name ends in one of these phrases.
_ALNUM = re.compile(r"[a-z0-9]")

# json.dumps() escapes, since capture_pod_logs stores its result as a JSON
# string and the model reads that string. Turned into whitespace rather than
# removed: `\n` between two words is a word boundary, and dropping it would
# join them into a token that appears in neither text.
_ESCAPED = re.compile(r"\\[nrtfv]")

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = dict.fromkeys(map(ord, "‘’‛′"), "'")
_QUOTES.update(dict.fromkeys(map(ord, "“”‟″"), '"'))


def normalise(text):
    """
    Lowercase, unescape and collapse -- and keep every punctuation mark.

    The one thing this must not do is strip punctuation. `db:5432` is a host
    and a port joined by a colon, and an instrument that removes the colon is
    asking whether the answer contains `db5432`, which is a string that occurs
    nowhere. Boundaries are handled at match time instead.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_DASHES).translate(_QUOTES)
    text = _ESCAPED.sub(" ", text)
    text = text.replace('\\"', '"').replace("\\\\", "\\")
    return re.sub(r"\s+", " ", text.lower()).strip()


def positions(phrase, text):
    """Offsets where `phrase` occurs in `text` as its own token."""
    found = []
    start = 0
    while True:
        at = text.find(phrase, start)
        if at < 0:
            return found
        before = text[at - 1] if at else ""
        end = at + len(phrase)
        after = text[end] if end < len(text) else ""
        if not _ALNUM.match(before or " ") and not _ALNUM.match(after or " "):
            found.append(at)
        start = at + 1


def contains(phrase, text):
    """True when `phrase` occurs in `text` as its own token."""
    return bool(positions(phrase, text))


class Fact:
    """
    One thing the log says and no other tool result does.

    `source` is the exact substring of the real container log the fact is
    drawn from; `evidence_carries()` checks it against what was actually
    captured, so a fixture whose command changes fails loudly here rather than
    quietly halving the score.

    `accept` is the ground-truth phrase first and every paraphrase observed in
    a real answer after it. Adding a paraphrase after seeing it is legitimate
    -- the question is whether the answer engaged with the log, not whether it
    quoted it -- but each one widens the instrument, so add it only where the
    phrase cannot be reached without having read the line.
    """

    __slots__ = ("name", "source", "accept")

    def __init__(self, name, source, accept):
        self.name = name
        self.source = normalise(source)
        self.accept = tuple(normalise(phrase) for phrase in accept)

    def matched(self, text):
        """The first accepted phrase present, or None."""
        for phrase in self.accept:
            if contains(phrase, text):
                return phrase
        return None


_CONNECT_FAILURE = (
    "connection refused",
    "connection was refused",
    "refused the connection",
    "refused connection",
    "refusing connections",
    "could not connect",
    "couldn't connect",
    "cannot connect",
    "can't connect",
    "unable to connect",
    "failed to connect",
    "connection failed",
    "connection failure",
    "connection error",
    # The bare participle, last so that a more specific phrase is the one
    # reported. Accepted because in a diagnosis of `crasher` there is nothing
    # else to refuse: no probe, no admission webhook, no image pull.
    "refused",
)

# Keyed by workload, because the phrases are the fixture's, not the model's.
# demo/broken-pods.yaml:
#   crasher      sh -c "echo 'connecting to database...'; sleep 2;
#                       echo 'FATAL: could not connect to db:5432:
#                       connection refused' >&2; exit 1"
#   nightly-sync sh -c "echo 'FATAL: upstream returned 503'; exit 1"
FACTS = {
    "crasher": (
        # The port is the strongest marker in the suite: 5432 appears in no
        # status, no event and no projection -- only in this log line.
        Fact("endpoint", "db:5432", ("5432",)),
        # What the port is for. `postgres` is an inference *from* 5432 rather
        # than a quote, and is accepted for that reason: it is not reachable
        # without the line.
        Fact("dependency", "connecting to database", (
            "database", "db-service", "db:5432", "postgres", "postgresql",
        )),
        Fact("failure", "connection refused", _CONNECT_FAILURE),
    ),
    "nightly-sync": (
        Fact("code", "503", ("503", "service unavailable")),
        Fact("upstream", "upstream returned", (
            "upstream", "remote service", "external service", "remote endpoint",
            "remote server", "external endpoint",
        )),
    ),
}

# Everything here is in the pod status or its events, so an answer can carry
# all of it having read no log at all. Not scored: recorded, so that a run
# with no facts can be told apart from a run with no content. Without this an
# empty answer and a fluent restatement of the status look identical.
DECOYS = {
    "crasher": (
        Fact("phase", "", ("crashloopbackoff", "crash loop", "back-off", "backoff")),
        Fact("exit", "", ("exit code 1", "exit status 1", "exited with 1")),
        Fact("restarts", "", ("restart", "restarts", "restarted", "restarting")),
    ),
    "nightly-sync": (
        Fact("phase", "", ("error", "failed", "terminated")),
        Fact("exit", "", ("exit code 1", "exit status 1", "exited with 1")),
        Fact("kind", "", ("cronjob", "cron job", "job")),
    ),
}


def evidence_text(prefetched):
    """
    The text `ask(prefetched=)` actually put in front of the model.

    capture_pod_logs() returns [{"name":..., "result": json.dumps(...)}], so
    the log arrives as a JSON string with escaped newlines. Reading `result`
    rather than re-fetching the log is deliberate: the question is what was in
    the prompt, and the pod may well be gone by the time anyone asks.
    """
    return normalise(" ".join(
        str(call.get("result", "")) for call in (prefetched or [])
    ))


def evidence_carries(workload, prefetched):
    """
    Facts whose source line is missing from the captured evidence.

    Empty means the run is measurable. Non-empty means it is void, not failed:
    `capture_pod_logs` returns [] on a 404, an error dict or a pod with no
    logs yet, and scoring "did not mention 5432" against a run that was never
    shown 5432 is the harness measuring itself.
    """
    text = evidence_text(prefetched)
    return [fact.name for fact in FACTS.get(workload, ()) if not contains(fact.source, text)]


def read(workload, answer):
    """
    Which log-only facts this answer carries.

    Returns {"facts", "matched", "read", "complete", "decoys", "status_only"}.

    `read` is the headline and it is deliberately generous -- one fact is
    enough. The failure this exists to catch is an answer that engaged with
    the log *not at all*, and a diagnosis that names the refused connection
    without repeating the port number has plainly read the line.
    """
    text = normalise(answer)
    facts = {fact.name: fact.matched(text) for fact in FACTS.get(workload, ())}
    matched = sorted(name for name, hit in facts.items() if hit)
    decoys = sorted(
        fact.name for fact in DECOYS.get(workload, ()) if fact.matched(text)
    )
    return {
        "workload": workload,
        "facts": facts,
        "matched": matched,
        "read": bool(matched),
        "complete": bool(facts) and len(matched) == len(facts),
        "decoys": decoys,
        # The specific shape of the defect: fluent about the status, silent
        # about the line that says why.
        "status_only": bool(decoys) and not matched,
    }
