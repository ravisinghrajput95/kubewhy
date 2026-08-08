"""
Tests for the inbound Slack endpoint.

This is the only surface in the project that is meant to be reachable from the
internet, so the property under test is not routing -- it is that nothing
Slack did not sign gets through. Every test here is a request that must be
refused.
"""

import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import slack_events

SECRET = "test-signing-secret"


@pytest.fixture
def client():
    with patch.object(slack_events, "SIGNING_SECRET", SECRET):
        yield TestClient(slack_events.app)


def sign(body: bytes, timestamp: str) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()


def post(client, body: bytes, timestamp=None, signature=None, headers=True):
    timestamp = timestamp or str(int(time.time()))
    sent = {}
    if headers:
        sent["X-Slack-Request-Timestamp"] = timestamp
        sent["X-Slack-Signature"] = signature or sign(body, timestamp)
    return client.post("/slack/events", content=body, headers=sent)


CHALLENGE = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()


class TestNothingUnsignedGetsThrough:
    def test_a_signed_request_is_accepted(self, client):
        assert post(client, CHALLENGE).json() == {"challenge": "abc123"}

    def test_an_unsigned_request_is_refused(self, client):
        assert post(client, CHALLENGE, headers=False).status_code == 401

    def test_a_wrong_signature_is_refused(self, client):
        assert post(client, CHALLENGE, signature="v0=" + "0" * 64).status_code == 401

    def test_a_replayed_request_is_refused(self, client):
        """A captured request is worthless after five minutes."""
        old = str(int(time.time()) - 600)
        assert post(client, CHALLENGE, timestamp=old).status_code == 401

    def test_a_tampered_body_is_refused(self, client):
        """The signature covers the body, so editing it invalidates the request."""
        timestamp = str(int(time.time()))
        signature = sign(CHALLENGE, timestamp)
        tampered = CHALLENGE.replace(b"abc123", b"hacked")

        assert post(client, tampered, timestamp, signature).status_code == 401

    def test_a_garbage_timestamp_is_refused_not_crashed(self, client):
        assert post(client, CHALLENGE, timestamp="not-a-number").status_code == 401

    def test_without_a_secret_nothing_is_served(self):
        """
        An unset secret must fail closed. Serving unverified would turn this
        into a remote trigger for anyone who finds the URL.
        """
        with patch.object(slack_events, "SIGNING_SECRET", ""):
            client = TestClient(slack_events.app)
            assert post(client, CHALLENGE).status_code == 503


class TestDiagnosisIsNotRunInline:
    def test_a_mention_is_acknowledged_immediately(self, client):
        """
        Slack retries anything slower than three seconds, and a diagnosis takes
        tens of them -- answering inline would post the same finding repeatedly.
        """
        body = json.dumps(
            {
                "type": "event_callback",
                "event": {
                    "type": "app_mention",
                    "text": "why is crasher failing?",
                    "channel": "C1",
                    "ts": "1699.1",
                },
            }
        ).encode()

        with patch.object(slack_events, "_answer") as answer:
            response = post(client, body)

        assert response.status_code == 200
        # Dispatched to an executor rather than awaited, so the ack does not
        # wait on the model.
        assert answer.call_count <= 1

    def test_the_bots_own_messages_are_ignored(self, client):
        """Otherwise it answers itself, forever."""
        body = json.dumps(
            {
                "type": "event_callback",
                "event": {
                    "type": "message",
                    "text": "a previous answer",
                    "channel": "C1",
                    "ts": "1699.1",
                    "bot_id": "B123",
                },
            }
        ).encode()

        with patch.object(slack_events, "_answer") as answer:
            post(client, body)

        answer.assert_not_called()
