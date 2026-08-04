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
"""

import re

# Status words worth checking. A model that reports OOMKilled when no tool
# said so is making exactly the mistake this module exists to catch.
KNOWN_STATUSES = {
    "oomkilled",
    "crashloopbackoff",
    "imagepullbackoff",
    "errimagepull",
    "containercreating",
    "createcontainerconfigerror",
    "evicted",
    "pending",
    "running",
    "succeeded",
    "failed",
    "unknown",
    "terminating",
    "notready",
    "memorypressure",
    "diskpressure",
    "pidpressure",
}

# Markdown list markers and headings: "1." starting a line is enumeration,
# not a measurement, and flagging it would bury the real findings.
_ORDINAL = re.compile(r"^[\s>*\-]*\d+[.)]\s", re.MULTILINE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# A number in a recommendation ("raise the limit to 128Mi") is a proposal, not
# a claim about what was measured, and flagging it would train the reader to
# ignore this whole signal.
_PRESCRIPTIVE = re.compile(
    r"\b(increase|raise|bump|set|change|update|adjust|scale|allocate|"
    r"try|consider|recommend|suggest|should be|e\.g\.)\b",
    re.IGNORECASE,
)


def _claim_text(answer):
    """Drop clauses that propose a value rather than report one."""
    keep = []
    for line in answer.splitlines():
        for clause in re.split(r"(?<=[.;:!?])\s+", line):
            if not _PRESCRIPTIVE.search(clause):
                keep.append(clause)
    return "\n".join(keep)


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


def check(answer, tool_outputs):
    """
    Compare an answer against the tool results behind it.

    tool_outputs is the list of raw JSON strings the tools returned. Returns
    {"confidence": ..., "unverified": [...]} where confidence is:

      grounded    every claim traces to tool output
      partial     some claims do not
      ungrounded  the model answered having called no tools at all
    """
    if not tool_outputs:
        # No measurements were taken, so nothing in the answer is grounded --
        # but an answer with no factual claims is fine.
        claims = _numbers(_claim_text(answer), strip_ordinals=True) or set()
        return {
            "confidence": "ungrounded" if claims else "grounded",
            "unverified": sorted(_format(c) for c in claims),
        }

    measured_text = " ".join(tool_outputs)
    measured_numbers = _numbers(measured_text)
    measured_lower = measured_text.lower()

    unverified = [
        _format(claim)
        for claim in sorted(_numbers(_claim_text(answer), strip_ordinals=True))
        if not _matches(claim, measured_numbers)
    ]

    # Status names are checked as plain substrings; the model may style them
    # as **OOMKilled** or `OOMKilled`, so compare against a lowered answer.
    answer_lower = answer.lower()
    for status in KNOWN_STATUSES:
        if status in answer_lower and status not in measured_lower:
            unverified.append(status)

    return {
        "confidence": "partial" if unverified else "grounded",
        "unverified": unverified,
    }


def _format(number):
    return str(int(number)) if number == int(number) else str(number)
