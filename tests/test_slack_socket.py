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

import sinks
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


class TestAnAnswerActuallyReachesTheSink:
    """
    `answer()` was never run against a real sink by any test -- every case
    patched it out or patched `sinks.build` to a mock, so nothing ever
    consumed the finding it builds.

    It did not work. The dict carried a `"pods"` key while every writer in
    `sinks.py` reads `finding["replicas"]` by subscript, so posting any Slack
    answer raised `KeyError: 'replicas'`. It ran on the answering thread,
    where the traceback goes to the thread excepthook and the person who
    asked simply never gets a reply. Found by reading a mutation survivor on
    that line -- the mutant changed a value nothing consumed, which is what
    made the key wrong rather than the number.

    These go through `sinks.StdoutSink` on purpose: a mock would pass again.
    """

    def _delivered(self, question="why is checkout broken?", channel="C1",
                   thread="1.1", user="U9"):
        captured = {}

        class Spy(sinks.StdoutSink):
            def send(self, finding):
                captured["finding"] = finding
                return super().send(finding)

        with patch.object(slack_socket.agent, "ask", return_value={
                "answer": "the pod exceeded its 64Mi memory limit",
                "confidence": "grounded", "unverified": []}), \
             patch.object(slack_socket.sinks, "build", return_value=Spy()):
            slack_socket.answer(question, channel, thread, user)

        return captured["finding"]

    def test_an_answer_is_delivered_rather_than_raising(self):
        assert "memory limit" in self._delivered()["answer"]

    def test_the_payload_carries_every_key_the_sinks_subscript(self):
        """
        The general form of the defect: `sinks` reads these by subscript, so a
        missing one is a KeyError at delivery rather than a blank field.

        The key set changed on 2026-09-04 when answers stopped being sent as
        findings -- the question used to travel in `workload` and every reply
        was headed "*<the question>* is unhealthy in ``". The assertion is the
        same one: whatever shape this sends, the renderer that receives it must
        find every key it subscripts.
        """
        payload = self._delivered()

        assert payload["kind"] == "answer"
        for key in ("question", "answer", "confidence", "unverified"):
            assert key in payload, key

    def test_it_is_not_sent_as_a_finding_about_a_broken_workload(self):
        """The defect itself: a question is not a workload."""
        payload = self._delivered()

        assert "workload" not in payload
        assert "is unhealthy" not in sinks.format_text(payload)

    def test_a_single_question_is_not_announced_as_several_pods(self):
        assert sinks.format_text(self._delivered()).count("pods") == 0


class TestTheAnswerIsAttributedAndRouted:
    """
    Three `or` fallbacks on this path all survived mutation, and each one
    fails the same way: it discards the real value and keeps the fallback.

    As `and`, `user or "unknown-slack-user"` attributes **every** Slack
    investigation to the placeholder, which is precisely the join key the
    audit trail exists to provide; `channel or CHANNEL` sends every reply to
    the configured default channel instead of the one that asked; and
    `thread_ts or ts` starts a new thread rather than replying in the one the
    question is in.
    """

    def test_the_audit_actor_is_the_slack_user_who_asked(self):
        with patch.object(slack_socket.audit, "actor") as actor, \
             patch.object(slack_socket.agent, "ask", return_value={
                 "answer": "x", "confidence": "grounded", "unverified": []}), \
             patch.object(slack_socket.sinks, "build"):
            slack_socket.answer("q", "C1", "1.1", "U0FFICE")

        assert actor.call_args.args[0] == "U0FFICE"

    def test_an_anonymous_event_still_records_an_actor(self):
        """The counter: the fallback has to remain reachable."""
        with patch.object(slack_socket.audit, "actor") as actor, \
             patch.object(slack_socket.agent, "ask", return_value={
                 "answer": "x", "confidence": "grounded", "unverified": []}), \
             patch.object(slack_socket.sinks, "build"):
            slack_socket.answer("q", "C1", "1.1", "")

        assert actor.call_args.args[0] == "unknown-slack-user"

    def test_the_reply_goes_to_the_channel_that_asked(self):
        with patch.object(slack_socket.agent, "ask", return_value={
                 "answer": "x", "confidence": "grounded", "unverified": []}), \
             patch.object(slack_socket.sinks, "build") as build:
            slack_socket.answer("q", "C-ASKED-HERE", "1.1", "U9")

        assert build.call_args.kwargs["channel"] == "C-ASKED-HERE"

    def test_a_reply_lands_in_the_thread_that_asked_not_a_new_one(self):
        client = MagicMock()
        started = []

        with patch.object(slack_socket.threading, "Thread") as thread:
            thread.side_effect = lambda **kw: started.append(kw) or MagicMock()
            slack_socket.handle(client, request(
                mention(ts="2.2", thread_ts="1.1")))

        assert started[0]["args"][2] == "1.1"

    def test_a_question_outside_a_thread_starts_one_at_its_own_timestamp(self):
        """The counter: `ts` is still the fallback when there is no thread."""
        client = MagicMock()
        started = []

        with patch.object(slack_socket.threading, "Thread") as thread:
            thread.side_effect = lambda **kw: started.append(kw) or MagicMock()
            slack_socket.handle(client, request(mention(ts="2.2")))

        assert started[0]["args"][2] == "2.2"

    def test_the_answering_thread_is_a_daemon(self):
        """A non-daemon answer thread keeps the process alive after shutdown."""
        client = MagicMock()
        started = []

        with patch.object(slack_socket.threading, "Thread") as thread:
            thread.side_effect = lambda **kw: started.append(kw) or MagicMock()
            slack_socket.handle(client, request(mention()))

        assert started[0]["daemon"] is True
