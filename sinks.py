"""
Where a finished diagnosis goes.

Kept deliberately dumb: a sink formats and delivers, and never decides whether
something is worth sending. That decision belongs to the controller, which has
the dedup and rate-limit state.
"""

import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.request

log = logging.getLogger("triage.sink")


def _tls_context():
    """
    A verified TLS context that also works on a developer's laptop.

    The container image has a system CA store and needs none of this. A
    python.org build on macOS does not, so every Slack post failed there with
    CERTIFICATE_VERIFY_FAILED while working fine in the cluster -- the worst
    shape of bug, since it only appears where nobody is watching for it.

    certifi arrives with the kubernetes client, so this is not a new
    dependency; falling back to the system store keeps it optional rather than
    required.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# Slack rejects a section block over 3000 characters, so the diagnosis has to
# be bounded. Leaving room for the marker below.
_SLACK_LIMIT = 2900


# Fenced blocks and inline code, so emphasis inside them is left alone. A
# diagnosis quotes YAML and log lines, and `**` inside a code span is text.
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)

# `**bold**` and `__bold__`, Markdown's two spellings. Non-greedy, and the
# lookarounds stop it spanning `** a ** b **` or eating an empty `****`.
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)

# `### Root Cause` at the start of a line. Slack has no headings, and left
# alone the hashes are visible -- seen in a real diagnosis on 2026-09-04.
# Bold is what a heading means here.
_HEADING = re.compile(r"^\s{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*\s*$", re.MULTILINE)

# A line that is only dashes, asterisks or underscores: Markdown's horizontal
# rule. Slack has none, so `---` arrives as three dashes on their own line.
_RULE = re.compile(r"^\s{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$\n?", re.MULTILINE)


def _mrkdwn(text):
    """
    Markdown as a model writes it, in the dialect Slack renders.

    Slack's `mrkdwn` is not Markdown. Bold is `*one*` asterisk, not two, so
    `**payments/archiver**` arrives as literal asterisks around the name --
    seen in #kubernetes-events on 2026-09-04, in a diagnosis that was
    otherwise correct and grounded. The blocks already declare `mrkdwn`;
    nothing was translating into it.

    Bold and headings, and nothing else. They are the constructs models emit
    constantly which Slack renders *wrong* rather than merely unstyled: `-`
    bullets, `1.` lists and backticks already mean what Markdown means, while
    `**bold**` shows its asterisks and `### Root Cause` shows its hashes. Both
    were seen in real diagnoses on 2026-09-04. A heading becomes bold, which
    is what a heading means in a channel.

    Horizontal rules (`---`) are dropped: Slack has no rule, and a line of
    three dashes in the middle of a diagnosis reads as a typo.

    `[text](url)` links are still passed through and show their target.
    Converting them needs the same care as the rest of a real parser, and
    unlike bold and headings the failure is ugly rather than misleading.

    Code is protected: `**` inside a fence or a code span is text.
    """
    def heading(m):
        # Emphasis inside a heading is flattened, not converted. The whole
        # line becomes bold, and `### ✅ **Key Findings**` run through bold
        # first then wrapped gives `*✅ *Key Findings**` -- seen in a real
        # answer on 2026-09-04, from the first version of this function.
        return "*" + _BOLD.sub(r"\2", m.group(1)).strip() + "*"

    def convert(chunk):
        # Headings first, and they consume their own emphasis, so the bold
        # pass below cannot reach inside one and nest asterisks.
        return _BOLD.sub(r"*\2*", _RULE.sub("", _HEADING.sub(heading, chunk)))

    out, last = [], 0
    for m in _CODE.finditer(text):
        out.append(convert(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(convert(text[last:]))
    return "".join(out)


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
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=_tls_context()
            ) as response:
                if response.status != 200:
                    log.warning("slack_rejected", extra={"status": response.status})
        except (urllib.error.URLError, OSError) as exc:
            log.warning("slack_delivery_failed", extra={"error": str(exc)})

    def _blocks(self, finding):
        if finding.get("kind") == "answer":
            return _answer_blocks_for(finding)
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
                    "text": {"type": "mrkdwn", "text": _fit(_mrkdwn(finding["diagnosis"]))},
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": context}],
                },
            ],
        }


def _answer_blocks_for(answer):
    """
    A reply to a question, which is not a finding about a workload.

    The bot sent answers through the finding renderer with the question in the
    `workload` key, so every reply was headed

        :warning: *why is checkout failing in payments?* is unhealthy in ``

    -- the question read as a broken workload, and the empty namespace
    rendered as empty backticks. Seen in #kubernetes-events on 2026-09-04
    above an answer that was correct and grounded.

    A separate renderer rather than a special case inside the finding one: the
    controller reports findings *about* workloads and the bot answers
    questions, and the two have different things to say in a header. Sharing
    one renderer is what produced the defect.
    """
    context = answer["confidence"]
    if answer["unverified"]:
        context += f" · unverified: {', '.join(answer['unverified'])}"
    return {
        "text": answer["question"],
        "blocks": [
            {"type": "section",
             "text": {"type": "mrkdwn", "text": f":mag: *{answer['question']}*"}},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": _fit(_mrkdwn(answer["answer"]))}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
        ],
    }


class SlackApiSink(SlackSink):
    """
    Posts with a bot token via chat.postMessage.

    A webhook is bound to one channel chosen when it was created; a bot token
    can post anywhere it has been invited, which is what makes routing by
    namespace or team possible later. It also returns a real error body, so a
    misconfiguration says what is wrong instead of failing silently.

    The signing secret is not used here: it verifies requests coming *from*
    Slack, which is slack_events.py's job. That surface is opt-in and separate
    precisely because it means exposing an endpoint to the internet.
    """

    API = "https://slack.com/api/chat.postMessage"

    def __init__(self, token, channel, timeout=10):
        super().__init__(webhook_url=self.API, timeout=timeout)
        self.token = token
        self.channel = channel

    def send(self, finding):
        body = self._blocks(finding)
        body["channel"] = self.channel

        # Answers belong under the question. Only set when the caller knows a
        # thread -- the controller posts unprompted and has none, and passing
        # an empty thread_ts makes chat.postMessage reject the whole message.
        if finding.get("thread_ts"):
            body["thread_ts"] = finding["thread_ts"]

        request = urllib.request.Request(
            self.API,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=_tls_context()
            ) as response:
                # chat.postMessage answers 200 even when it refuses; the truth
                # is in the body, so a channel typo or a missing invite would
                # otherwise look like a successful post.
                payload = json.loads(response.read() or "{}")
                if not payload.get("ok"):
                    log.warning(
                        "slack_rejected", extra={"error": payload.get("error")}
                    )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("slack_delivery_failed", extra={"error": str(exc)})


def format_text(finding):
    if finding.get("kind") == "answer":
        # Same split as the Slack renderer, for the same reason: stdout is
        # what you read while trying the bot out, and "[] <question> in "
        # reads as a broken parser.
        tail = (f"  -- {finding['confidence']}"
                + (f", unverified: {', '.join(finding['unverified'])}"
                   if finding["unverified"] else ""))
        return "\n".join([f"Q: {finding['question']}",
                           finding["answer"], tail]) + "\n"
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


def build(name=None, webhook_url=None, token=None, channel=None):
    """
    Pick a sink from configuration, falling back to stdout.

    A bot token wins over a webhook when both are set: it is the more capable
    of the two and the one someone configuring both almost certainly meant.
    Credentials come from the environment only -- never an argument default,
    never a file in this repo.
    """
    name = (name or os.getenv("TRIAGE_SINK", "stdout")).lower()
    webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")
    token = token or os.getenv("SLACK_BOT_TOKEN", "")
    channel = channel or os.getenv("SLACK_CHANNEL", "")

    if name == "slack":
        if token and channel:
            return SlackApiSink(token, channel)
        if token and not channel:
            log.warning("slack_bot_token_without_channel falling back to stdout")
            return StdoutSink()
        if not webhook_url:
            log.warning("slack_sink_requested_without_url falling back to stdout")
            return StdoutSink()
        return SlackSink(webhook_url)

    return StdoutSink()
