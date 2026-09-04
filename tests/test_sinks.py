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


class TestTheReplicaCountBoundary:
    """
    `replicas > 1` decides whether a finding says how many pods are affected,
    and the Slack blocks and the text sink each make that call separately.

    Mutation testing found the boundary open in three places at once. The
    existing case pairs one pod against three and never asks about **two**, so
    relaxing `> 1` to `> 2` survived in both writers -- a two-pod incident
    would have been reported as if it were one. `>` to `>=` survived in
    `_blocks` as well, which had no case asserting that a single pod stays
    quiet; only `format_text` did.
    """

    def test_two_pods_are_named_in_the_text_sink(self):
        assert "2 pods" in sinks.format_text(finding(replicas=2))

    def test_two_pods_are_named_in_the_slack_header(self):
        blocks = sinks.SlackSink("https://x.invalid")._blocks(finding(replicas=2))

        assert "2 pods affected" in blocks["blocks"][0]["text"]["text"]

    def test_one_pod_is_not_announced_as_several_in_the_slack_header(self):
        blocks = sinks.SlackSink("https://x.invalid")._blocks(finding(replicas=1))

        assert "pods affected" not in blocks["blocks"][0]["text"]["text"]


class TestTheScopeIsTheWorkloadWhenThereIsOne:
    """
    `workload or pod` -- the workload is the name an operator recognises, and
    the pod is the fallback for a bare pod with no owner.

    The existing case only covers the fallback, so swapping `or` for `and` in
    `format_text` survived: every finding would have been labelled with the
    replica hash instead of the workload, which is the name nobody greps for.
    The fixture's workload is a prefix of its pod name, so this uses two names
    that cannot be confused for one another.
    """

    def test_the_text_sink_names_the_workload_not_the_pod(self):
        text = sinks.format_text(
            finding(workload="checkout", pod="nightly-sync-7f9c2")
        )

        assert "checkout" in text
        assert "nightly-sync-7f9c2" not in text


class TestASuccessfulDeliveryIsSilent:
    """
    The 200 branch had no test, only the failure branch.

    Two separate mutants therefore survived on `response.status != 200`:
    reading the comparison against 201, and inverting it to `==`. Either turns
    every delivered message into a `slack_rejected` warning, which is the log
    line an operator would use to decide delivery is broken.
    """

    def _response(self, status):
        response = MagicMock()
        response.status = status
        response.__enter__ = lambda self: response
        response.__exit__ = lambda *a: False
        return response

    def test_a_200_is_not_logged_as_a_rejection(self, caplog):
        sink = sinks.SlackSink("https://hooks.example.invalid/x")

        with caplog.at_level("WARNING"):
            with patch("urllib.request.urlopen", return_value=self._response(200)):
                sink.send(finding())

        assert "slack_rejected" not in caplog.text

    def test_a_500_still_is(self, caplog):
        """The counter for the test above: it must be able to see a rejection."""
        sink = sinks.SlackSink("https://hooks.example.invalid/x")

        with caplog.at_level("WARNING"):
            with patch("urllib.request.urlopen", return_value=self._response(500)):
                sink.send(finding())

        assert "slack_rejected" in caplog.text


class TestATokenWithoutAChannelDoesNotBorrowTheWebhook:
    """
    `if token and not channel` is reached only after `token and channel` has
    already returned, so dropping the `not` looks harmless -- and against the
    existing case it is, because that case configures no webhook and lands on
    stdout either way.

    With a webhook also configured the two diverge: the documented behaviour
    is to fall back to stdout rather than guess where the operator meant the
    message to go, and the mutant posts it to the webhook instead.
    """

    def test_it_falls_back_to_stdout_rather_than_to_the_webhook(self):
        built = sinks.build(
            "slack",
            webhook_url="https://hooks.slack.com/services/x",
            token="xoxb-not-a-real-token",
            channel="",
        )

        assert isinstance(built, sinks.StdoutSink)


class TestTheTruncationBoundary:
    """
    `len(text) <= limit` returns the text untouched. Tightened to `<`, a
    message of exactly the limit gains a "truncated" marker while losing
    nothing, which tells the reader to go and find a rest that does not exist.
    """

    def test_a_message_of_exactly_the_limit_is_untouched(self):
        text = "x" * 200

        assert sinks._fit(text, limit=200) == text

    def test_one_character_more_is_truncated(self):
        text = "x" * 201

        assert sinks._fit(text, limit=200) != text
        assert "truncated" in sinks._fit(text, limit=200)


class TestTheStdoutSinkFlushes:
    """
    `flush=True` is not decoration. The controller runs unattended in a
    container, where stdout is a pipe rather than a terminal and therefore
    block-buffered: without the flush a finding sits in a 4KB buffer until
    enough of them accumulate, and is lost outright if the pod is killed
    first. That is the whole delivery guarantee of this sink.

    Nothing asserted it, so dropping the flag survived.
    """

    def test_a_finding_is_written_through_rather_than_buffered(self):
        with patch("builtins.print") as printed:
            sinks.StdoutSink().send(finding())

        assert printed.call_args.kwargs.get("flush") is True


class TestSlackRendersTheModelsMarkdown:
    """
    Slack's `mrkdwn` is not Markdown: bold is one asterisk, not two. The blocks
    already declared `mrkdwn` and nothing translated into it, so a diagnosis
    reading `**payments/archiver (Pending)**` arrived with the asterisks
    visible around the name.

    Observed in #kubernetes-events on 2026-09-04, in an answer that was
    otherwise correct and scored `grounded`. Reading the raw string over the
    API did not show it; a screenshot of the rendered message did, which is
    the same trap `test_ui_markup.py` records for the console.
    """

    def test_double_asterisks_become_slack_bold(self):
        assert sinks._mrkdwn("**payments/archiver**") == "*payments/archiver*"

    def test_underscored_bold_is_the_other_markdown_spelling(self):
        assert sinks._mrkdwn("__also bold__") == "*also bold*"

    def test_bold_inside_a_fence_is_left_alone(self):
        """A diagnosis quotes YAML and log lines; `**` in a fence is text."""
        text = "before ```x = **literal**``` after"

        assert sinks._mrkdwn(text) == text

    def test_bold_inside_a_code_span_is_left_alone(self):
        text = "the flag `--a**b**c` is not emphasis"

        assert sinks._mrkdwn(text) == text

    def test_stray_asterisks_are_not_emphasis(self):
        """`a ** b ** c` is multiplication or noise, not bold."""
        for text in ("a ** b ** c", "****", "2 ** 8 is 256"):
            assert sinks._mrkdwn(text) == text

    def test_the_conversion_reaches_the_block_that_is_sent(self):
        """
        The counter for the cases above: they all test the helper, and a
        helper nothing calls converts nothing.
        """
        blocks = sinks.SlackSink("https://hooks.example.invalid/x")._blocks({
            "workload": "checkout", "namespace": "payments", "pod": "checkout-1",
            "status": "CrashLoopBackOff", "replicas": 1,
            "diagnosis": "The **checkout** container exited 1.",
            "confidence": "grounded", "unverified": [],
        })
        body = "".join(
            b.get("text", {}).get("text", "") for b in blocks["blocks"]
            if isinstance(b.get("text"), dict))

        assert "*checkout* container" in body
        assert "**checkout**" not in body


class TestAnAnswerIsNotAFinding:
    """
    The bot sent answers through the finding renderer with the question in the
    `workload` key, so every reply was headed

        :warning: *why is checkout failing in payments?* is unhealthy in ``

    Seen in #kubernetes-events on 2026-09-04, above an answer that was itself
    correct and grounded. The controller reports findings about workloads; the
    bot answers questions. Sharing one renderer is what produced it.
    """

    ANSWER = {
        "kind": "answer",
        "question": "why is checkout failing in payments?",
        "answer": "The **checkout** container exited 1.",
        "confidence": "grounded",
        "unverified": [],
    }

    def test_the_header_does_not_call_the_question_unhealthy(self):
        blocks = sinks.SlackSink("https://hooks.example.invalid/x")._blocks(self.ANSWER)
        header = blocks["blocks"][0]["text"]["text"]

        assert "is unhealthy in" not in header
        assert "``" not in header, "an empty namespace rendered as empty backticks"
        assert self.ANSWER["question"] in header

    def test_the_answer_is_still_rendered_as_mrkdwn(self):
        blocks = sinks.SlackSink("https://hooks.example.invalid/x")._blocks(self.ANSWER)
        body = blocks["blocks"][1]["text"]["text"]

        assert "*checkout* container" in body and "**checkout**" not in body

    def test_the_fallback_text_is_the_question(self):
        """What a notification preview shows, and what search indexes."""
        blocks = sinks.SlackSink("https://hooks.example.invalid/x")._blocks(self.ANSWER)

        assert blocks["text"] == self.ANSWER["question"]

    def test_the_verdict_still_travels_with_it(self):
        answer = {**self.ANSWER, "confidence": "partial", "unverified": ["8Gi"]}
        blocks = sinks.SlackSink("https://hooks.example.invalid/x")._blocks(answer)
        context = blocks["blocks"][2]["elements"][0]["text"]

        assert "partial" in context and "8Gi" in context

    def test_stdout_splits_them_too(self):
        """`[] <question> in ` reads as a broken parser."""
        text = sinks.format_text(self.ANSWER)

        assert text.startswith("Q: why is checkout failing in payments?")
        assert "is unhealthy" not in text

    def test_a_finding_is_still_a_finding(self):
        """The controller's path must be untouched."""
        blocks = sinks.SlackSink("https://hooks.example.invalid/x")._blocks({
            "workload": "checkout", "namespace": "payments", "pod": "checkout-1",
            "status": "CrashLoopBackOff", "replicas": 1,
            "diagnosis": "exited 1", "confidence": "grounded", "unverified": [],
        })

        assert "is unhealthy in `payments`" in blocks["blocks"][0]["text"]["text"]


class TestSlackHasNoHeadings:
    """
    A real diagnosis on 2026-09-04 arrived containing

        ### Root Cause
        - **Memory Exhaustion**: the container's limit is **96Mi** ...

    with the hashes visible. Slack has no heading syntax; bold is what a
    heading means in a channel.

    This was first written up as deliberately unconverted, on the grounds that
    doing it well needs a parser. Seeing it in real output did not survive that
    reasoning: a heading is one anchored line, and leaving it produced a wart
    in every structured answer the model writes.
    """

    def test_a_heading_becomes_bold(self):
        assert sinks._mrkdwn("### Root Cause") == "*Root Cause*"

    def test_every_depth_is_a_heading(self):
        assert sinks._mrkdwn("# One") == "*One*"
        assert sinks._mrkdwn("###### Six") == "*Six*"

    def test_closing_hashes_are_dropped(self):
        assert sinks._mrkdwn("## Trailing ##") == "*Trailing*"

    def test_a_hash_mid_line_is_not_a_heading(self):
        """`kubectl get pod # comment` is not a title."""
        assert sinks._mrkdwn("a # not a heading") == "a # not a heading"

    def test_a_hash_without_a_space_is_not_a_heading(self):
        """`#nospace` is a tag or an anchor, not Markdown."""
        assert sinks._mrkdwn("#nospace") == "#nospace"

    def test_a_comment_inside_a_fence_is_left_alone(self):
        """The case that makes this safe: diagnoses quote shell and YAML."""
        text = "```\n# set the limit\nresources:\n```"

        assert sinks._mrkdwn(text) == text

    def test_a_heading_and_bold_together(self):
        got = sinks._mrkdwn("### Root Cause\n- **Memory** exhausted")

        assert got == "*Root Cause*\n- *Memory* exhausted"
