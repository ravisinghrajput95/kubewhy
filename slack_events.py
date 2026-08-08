"""
The inbound half of Slack, kept deliberately separate from the diagnostic API.

The controller posts and nothing reads back. Replies, buttons and slash
commands need an endpoint Slack can reach, which means putting something on the
internet -- so this is its own app on its own port rather than routes bolted
onto app.py. Tunnel this and only this: app.py serves pod logs and node
inventory, and exposing that to reach a button is not a trade worth making.

    export SLACK_SIGNING_SECRET=...        # Basic Information -> App Credentials
    export SLACK_BOT_TOKEN=xoxb-...        # to post answers back
    fastapi run slack_events.py --port 8790

    cloudflared tunnel --url http://127.0.0.1:8790

Point the app's Event Subscriptions and Interactivity request URLs at the
generated https address plus /slack/events and /slack/interactive.

Every request is verified against the signing secret before anything reads its
body. That verification is the entire security boundary here: the URL is
public, guessable by anyone watching certificate transparency logs, and an
unverified endpoint is an open invitation to run diagnoses on your cluster.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time

from fastapi import FastAPI, Header, HTTPException, Request

import agent
import observability
import sinks

observability.configure()
log = logging.getLogger("triage.slack")

SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")

# Slack's own recommendation. A captured request replayed after this window is
# refused, which is the difference between a signature and a bearer token.
MAX_SKEW_SECONDS = 60 * 5

app = FastAPI(
    title="kubewhy Slack endpoint",
    description=__doc__,
    version="0",
)


def verify(body: bytes, timestamp: str, signature: str):
    """
    Reject anything Slack did not sign.

    Raises rather than returning False: there is no path through this module
    that should continue on a failed check, and a boolean invites one.
    """
    if not SIGNING_SECRET:
        # Refusing to start would be worse -- the process also serves health --
        # but answering requests unverified would make this an open remote
        # trigger for anyone who finds the URL.
        log.error("slack_signing_secret_unset refusing request")
        raise HTTPException(503, "SLACK_SIGNING_SECRET is unset")

    if not timestamp or not signature:
        raise HTTPException(401, "unsigned request")

    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        raise HTTPException(401, "bad timestamp") from None

    if age > MAX_SKEW_SECONDS:
        raise HTTPException(401, "stale request")

    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(
        SIGNING_SECRET.encode(), base, hashlib.sha256
    ).hexdigest()

    # compare_digest, not ==: a byte-by-byte comparison leaks the prefix length
    # through timing, and the attacker controls how many guesses they get.
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "bad signature")


async def _verified_body(request: Request, timestamp: str, signature: str) -> bytes:
    body = await request.body()
    verify(body, timestamp, signature)
    return body


def _answer(question: str, channel: str, thread_ts: str | None):
    """
    Run a diagnosis and post the result. Called in the background, never inline.

    Slack retries anything that does not answer within three seconds, and a
    diagnosis takes tens of seconds -- answering inline would deliver the same
    finding three times and still time out.
    """
    try:
        result = agent.ask(question)
    except Exception as exc:  # noqa: BLE001 - a failed diagnosis must still reply
        log.exception("slack_diagnosis_failed")
        result = {
            "answer": f"Could not answer that: {exc}",
            "confidence": "ungrounded",
            "unverified": [],
        }

    sinks.build(name="slack", channel=channel).send(
        {
            "workload": question[:80],
            "namespace": "",
            "pods": 0,
            "status": "",
            "diagnosis": result["answer"],
            "confidence": result.get("confidence", "ungrounded"),
            "unverified": result.get("unverified", []),
            "thread_ts": thread_ts,
        }
    )


@app.get("/healthz", tags=["meta"])
def healthz():
    """Liveness. Deliberately unsigned, and says nothing about the cluster."""
    return {"status": "ok", "signing_secret": bool(SIGNING_SECRET)}


@app.post("/slack/events", tags=["slack"])
async def events(
    request: Request,
    x_slack_request_timestamp: str = Header(default=""),
    x_slack_signature: str = Header(default=""),
):
    """
    The Events API: URL verification, then mentions.

    Answers immediately and diagnoses afterwards, because Slack's three-second
    budget is shorter than any diagnosis this tool produces.
    """
    body = await _verified_body(request, x_slack_request_timestamp, x_slack_signature)
    payload = json.loads(body or b"{}")

    # The one-time handshake when you paste the URL into the app config. It is
    # signed like everything else, so it is verified before we echo anything.
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    event = payload.get("event", {})
    if event.get("type") in ("app_mention", "message") and not event.get("bot_id"):
        question = event.get("text", "")
        channel = event.get("channel", "")
        # Reply in the thread that asked, so a busy channel does not scatter
        # answers away from their questions.
        thread = event.get("thread_ts") or event.get("ts")
        if question and channel:
            asyncio.get_running_loop().run_in_executor(
                None, _answer, question, channel, thread
            )

    return {"ok": True}


@app.post("/slack/interactive", tags=["slack"])
async def interactive(
    request: Request,
    x_slack_request_timestamp: str = Header(default=""),
    x_slack_signature: str = Header(default=""),
):
    """
    Button clicks. Form-encoded with the payload as a JSON string, not JSON.

    Signature verification runs over the raw body either way, which is why the
    body is read once as bytes and parsed after.
    """
    body = await _verified_body(request, x_slack_request_timestamp, x_slack_signature)

    from urllib.parse import parse_qs

    raw = parse_qs(body.decode()).get("payload", ["{}"])[0]
    payload = json.loads(raw)

    actions = payload.get("actions") or [{}]
    action = actions[0].get("action_id", "")
    channel = (payload.get("channel") or {}).get("id", "")
    message = payload.get("message") or {}
    thread = message.get("thread_ts") or message.get("ts")

    log.info("slack_action", extra={"action": action})

    if action == "rediagnose" and channel:
        value = actions[0].get("value", "")
        asyncio.get_running_loop().run_in_executor(
            None, _answer, f"why is {value} failing?", channel, thread
        )

    # An empty 200 leaves the original message untouched, which is what you
    # want for an acknowledgement.
    return {}
