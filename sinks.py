"""
Where a finished diagnosis goes.

Kept deliberately dumb: a sink formats and delivers, and never decides whether
something is worth sending. That decision belongs to the controller, which has
the dedup and rate-limit state.
"""

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("triage.sink")

# Slack rejects a section block over 3000 characters, so the diagnosis has to
# be bounded. Leaving room for the marker below.
_SLACK_LIMIT = 2900


def _fit(text, limit=_SLACK_LIMIT):
    """
    Trim a diagnosis to Slack's block limit without lying about it.

    A hard slice cut mid-sentence and said nothing, so a truncated answer read
    as a complete one that simply stopped making sense -- and the reader had no
    way to know the rest existed. Cut at a paragraph or sentence boundary where
    there is one nearby, and always say that something was dropped.
    """
    if len(text) <= limit:
        return text

    marker = "\n\n_… truncated, see the controller logs for the full diagnosis._"
    room = limit - len(marker)
    head = text[:room]

    # Prefer a paragraph break, then a sentence end, but only if it is not so
    # far back that it throws away most of the message.
    for boundary in ("\n\n", ". "):
        cut = head.rfind(boundary)
        if cut > room * 0.6:
            head = head[:cut]
            break

    return head.rstrip() + marker

# Emoji by severity of what was found, so a channel can be skimmed.
_ICON = {
    "OOMKilled": ":boom:",
    "CrashLoopBackOff": ":recycle:",
    "ImagePullBackOff": ":no_entry:",
    "ErrImagePull": ":no_entry:",
    "Evicted": ":wastebasket:",
    "Pending": ":hourglass:",
}


class StdoutSink:
    """Default. Also what you want when trying the controller out."""

    def send(self, finding):
        print(format_text(finding), flush=True)


class SlackSink:
    """
    Posts to an incoming webhook.

    Delivery failures are logged and swallowed: a broken webhook must not take
    down the controller, and the diagnosis is already in the logs.
    """

    def __init__(self, webhook_url, timeout=10):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, finding):
        payload = json.dumps(self._blocks(finding)).encode()
        request = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    log.warning("slack_rejected", extra={"status": response.status})
        except (urllib.error.URLError, OSError) as exc:
            log.warning("slack_delivery_failed", extra={"error": str(exc)})

    def _blocks(self, finding):
        icon = _ICON.get(finding["status"], ":warning:")
        scope = finding["workload"] or finding["pod"]

        header = f"{icon} *{scope}* is unhealthy in `{finding['namespace']}`"
        if finding["replicas"] > 1:
            header += f" — {finding['replicas']} pods affected"

        context = f"{finding['status']} · {finding['confidence']}"
        if finding["unverified"]:
            context += f" · unverified: {', '.join(finding['unverified'])}"

        return {
            "text": f"{scope} is unhealthy in {finding['namespace']}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": header}},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _fit(finding["diagnosis"])},
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": context}],
                },
            ],
        }


def format_text(finding):
    scope = finding["workload"] or finding["pod"]
    lines = [
        f"[{finding['status']}] {scope} in {finding['namespace']}"
        + (f" ({finding['replicas']} pods)" if finding["replicas"] > 1 else ""),
        finding["diagnosis"],
        f"  -- {finding['confidence']}"
        + (
            f", unverified: {', '.join(finding['unverified'])}"
            if finding["unverified"]
            else ""
        ),
    ]
    return "\n".join(lines) + "\n"


def build(name=None, webhook_url=None):
    """Pick a sink from configuration, falling back to stdout."""
    name = (name or os.getenv("TRIAGE_SINK", "stdout")).lower()
    webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")

    if name == "slack":
        if not webhook_url:
            log.warning("slack_sink_requested_without_url falling back to stdout")
            return StdoutSink()
        return SlackSink(webhook_url)

    return StdoutSink()
