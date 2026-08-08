"""
Inbound Slack over a WebSocket the process opens itself.

Socket Mode inverts the direction: instead of Slack calling a URL you had to
publish, this dials out to Slack and events arrive down the connection. For a
tool whose whole claim is that nothing leaves your network, that is the better
shape by some distance -- there is no public hostname, no tunnel, no inbound
listener, and no signature to verify, because nothing unauthenticated can
reach the process in the first place.

It also removes the reason this feature sat in the backlog. Accepting replies
and buttons no longer requires deciding to expose anything.

    export SLACK_APP_TOKEN=xapp-...   # Basic Information -> App-Level Tokens
    export SLACK_BOT_TOKEN=xoxb-...   # OAuth & Permissions
    export SLACK_CHANNEL='#kubernetes-events'
    python slack_socket.py

The app-level token needs the connections:write scope, and the app needs
app_mention under Event Subscriptions -> Subscribe to bot events. Without that
scope the socket connects and no event ever arrives, which looks identical to
a broken integration.
"""

import logging
import os
import threading

from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web import WebClient

import agent
import observability
import sinks

observability.configure()
log = logging.getLogger("triage.slack")

APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
CHANNEL = os.getenv("SLACK_CHANNEL", "")


def answer(question, channel, thread_ts):
    """
    Diagnose, then post the result back into the thread that asked.

    Runs on its own thread. Slack expects the socket acknowledged in seconds
    and a diagnosis takes tens of them, so doing this inline would stall the
    connection and earn a redelivery of the same event.
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

    sinks.build(name="slack", channel=channel or CHANNEL).send(
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


def strip_mention(text):
    """
    Drop the <@U123> the mention arrives with.

    Left in, it reaches the model as part of the question, and a user id is
    exactly the kind of incidental token that reads like something to look up.
    """
    return " ".join(word for word in text.split() if not word.startswith("<@")).strip()


def handle(client, request: SocketModeRequest):
    """
    One envelope. Acknowledge first, always, then decide whether to work.

    Acknowledging before the work rather than after is the whole contract:
    Slack redelivers anything unacknowledged, and a redelivered diagnosis is a
    second identical answer in the channel.
    """
    client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))

    if request.type != "events_api":
        return

    event = (request.payload or {}).get("event", {})
    if event.get("type") not in ("app_mention", "message"):
        return

    # Its own messages come back down the socket. Answering them is a loop.
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    question = strip_mention(event.get("text", ""))
    if not question:
        return

    channel = event.get("channel", "")
    thread = event.get("thread_ts") or event.get("ts")
    log.info("slack_question", extra={"channel": channel})

    threading.Thread(
        target=answer, args=(question, channel, thread), daemon=True
    ).start()


def build_client():
    if not APP_TOKEN.startswith("xapp-"):
        raise SystemExit(
            "SLACK_APP_TOKEN must be an app-level token (xapp-...), from "
            "Basic Information -> App-Level Tokens with connections:write.\n"
            "A bot token (xoxb-...) will not open a socket."
        )
    if not BOT_TOKEN:
        raise SystemExit("SLACK_BOT_TOKEN is unset: answers would have nowhere to go.")

    client = SocketModeClient(app_token=APP_TOKEN, web_client=WebClient(token=BOT_TOKEN))
    client.socket_mode_request_listeners.append(handle)
    return client


def main():
    client = build_client()
    client.connect()
    log.info("slack_socket_connected", extra={"channel": CHANNEL})
    print("listening on the Slack socket; mention the bot in a channel", flush=True)

    # connect() returns once the socket is up; the listener runs on its own
    # thread, so the main thread has to stay alive for it.
    threading.Event().wait()


if __name__ == "__main__":
    main()
