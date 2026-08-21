"""
Checks an answer against the tool output it was supposedly built from.

The model is asked never to invent a figure, but asking is not enforcement:
in testing qwen3 once reported an uptime of "18 days" for a host that had been
up four hours, having never called the tool that reports uptime. This module
catches that case by pulling the factual claims out of an answer -- numbers
and status names -- and checking each one appears in what the tools actually
returned.

It is deliberately a lint, not a gate. A flagged claim means "the model did
not read this anywhere", which is usually a fabrication and occasionally
arithmetic it did itself.

Known weakness -- incidental numbers launder fabrications. Tool output is full
of digits that mean nothing to the claim: timestamps, IP addresses, pod name
hashes, ports. A fabricated figure that happens to collide with one of them is
reported as grounded. This is not hypothetical: CI caught the fabricated
"18 days" test case passing on a runner whose boot timestamp was 18:12, since
"18" appeared in the measured text. Matching numbers to the units and context
they were stated in would fix it and is not implemented.

Claims are scoped to the entity they name. A sentence about one workload is
checked against the measurements for *that* workload, not against every
measurement taken -- otherwise a status measured for workload A supports the
same status asserted about workload B, and scan_cluster made that far worse by
returning every failing workload in one result. Verified on a live cluster: an
answer attributing ErrImagePull to a workload measured as ImagePullBackOff
passed as grounded, because a different workload in the same result was in
ErrImagePull.

Two deliberate looseneses remain in that scoping, both erring toward silence
rather than false alarms. A clause naming nothing recognisable is checked
against everything, because scoping a summary sentence to nothing would flag
every one of them. And entity matching is substring, so a short name inside a
longer one widens the scope. Both can cause a miss; neither invents one.

The practical consequence is that a `grounded` verdict is weaker evidence than
a `partial` one. `partial` reliably means something was not measured;
`grounded` means nothing contradicted the answer.
"""

import json
import re

# The answer states nothing that can be traced to a measurement. Distinct from
# `partial`, which means something was traced and failed, and from `grounded`,
# which now requires at least one claim that succeeded.
INSUFFICIENT = "insufficient_evidence"

# Status words worth checking. A model that reports OOMKilled when no tool
# said so is making exactly the mistake this module exists to catch.
#
# Only distinctive ones. "Running", "Pending", "Failed" and "Unknown" are
# ordinary English words as well as Kubernetes statuses, and checking them
# flagged every answer containing a phrase like "the database service is
# running" -- a false positive on a correct answer, which is the fastest way
# to make this signal worth ignoring.
KNOWN_STATUSES = {
    "oomkilled",
    "crashloopbackoff",
    "imagepullbackoff",
    "errimagepull",
    "containercreating",
    "createcontainerconfigerror",
    "evicted",
    "notready",
    "memorypressure",
    "diskpressure",
    "pidpressure",
}

# How a tool spells a status the model writes as one word. Checked in addition
# to the canonical form, never instead of it.
#
# scan_cluster labels a Running-but-unready workload `fault: not-ready`
# (routers/k8s_pods_info.py) -- a label added to stop the model dropping that
# entry from its summary. "notready" is not a substring of "not-ready", so the
# checker could not see a fault its own tool had just reported, and an answer
# repeating it verbatim scored `partial`. Observed live on 2026-08-21.
#
# An explicit table rather than stripping punctuation before comparing. The
# citation has to resolve too: cite() looks the value up in the JSON to name
# the field it came from, so the checker has to know which spelling is
# actually present rather than compare against a normalised copy that no
# longer matches anything on disk.
_TOOL_SPELLINGS = {
    "notready": ("not-ready", "not_ready"),
}

# Named causes: diagnoses that assert a mechanism rather than report a status.
# A status is what the cluster said; these are claims about WHY, and the tools
# either establish them or they are speculation wearing the same voice.
#
# Observed 2026-08-18: asked what caused "the application memory leak" in a pod
# whose only evidence was OOMKilled, the agent accepted the premise and
# explained the leak. polinux/stress allocates memory deliberately -- there is
# no leak, and nothing measured could have shown one. The answer was scored
# grounded because "memory leak" is neither a number nor a status.
#
# Deliberately short, and only phrases whose presence in tool output would be
# unambiguous. A general "is this inference?" detector is not something a
# regex can be, and a long list here would flag ordinary hedged prose.
KNOWN_CAUSES = (
    "memory leak",
    "deadlock",
    "race condition",
    "network partition",
    "dns failure",
    "disk full",
    "clock skew",
    "certificate expired",
)

# A claim labelled with the kind of thing it is about: "pod log-shipper-xyz",
# "deployment healthy-web", "namespace demo". The label is what makes the
# check safe -- an unlabelled hyphenated token is as likely to be
# "out-of-memory" as an object name, and flagging English would make this
# signal worthless.
_ENTITY_KINDS = ("pod", "deployment", "service", "namespace", "node",
                 "container", "daemonset", "statefulset", "job", "cronjob")
_LABELLED_ENTITY = re.compile(
    r"\b(" + "|".join(_ENTITY_KINDS) + r")s?\b[\s:=]+[`\"'*]*"
    r"([a-z0-9][a-z0-9.\-]{2,})[`\"'*]*",
    re.IGNORECASE,
)

# Markdown list markers and headings: "1." starting a line is enumeration,
# not a measurement, and flagging it would bury the real findings.
_ORDINAL = re.compile(r"^[\s>*\-]*\d+[.)]\s", re.MULTILINE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# A number in a recommendation ("raise the limit to 128Mi") is a proposal, not
# a claim about what was measured, and flagging it would train the reader to
# ignore this whole signal.
#
# `e\.g\.` is a separate alternative rather than a member of the word list,
# and that is the whole point. Inside the list it carried the list's trailing
# `\b`, which can only match where the next character is a word character --
# and "e.g." is followed by a comma or a space every time it is ever written.
# The branch could not fire, so "check the logs (e.g., OOMKilled)" was scored
# as a claim that the pod was OOMKilled. Found 2026-08-21 in a live
# cluster_wide_scan run; measured across every recorded result file, 10 of
# 138 unverified flags were introduced by it, in four different cases.
#
# Only the abbreviation is moved out. Widening this pattern further ("such
# as", "for example") would exempt more text from checking, which is the
# direction that hides fabrications, and no measurement asks for it.
_PRESCRIPTIVE = re.compile(
    r"\b(increase|raise|bump|set|change|update|adjust|scale|allocate|"
    r"try|consider|recommend|suggest|should be|fix)\b"
    r"|e\.g\.",
    re.IGNORECASE,
)


def _claim_text(answer):
    """
    Drop the parts of an answer that propose a value rather than report one.

    Once a line turns prescriptive it stays prescriptive to the end of that
    line. Testing each fragment in isolation was a bug: the split includes ":",
    so "Fix: Increase the limit (e.g. limits.memory: 256Mi)" breaks apart
    inside the Kubernetes field name, leaving "256Mi and requests.memory:" as a
    fragment containing no verb at all. Those numbers were then flagged as
    unmeasured -- on a recommendation, which is exactly what the exemption
    exists to avoid, and on `key: value` syntax, which is how every resource
    recommendation this tool gives is written.

    A measurement stated *before* the verb on the same line is still checked,
    so "The pod uses 64Mi. Raise it to 128Mi." keeps the 64Mi claim. Only a
    measurement stated after a recommendation begins is lost, which is the
    rarer order and the price of not crying wolf on ordinary advice.
    """
    return "\n".join(_claims(answer))


def _claims(answer):
    """
    The reporting clauses of an answer, in order. See _claim_text.

    List markers go before the split, not after: "1. first uses 19.66%" splits
    on the "." into a bare "1." that no longer looks like enumeration, and the
    numbering gets reported as an unmeasured figure.
    """
    keep = []
    in_block = False
    block_is_proposal = False
    previous_was_prescriptive = False

    for line in answer.splitlines():
        if line.strip().startswith("```"):
            if not in_block:
                # A fenced block inherits the intent of the prose introducing
                # it. "Fix: ... ```yaml limits.memory: 256Mi``` " is one
                # recommendation split across a fence, and its lines contain no
                # verb of their own, so checking them as claims flags the very
                # values being proposed. A block introduced by ordinary prose
                # is still evidence and still checked.
                block_is_proposal = previous_was_prescriptive
            in_block = not in_block
            continue

        if in_block:
            if not block_is_proposal:
                keep.append(line)
            continue

        stripped = _ORDINAL.sub(" ", line)
        prescriptive = False
        for clause in re.split(r"(?<=[.;:!?])\s+", stripped):
            if _PRESCRIPTIVE.search(clause):
                prescriptive = True
                break
            keep.append(clause)

        if line.strip():
            previous_was_prescriptive = prescriptive

    return keep


# Keys whose value names the subject of a whole tool result: describe_pod
# returns {"pod": "memory-hog-x", ...}, and everything in that document is
# about that pod.
_SUBJECT_KEYS = ("pod", "service", "node", "deployment", "workload")


def records(tool_outputs, names=None):
    """
    Wrap raw tool results as evidence records with stable ids.

    A claim is only auditable if you can say *which* result supports it, so
    every result gets an id ("tool-1") and, when the caller knows it, the name
    of the tool that produced it. check() accepts either shape: a plain list
    of JSON strings, as every existing caller passes, or these records.
    """
    names = names or []
    out = []
    for i, text in enumerate(tool_outputs, 1):
        out.append({
            "id": f"tool-{i}",
            "tool": names[i - 1] if i - 1 < len(names) else None,
            "result": text,
        })
    return out


def _locate(value, data, path=""):
    """
    The field a value was found in, as a dotted path, or None.

    This is what turns "the answer said 64Mi and so did some tool" into
    "describe_pod reported containers.hog.limits.memory = 64Mi". Without the
    field the citation is not much better than a substring match.
    """
    if isinstance(data, dict):
        for key, item in data.items():
            found = _locate(value, item, f"{path}.{key}" if path else str(key))
            if found:
                return found
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found = _locate(value, item, f"{path}[{index}]")
            if found:
                return found
    else:
        if value in str(data).lower():
            return path or None
    return None


def _entity_index(tool_outputs):
    """
    Map each thing a tool described to the text describing *it*.

    Two shapes cover every collector here. A document that names its subject
    (describe_pod, get_pod_logs, get_service_endpoints) belongs wholly to that
    subject. A document that is a mapping of name to detail (list_pods,
    scan_cluster, list_nodes, list_deployments) contributes one entry per key.

    scan_cluster keys look like "namespace/workload", so the trailing segment
    is registered too -- an answer usually says "bad-image", not
    "demo/bad-image".
    """
    index = {}

    def add(name, text, source=None):
        name = str(name).strip().lower()
        if not name:
            return
        index.setdefault(name, []).append({"text": text, "source": source})

        # Answers name the workload ("bad-image"), while the measurement is
        # filed under the pod ("bad-image-647c5576d5-pxmvr"). Register the
        # ReplicaSet-trimmed prefix as an alias, the same trimming
        # workload_of() does, or scoping invents failures: a restart count read
        # by describe_pod would not be in scope for a sentence about the
        # workload that owns it.
        parts = name.split("-")
        if len(parts) >= 3:
            index.setdefault("-".join(parts[:-2]), []).append(
                {"text": text, "source": source}
            )

    for record in tool_outputs:
        output = record["result"] if isinstance(record, dict) else record
        source = record if isinstance(record, dict) else None
        try:
            data = json.loads(output)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        subject = next(
            (data[key] for key in _SUBJECT_KEYS if isinstance(data.get(key), str)),
            None,
        )
        if subject:
            add(subject, output, source)
            continue

        for key, value in data.items():
            if not isinstance(value, dict) or str(key).startswith("_"):
                continue
            if key in ("result", "error"):
                continue
            fragment = json.dumps({key: value})
            add(key, fragment, source)
            if "/" in str(key):
                add(str(key).rsplit("/", 1)[-1], fragment, source)

    return index


def _scope(clause, index, everything):
    """
    The measurements that can support this clause.

    Narrowed to the entities the clause actually names, which is the whole
    point: a status measured for one workload must not validate the same
    status asserted about a different one. A clause naming nothing recognisable
    falls back to every measurement, because scoping it to nothing would flag
    every summary sentence.

    Matching is substring, so a short name inside a longer one ("web" within
    "healthy-web") pulls in an extra entity. That widens the scope, which can
    only cause a miss, never a false alarm -- the safe direction for a check
    people are meant to trust.
    """
    lowered = clause.lower()
    hits = [
        entry for name, entries in index.items() if name in lowered
        for entry in entries
    ]
    return hits if hits else everything


def _numbers(text, strip_ordinals=False):
    if strip_ordinals:
        text = _ORDINAL.sub(" ", text)
    return {float(m.group()) for m in _NUMBER.finditer(text)}


def _matches(claim, measured):
    """
    True if a measured value supports the claim.

    Exact first, then rounding: a model that says "20%" off a measured 19.66
    is summarising, not inventing, so match when some measurement rounds to
    the claim at the precision the claim was stated to.
    """
    if claim in measured:
        return True

    decimals = 0
    if "." in f"{claim}":
        decimals = len(f"{claim}".split(".")[1].rstrip("0"))

    return any(round(value, decimals) == claim for value in measured)


def _inference(value, kind):
    """
    A hedged claim, recorded but not held to the evidence.

    It carries no citation by construction -- a tool did not say this -- and
    `contract()` files anything that is not "observed" under inferences, so
    the claim stays visible in the record without counting against grounding.
    """
    return {"value": value, "kind": kind, "status": "inferred", "evidence": []}


def check(answer, tool_outputs):
    """
    Compare an answer against the tool results behind it.

    tool_outputs is the list of raw JSON strings the tools returned, or the
    records() shape when the caller knows which tool produced which result.
    Returns {"confidence", "unverified", "checked", "claims"} where confidence
    is:

      grounded               at least one claim, and every claim traces to a
                             tool result for the entity it was made about
      partial                some claims do not
      ungrounded             claims were made with no tool result behind them,
                             or the answer was empty
      insufficient_evidence  the answer states nothing this can check

    **grounded requires a verified claim.** It used to mean "nothing
    contradicted the answer", which an empty string satisfies trivially: an
    agent that returned "" scored the same badge as one that quoted the
    kubelet. Measured against a live cluster on 2026-08-18 -- a run whose tool
    call 404'd returned an empty answer and was reported `grounded`. Silence is
    not evidence, and neither is prose with nothing falsifiable in it.
    """
    text = (answer or "").strip()
    if not text:
        # No claims, no answer, nothing to be confident about. This is the
        # case that motivated the rewrite; it must never come back grounded.
        return {
            "confidence": "ungrounded",
            "unverified": [],
            "checked": 0,
            "claims": [],
        }

    if not tool_outputs:
        # No measurements were taken, so nothing in the answer is grounded --
        # but an answer with no factual claims is fine.
        claims = _numbers(_claim_text(answer), strip_ordinals=True) or set()
        return {
            "confidence": "ungrounded" if claims else INSUFFICIENT,
            "unverified": sorted(_format(c) for c in claims),
            "checked": len(claims),
            "claims": [
                {"value": _format(c), "kind": "number", "status": "unverified",
                 "evidence": []}
                for c in sorted(claims)
            ],
        }

    evidence = (
        tool_outputs
        if tool_outputs and isinstance(tool_outputs[0], dict)
        else records(tool_outputs)
    )
    everything = [{"text": r["result"], "source": r} for r in evidence]
    measured_text = " ".join(r["result"] for r in evidence)
    index = _entity_index(evidence)

    unverified = []
    claims = []
    # How many claims were actually examined, which is not the same question as
    # how many failed. With no claims at all, unverified is empty and the answer
    # comes back "grounded" -- a green badge on "I could not identify any
    # failing pods", which reads as though the cluster confirmed it. Empty and
    # clean are different states and the caller has to be able to tell them
    # apart.
    checked = 0

    def flag(item):
        # One mention is enough; repeating a claim should not repeat the alarm.
        if item not in unverified:
            unverified.append(item)

    for clause in _claims(answer):
        # Each clause is checked against the measurements for the entity it
        # names, not against every measurement taken. Otherwise a status
        # measured for one workload silently supports the same status claimed
        # about another -- and the wider the tool result, the more it covers.
        entries = _scope(clause, index, everything)
        scope = " ".join(e["text"] for e in entries)
        scope_numbers = _numbers(scope)
        scope_lower = scope.lower()

        def cite(value):
            """Which result, and which field in it, carries this value."""
            for entry in entries:
                source = entry["source"] or {}
                try:
                    data = json.loads(entry["text"])
                except (TypeError, ValueError):
                    continue
                field = _locate(str(value).lower(), data)
                if field:
                    return [{
                        "id": source.get("id"),
                        "tool": source.get("tool"),
                        "field": field,
                        "value": str(value),
                    }]
            return []

        for claim in sorted(_numbers(clause, strip_ordinals=True)):
            checked += 1
            supported = _matches(claim, scope_numbers)
            if not supported:
                flag(_format(claim))
            claims.append({
                "value": _format(claim),
                "kind": "number",
                "status": "observed" if supported else "unverified",
                "evidence": cite(_format(claim)) if supported else [],
            })

        lowered = clause.lower()

        # Entity identity, before any value is considered. A claim about a pod
        # no tool ever returned cannot be grounded however correct its numbers
        # are -- and its numbers are often correct, because the model lifted
        # them from the pod it should have been talking about.
        #
        # Observed live 2026-08-19: "The crasher pod log-shipper-8gnqk is in
        # Error with 7 restarts". Error and 7 were both measured, for
        # log-shipper, so every value checked out and the answer scored
        # grounded while naming the wrong workload in the same breath.
        for kind, name in _LABELLED_ENTITY.findall(clause):
            name = name.strip(".,;:").lower()
            if not name or name in _ENTITY_KINDS:
                continue
            # Must look like a generated object name rather than the next
            # English word. "The pod restarted 9 times" captured `restarted`
            # and reported a nonexistent pod; requiring a hyphen or a digit
            # keeps log-shipper-xyz and crasher-abc123 and drops every verb.
            # A bare word like `demo` is skipped here and caught, when it is
            # real, by the evidence test below.
            if not any(ch.isdigit() or ch == "-" for ch in name):
                continue
            # Known if any indexed entity matches it either way round -- the
            # answer may say the workload where the tool keyed the pod -- or
            # if the name appears anywhere in what the tools returned. The
            # second test matters for kinds the index does not key on:
            # "namespace demo" is a field value, not a document subject, and
            # flagging it would fire on almost every correct answer.
            known = (
                name in measured_text.lower()
                or any(name == entity or name in entity or entity in name
                       for entity in index)
            )
            if known:
                continue
            checked += 1
            flag(f"{kind.lower()} {name}")
            claims.append({
                "value": f"{kind.lower()} {name}",
                "kind": "entity",
                "status": "unverified",
                "evidence": [],
            })

        # A named cause is only a finding if a tool said so. Hedged, it is
        # honest speculation and the prompt asks for exactly that, so a clause
        # that marks itself is not held to the evidence -- flagging "possibly
        # a memory leak" would punish the labelling this project asks for.
        # It is still recorded, as an inference, so the claim stays auditable
        # rather than disappearing from the record for having been hedged.
        hedged = any(word in lowered for word in
                     ("likely", "possibl", "probabl", "may ", "might", "could",
                      "suspect", "perhaps", "appears", "seems", "worth checking"))
        for cause in KNOWN_CAUSES:
            if cause in lowered:
                supported = cause in scope_lower
                if not supported and hedged:
                    claims.append(_inference(cause, "cause"))
                    continue
                checked += 1
                if not supported:
                    flag(cause)
                claims.append({
                    "value": cause,
                    "kind": "cause",
                    "status": "observed" if supported else "unverified",
                    "evidence": cite(cause) if supported else [],
                })

        # Status names are checked as plain substrings; the model may style
        # them as **OOMKilled** or `OOMKilled`, so compare lowered.
        #
        # A hedged status is treated exactly as a hedged cause, and for the
        # same reason. `scan_cluster` reports a workload's phase and not its
        # termination reason, so "CrashLoopBackOff (likely OOMKilled)" is an
        # inference the tools cannot settle either way -- correctly labelled,
        # which is the behaviour `inference_is_marked` grades for. Holding it
        # to the evidence made a correctly hedged answer score `partial`:
        # replayed over the 34 scan_cluster-only probe runs, 22 of the 30
        # unverified flags were `oomkilled` and 15 of those sat in a hedged
        # clause; exempting them moves the set from 10/34 grounded to 20/34,
        # with no run moving the other way (McNemar exact, p=0.002).
        #
        # An unhedged status is still checked, so a flat "the pod was
        # OOMKilled" with nothing behind it is caught as before.
        for status in KNOWN_STATUSES:
            if status in lowered:
                # Whichever spelling the tool used, so the citation below can
                # find it in the result it came from.
                present = next(
                    (spelling
                     for spelling in (status, *_TOOL_SPELLINGS.get(status, ()))
                     if spelling in scope_lower),
                    None,
                )
                supported = present is not None
                if not supported and hedged:
                    claims.append(_inference(status, "status"))
                    continue
                checked += 1
                if not supported:
                    flag(status)
                claims.append({
                    "value": status,
                    "kind": "status",
                    "status": "observed" if supported else "unverified",
                    "evidence": cite(present) if supported else [],
                })

    if not checked:
        # Tools ran and the answer asserts nothing this can trace. Not a
        # failure and not a confirmation: the honest word for it is that there
        # is no evidence either way, and a caller badging this as grounded is
        # putting a tick next to an unfalsifiable sentence.
        confidence = INSUFFICIENT
    elif unverified:
        confidence = "partial"
    else:
        confidence = "grounded"

    return {
        "confidence": confidence,
        "unverified": unverified,
        "checked": checked,
        "claims": claims,
    }


def _format(number):
    return str(int(number)) if number == int(number) else str(number)


AUDIT_MARKER = "Evidence audit"


def annotate(answer, verdict):
    """
    Append a deterministic evidence audit to an answer that needs one.

    The model is told not to invent figures and mostly obeys; "mostly" is the
    problem. Observed live 2026-08-18, all three inside otherwise correct
    diagnoses: a 512Mi limit reported for a container measured at 64Mi, a
    "503 Service Unavailable" for a probe that was refused a connection, and
    "exit code 137 indicates the OOM killer" for a pod with no memory limit at
    all. The RCA headline was right in every case, which is what makes this the
    dangerous failure mode -- a reader has no way to tell which half to trust.

    The prose is left alone rather than rewritten. Deleting a clause out of an
    answer risks changing what it says, and this text is going to an on-call
    engineer who has to be able to trust that the sentences are the model's.
    So the unsupported values are named underneath, with the evidence that a
    grounded claim rests on named the same way. Marking, not silent removal.

    Returns the answer unchanged when there is nothing to say about it, so an
    ordinary grounded answer carries no boilerplate.
    """
    text = (answer or "").strip()
    if not text or AUDIT_MARKER in text:
        return answer

    confidence = verdict.get("confidence")
    unverified = verdict.get("unverified") or []
    checked = verdict.get("checked", 0)

    # Only when something is actually wrong. The first cut also annotated
    # `insufficient_evidence`, which put a "Root cause: UNKNOWN" banner under
    # "It is healthy." -- a correct, complete answer to a question about a
    # healthy workload. Crying wolf on good answers is how a signal becomes
    # something readers learn to skip, and this module has been bitten by that
    # before. The state stays on the record for callers and evals to read; it
    # does not become prose.
    if not unverified and confidence != "ungrounded":
        return answer

    lines = ["", "---", f"**{AUDIT_MARKER}.**"]

    if unverified:
        values = ", ".join(f"`{value}`" for value in unverified)
        lines.append(
            f"{len(unverified)} of {checked} stated values could not be traced "
            f"to any tool result for the workload they were stated about: "
            f"{values}. Treat these as inference, not measurement."
        )

    if confidence == "ungrounded":
        lines.append(
            "No tool result supports this answer. Nothing here was measured."
        )

    return text + "\n" + "\n".join(lines)


# A value is only replaced where it stands alone. Without the neighbour guard
# a flagged "3" rewrites the 3 inside `memory-hog-bc76968c6-87fbc`, and a
# redaction pass that corrupts pod names is worse than the fabrication it was
# built to remove.
def _standalone(value):
    """
    The value as its own token, with any unit still attached.

    _numbers extracts digits, so a fabricated "512Mi" is flagged as "512" --
    and replacing only the digits would leave a dangling "Mi" in the sentence.
    The optional suffix keeps the token whole, and the neighbour guards stop
    it matching inside a pod hash.
    """
    # The dot is excluded only when it continues a number: "3.5" must not
    # match a flagged "3", while "HTTP 503." at the end of a sentence must.
    # The first cut excluded every following dot and silently skipped every
    # claim that ended a sentence -- which is most of them.
    return re.compile(
        r"(?<![\w\-])(?<!\d\.)" + re.escape(value)
        + r"(?:[a-zA-Z%]+)?(?![\w\-])(?!\.\d)",
        re.IGNORECASE,
    )

_UNIT = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-zA-Z%]+)$")


def _observed_counterpart(token, scope_text):
    """
    The measured value this fabricated one was probably meant to be.

    Only for a claim carrying a unit -- 512Mi against a measured 64Mi -- and
    only when the evidence in scope holds exactly one value in that same unit.
    One candidate is a correction; two is a guess, and this module does not
    guess. Returns None when it cannot be sure, and the caller marks the claim
    unverified instead of replacing it.
    """
    match = _UNIT.match(token.strip())
    if not match:
        return None

    unit = match.group(2)
    found = {
        m.group(0) for m in
        re.finditer(r"\d+(?:\.\d+)?" + re.escape(unit) + r"\b", scope_text)
    }
    found = {f for f in found if f.lower() != token.strip().lower()}
    return found.pop() if len(found) == 1 else None


def verify(answer, verdict, tool_outputs=()):
    """
    Rewrite a draft so no unsupported specific survives as a statement of fact.

    The audit that preceded this named the fabricated values underneath the
    answer and left them in the prose above it, which is only half a fix: the
    sentence "the pod has a 512Mi memory limit" still read as a measurement,
    and a reader skimming for the number found the wrong one. Detection is not
    prevention.

    Three outcomes per unsupported value, in order of what the evidence can
    support:

      corrected   the claim carries a unit and the evidence in scope holds
                  exactly one value in that unit -> replaced, and labelled
                  as the observed figure
      marked      no counterpart -> wrapped as [unverified: X] so it can
                  still be read, and can no longer be read as measured
      untouched   the value sits in a recommendation rather than a claim,
                  which _claims already excludes -- proposing 256Mi is not
                  asserting it

    Prose is rewritten at the value, never regenerated. Handing the draft back
    to the model to rewrite would invite a second round of invention, and
    deleting whole sentences changes what the answer says. Replacing the
    fabricated token in place is the smallest edit that makes the sentence
    true.
    """
    text = (answer or "").strip()
    unverified = verdict.get("unverified") or []
    if not text or not unverified:
        return answer, []

    scope_text = " ".join(
        r["result"] if isinstance(r, dict) else r for r in (tool_outputs or [])
    )

    # Only reporting lines are rewritten. A prescriptive line is a proposal,
    # and "raise it to 256Mi" must survive intact or the advice becomes
    # nonsense.
    claim_lines = set()
    for line in text.splitlines():
        if line.strip() and _claims(line):
            claim_lines.add(line)

    edits = []
    out = []
    for line in text.splitlines():
        if line not in claim_lines:
            out.append(line)
            continue

        for value in unverified:
            pattern = _standalone(value)
            if not pattern.search(line):
                continue

            # The token as it appears, e.g. "512Mi" for a flagged "512".
            token = pattern.search(line).group(0)
            observed = _observed_counterpart(token, scope_text)
            if observed:
                line = pattern.sub(f"{observed} (observed)", line)
                edits.append({"claim": token, "action": "corrected",
                              "observed": observed})
            else:
                line = pattern.sub(f"[unverified: {token}]", line)
                edits.append({"claim": token, "action": "marked"})
        out.append(line)

    return "\n".join(out), edits


def contract(verdict, edits=()):
    """
    The answer as a fact contract: what was observed, inferred, and unknown.

    Built from the verified claims rather than asked of the model, because a
    model that can invent a memory limit can invent the citation for it just
    as easily. Every observation here carries the result id and field the
    value was actually found in.
    """
    observations, inferences = [], []
    for claim in verdict.get("claims", []):
        if claim["status"] == "observed":
            observations.append({
                "claim": claim["value"],
                "kind": claim["kind"],
                "evidence": claim["evidence"],
            })
        else:
            inferences.append({"claim": claim["value"], "kind": claim["kind"]})

    return {
        "observations": observations,
        "inferences": inferences,
        # What the run could not establish: every value it stated and could
        # not support, after rewriting.
        "unknowns": [e["claim"] for e in edits if e["action"] == "marked"],
        "corrections": [e for e in edits if e["action"] == "corrected"],
    }
