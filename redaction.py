"""
Strips credentials out of text before it reaches the model or the terminal.

Pod logs are where secrets surface: connection strings with passwords, bearer
tokens, cloud keys. Reading them into a model context and printing them to a
screen is how a credential ends up in a scrollback buffer or a chat history.

This is a best-effort filter, not a guarantee. It catches the shapes that
appear most often; a novel secret format will pass through. Anything it does
catch is replaced with a marker naming the kind of secret, so the model can
still reason about "the connection string is malformed" without seeing it.
"""

import re

# Ordered: more specific patterns first, so a JWT is not caught by the
# generic long-token rule and mislabelled.
_PATTERNS = [
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    # Google API keys carry a fixed prefix and a fixed length, so this cannot
    # collide with anything else. They reach pod logs through client libraries
    # that echo their configuration.
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    # Password inside a URL: postgres://user:secret@host -- keep the shape so
    # the model can still see which service and host are involved.
    ("URL_PASSWORD", re.compile(r"(?<=://)([^:/\s@]+):([^@/\s]+)(?=@)")),
    # key=value and "key": "value" for anything named like a secret.
    # The optional [a-z0-9_]* prefix matters: \b does not match between "_"
    # and "P", so a bare \b would miss DB_PASSWORD -- the single most common
    # form this appears in.
    # key=value and "key": "value" for anything named like a secret.
    #
    # The optional [a-z0-9_]* prefix matters: \b does not match between "_"
    # and "P", so a bare \b would miss DB_PASSWORD -- the single most common
    # form this appears in.
    #
    # The optional quote AFTER the key name is what makes this work on JSON,
    # and it was missing. `{"auth": "dXNlcjpwYXNz"}` -- a dockerconfigjson
    # image-pull secret -- and `{"data": {"password": "..."}}` -- the shape of
    # every Kubernetes Secret -- both put a quote between the name and the
    # colon, so the pattern stopped matching at exactly the two places a
    # Kubernetes credential is most likely to be written down. Measured
    # 2026-08-23.
    #
    # `account[_-]?key` is Azure's storage connection string, which is the
    # commonest cloud credential this project had no rule for.
    ("SECRET_ASSIGNMENT", re.compile(
        r"(?i)([a-z0-9]*[_-]?(?:password|passwd|secret|token|api[_-]?key|"
        r"access[_-]?key|account[_-]?key|auth|credential)s?)"
        r"([\"']?\s*[:=]\s*[\"']?)([^\s\"',;}]{4,})")),
    # Every HTTP auth scheme, not only Bearer. Basic carries
    # base64(user:password) and was passing through untouched while its
    # sibling in the same header was caught -- an inconsistency rather than a
    # novel format.
    ("HTTP_AUTH", re.compile(
        r"(?i)\b(?:bearer|basic|digest|negotiate)\s+[A-Za-z0-9._~+/=-]{12,}")),
]


def redact(text):
    """Replace recognisable secrets with [REDACTED:KIND] markers."""
    if not text:
        return text

    for kind, pattern in _PATTERNS:
        if kind == "URL_PASSWORD":
            text = pattern.sub(lambda m: f"{m.group(1)}:[REDACTED:PASSWORD]", text)
        elif kind == "SECRET_ASSIGNMENT":
            # The separator is put back exactly as it was found, rather than
            # normalised to "=". Tool output is JSON and the model reads it as
            # JSON: rewriting `"password": "x"` into `password=[REDACTED]`
            # deletes the quote and the colon and leaves a document that no
            # longer parses. grounding.check() then cannot read the evidence at
            # all, so a redaction pass meant to protect a credential would
            # silently disable the claim checker. Caught before it shipped, on
            # 2026-08-23, by parsing the output of this function.
            text = pattern.sub(
                lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:SECRET]", text)
        else:
            text = pattern.sub(f"[REDACTED:{kind}]", text)

    return text
