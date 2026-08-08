"""
Tests for the Socket Mode listener.

No signature checks to test here, which is the point of the transport: the
socket is outbound and authenticated by the app-level token, so nothing
unauthenticated can reach the process. What is worth pinning down is the
acknowledgement contract and the loop.
"""

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("slack_sdk")

import slack_socket
from slack_sdk.socket_mode.request import SocketModeRequest


def request(event, kind="events_api"):
    return SocketModeRequest(
        type=kind, envelope_id="env-1", payload={"event": event} if event else {}
    )


def mention(text="<@U123> why is crasher failing?", **extra):
    return {"type": "app_mention", "text": text, "channel": "C1", "ts": "1.1", **extra}


class TestAcknowledgement:
    def test_every_envelope_is_acknowledged(self):
        """Slack redelivers anything unacknowledged, and a redelivered
        diagnosis is the same answer posted twice."""
        client = MagicMock()
        with patch.object(slack_socket, "answer"):
            slack_socket.handle(client, request(mention()))

        assert client.send_socket_mode_response.call_count == 1

    def test_acknowledged_even_when_ignored(self):
        client = MagicMock()
        slack_socket.handle(client, request({"type": "reaction_added"}))

        client.send_socket_mode_response.assert_called_once()

    def test_the_diagnosis_does_not_block_the_socket(self):
        """Answering inline would stall the connection for the whole run."""
        client = MagicMock()
        started = []
        with patch.object(slack_socket.threading, "Thread") as thread:
            thread.side_effect = lambda **kw: started.append(kw) or MagicMock()
            slack_socket.handle(client, request(mention()))

        assert started and started[0]["target"] is slack_socket.answer


class TestWhatItAnswers:
    def test_its_own_messages_are_ignored(self):
        """Otherwise it answers itself, forever."""
        client = MagicMock()
        with patch.object(slack_socket, "answer") as answer:
            slack_socket.handle(client, request(mention(bot_id="B1")))
            slack_socket.handle(client, request(mention(subtype="bot_message")))

        answer.assert_not_called()

    def test_non_event_envelopes_are_ignored(self):
        client = MagicMock()
        with patch.object(slack_socket, "answer") as answer:
            slack_socket.handle(client, request(mention(), kind="slash_commands"))

        answer.assert_not_called()

    def test_an_empty_question_is_not_asked(self):
        client = MagicMock()
        with patch.object(slack_socket, "answer") as answer:
            slack_socket.handle(client, request(mention(text="<@U123>")))

        answer.assert_not_called()

    def test_the_mention_is_stripped_from_the_question(self):
        """The user id would otherwise reach the model as part of the question,
        looking like something to go and look up."""
        assert slack_socket.strip_mention("<@U123> why is crasher failing?") == (
            "why is crasher failing?"
        )


class TestConfiguration:
    def test_a_bot_token_cannot_open_a_socket(self):
        """The two tokens are easy to confuse and the failure is opaque."""
        with patch.object(slack_socket, "APP_TOKEN", "xoxb-not-an-app-token"):
            with pytest.raises(SystemExit) as exit_:
                slack_socket.build_client()

        assert "xapp-" in str(exit_.value)

    def test_answers_need_somewhere_to_go(self):
        with patch.object(slack_socket, "APP_TOKEN", "xapp-ok"), patch.object(
            slack_socket, "BOT_TOKEN", ""
        ):
            with pytest.raises(SystemExit):
                slack_socket.build_client()
