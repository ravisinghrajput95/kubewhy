"""
Structured logging.

A tool aimed at people who run things should be greppable itself. Logs go out
as one JSON object per line, so `jq` works and a log shipper needs no parser.

Set LOG_FORMAT=text for human-readable output when working locally.
"""

import json
import logging
import os
import sys

# Attributes LogRecord always carries; anything else was passed via extra=
# and belongs in the structured output.
_BUILTIN = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"asctime", "message", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _BUILTIN:
                payload[key] = value

        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure(level=None):
    """Idempotent: safe to call from both the API and the CLI."""
    root = logging.getLogger()
    if any(getattr(h, "_triage", False) for h in root.handlers):
        return

    handler = logging.StreamHandler(sys.stderr)
    handler._triage = True

    if os.getenv("LOG_FORMAT", "json").lower() == "text":
        handler.setFormatter(logging.Formatter("%(levelname)-7s %(name)s  %(message)s"))
    else:
        handler.setFormatter(JsonFormatter())

    root.addHandler(handler)
    root.setLevel(level or os.getenv("LOG_LEVEL", "INFO").upper())

    # The kubernetes client logs every request at INFO; useful only when
    # debugging the client itself.
    logging.getLogger("kubernetes").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
