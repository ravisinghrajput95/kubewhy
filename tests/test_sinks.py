"""
Tests for delivery.

This is the least observed surface in the project: the controller runs
unattended, so a malformed or silently truncated message is not noticed by
anyone. Slack also rejects a section block over 3000 characters outright, which
turns a formatting mistake into no alert at all -- exactly when an alert is the
point.
"""

import json
from unittest.mock import MagicMock, patch

import sinks


def finding(diagnosis="the pod exceeded its 64Mi memory limit", **overrides):
    base = {
        "pod": "memory-hog-abc",
        "namespace": "demo",
        "workload": "memory-hog",
        "status": "OOMKilled",
        "replicas": 1,
        "diagnosis": diagnosis,
        "confidence": "grounded",
        "unverified": [],
        "tool_calls": ["describe_pod"],
    }
    return {**base, **overrides}


class TestSlackLimit:
    def test_a_short_diagnosis_is_untouched(self):
        assert sinks._fit("all fine") == "all fine"

    def test_a_long_diagnosis_stays_within_the_block_limit(self):
        """Over 3000 characters Slack rejects the block and nothing arrives."""
        fitted = sinks._fit("word " * 2000)

        assert len(fitted) <= sinks._SLACK_LIMIT

    def test_truncation_is_admitted(self):
        """
        A hard slice cut mid-sentence and said nothing, so a truncated answer
        read as a complete one that stopped making sense.
        """
        fitted = sinks._fit("word " * 2000)

        assert "truncated" in fitted

    def test_it_cuts_at_a_paragraph_boundary_when_one_is_near(self):
        # The break sits late enough to keep most of the message, so the cut
        # should land on it rather than mid-word.
        text = ("a" * 2000) + "\n\n" + ("b" * 2000)
        fitted = sinks._fit(text)

        assert "b" not in fitted
        assert fitted.startswith("a" * 2000)

    def test_a_boundary_that_would_discard_most_of_the_message_is_ignored(self):
        """Cutting back to an early full stop would throw the answer away."""
        text = "Short. " + ("y" * 3000)
        fitted = sinks._fit(text)

        # Kept most of the available room rather than collapsing to "Short."
        assert len(fitted) > sinks._SLACK_LIMIT * 0.8

    def test_the_block_payload_is_bounded(self):
        sink = sinks.SlackSink("https://hooks.example.invalid/x")
        blocks = sink._blocks(finding(diagnosis="z" * 5000))

        body = blocks["blocks"][1]["text"]["text"]
        assert len(body) <= sinks._SLACK_LIMIT


class TestFormatting:
    def test_replica_count_is_only_shown_when_it_matters(self):
        assert "pods" not in sinks.format_text(finding(replicas=1))
        assert "3 pods" in sinks.format_text(finding(replicas=3))

    def test_unverified_claims_travel_with_the_answer(self):
        """A diagnosis delivered without its caveats is worse than none."""
        text = sinks.format_text(
            finding(confidence="partial", unverified=["18", "oomkilled"])
        )

        assert "partial" in text
        assert "18" in text

    def test_slack_context_carries_confidence(self):
        sink = sinks.SlackSink("https://hooks.example.invalid/x")
        blocks = sink._blocks(finding(confidence="partial", unverified=["42"]))

        context = blocks["blocks"][2]["elements"][0]["text"]
        assert "partial" in context and "42" in context

    def test_a_pod_with_no_workload_still_names_something(self):
        blocks = sinks.SlackSink("https://x.invalid")._blocks(
            finding(workload=None)
        )
        assert "memory-hog-abc" in blocks["text"]


class TestDeliveryFailuresAreSurvivable:
    def test_a_broken_webhook_does_not_raise(self):
        """
        The controller must outlive its sink. A delivery failure that escapes
        kills the watch loop, so one bad webhook stops all diagnosis.
        """
        sink = sinks.SlackSink("https://hooks.example.invalid/x")

        with patch("urllib.request.urlopen", side_effect=OSError("no route")):
            sink.send(finding())  # must not raise

    def test_a_non_200_is_logged_not_raised(self):
        sink = sinks.SlackSink("https://hooks.example.invalid/x")
        response = MagicMock()
        response.status = 500
        response.__enter__ = lambda self: response
        response.__exit__ = lambda *a: False

        with patch("urllib.request.urlopen", return_value=response):
            sink.send(finding())

    def test_the_payload_is_valid_json(self):
        sink = sinks.SlackSink("https://hooks.example.invalid/x")
        captured = {}

        def capture(request, timeout=None, **kwargs):
            captured["body"] = request.data
            response = MagicMock()
            response.status = 200
            response.__enter__ = lambda self: response
            response.__exit__ = lambda *a: False
            return response

        with patch("urllib.request.urlopen", side_effect=capture):
            sink.send(finding())

        assert json.loads(captured["body"])["blocks"]


class TestSinkSelection:
    def test_slack_without_a_url_falls_back_rather_than_failing(self):
        """Misconfiguration should degrade to stdout, not silence."""
        assert isinstance(sinks.build("slack", ""), sinks.StdoutSink)

    def test_slack_with_a_url(self):
        assert isinstance(
            sinks.build("slack", "https://hooks.example.invalid/x"), sinks.SlackSink
        )

    def test_default_is_stdout(self):
        assert isinstance(sinks.build(None, ""), sinks.StdoutSink)


class TestSlackApiSink:
    """
    Bot token via chat.postMessage. A webhook is bound to the channel it was
    created for; a token can post anywhere it is invited, which is what makes
    routing by namespace possible later.
    """

    def test_posts_to_the_named_channel_with_a_bearer_token(self):
        sink = sinks.SlackApiSink("xoxb-not-a-real-token", "#kubernetes-events")
        captured = {}

        def capture(request, timeout=None, **kwargs):
            captured["headers"] = request.headers
            captured["body"] = json.loads(request.data)
            response = MagicMock()
            response.read = lambda: b'{"ok": true}'
            response.__enter__ = lambda self: response
            response.__exit__ = lambda *a: False
            return response

        with patch("urllib.request.urlopen", side_effect=capture):
            sink.send(finding())

        assert captured["body"]["channel"] == "#kubernetes-events"
        assert captured["headers"]["Authorization"].startswith("Bearer ")
        assert captured["body"]["blocks"]

    def test_a_refusal_is_noticed_even_though_slack_answers_200(self):
        """
        chat.postMessage returns HTTP 200 with {"ok": false} for a bad channel
        or a missing invite. Trusting the status code makes that look like a
        successful post and the alert is simply never seen.
        """
        sink = sinks.SlackApiSink("xoxb-not-a-real-token", "#nope")
        response = MagicMock()
        response.read = lambda: b'{"ok": false, "error": "channel_not_found"}'
        response.__enter__ = lambda self: response
        response.__exit__ = lambda *a: False

        with patch("urllib.request.urlopen", return_value=response):
            with patch.object(sinks.log, "warning") as warned:
                sink.send(finding())

        assert warned.called
        assert warned.call_args.kwargs["extra"]["error"] == "channel_not_found"

    def test_delivery_failure_does_not_raise(self):
        sink = sinks.SlackApiSink("xoxb-not-a-real-token", "#x")
        with patch("urllib.request.urlopen", side_effect=OSError("no route")):
            sink.send(finding())

    def test_a_bot_token_is_preferred_over_a_webhook(self):
        """Someone who configured both meant the more capable one."""
        built = sinks.build(
            "slack",
            webhook_url="https://hooks.slack.com/services/x",
            token="xoxb-not-a-real-token",
            channel="#kubernetes-events",
        )
        assert isinstance(built, sinks.SlackApiSink)

    def test_a_token_without_a_channel_falls_back_rather_than_guessing(self):
        built = sinks.build("slack", token="xoxb-not-a-real-token", channel="")
        assert isinstance(built, sinks.StdoutSink)
