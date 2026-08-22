"""
Tests for the evidence-read instrument.

This file exists because the two previous attempts at this measurement both
shipped, both ran, and both produced numbers that meant nothing -- a token
overlap that could not match `db:5432` against `db:5432:`, and its "fix",
which stripped the punctuation before testing for it. Neither had a test with
an input carrying the punctuation.

So the cases below are the failures first: the trailing colon, the JSON escape
glued to `FATAL`, and the paraphrases seen in real answers. Then the negatives
that stop the instrument reading `read` off any fluent text -- a restatement
of the status, a different port, a substring of a longer number.

No cluster, no model: `read()` is a pure function over a string.
"""

import importlib.util
import json
import os

EVALS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evals")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(EVALS, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = _load("evidence_read")


def prefetched(text):
    """Evidence in the shape capture_pod_logs() returns, escapes and all."""
    return [{
        "name": "get_pod_logs",
        "arguments": {"name": "crasher-abc", "namespace": "demo"},
        "result": json.dumps({"pod": "crasher-abc", "logs": text}),
    }]


CRASHER_LOG = (
    "connecting to database...\n"
    "FATAL: could not connect to db:5432: connection refused\n"
)


class TestTheFailuresThatSankTheEarlierInstruments:
    def test_the_log_carries_a_trailing_colon_the_answer_does_not(self):
        # `db:5432:` in the log against `db:5432` in the answer. Token overlap
        # scored this as no overlap at all.
        assert ev.contains("db:5432", ev.normalise("could not connect to db:5432: refused"))

    def test_a_json_escaped_newline_does_not_swallow_the_next_word(self):
        # capture_pod_logs stores json.dumps(result), so the model reads
        # `...database...\nFATAL: could not...`. The escape has to become a
        # boundary, not disappear: deleting it yields `database...fatal`.
        text = ev.evidence_text(prefetched(CRASHER_LOG))
        assert "\\n" not in text
        assert ev.contains("fatal: could not connect to db:5432: connection refused", text)

    def test_evidence_carries_every_source_line(self):
        assert ev.evidence_carries("crasher", prefetched(CRASHER_LOG)) == []

    def test_an_empty_capture_voids_the_run_rather_than_failing_it(self):
        # capture_pod_logs returns [] on a 404 or an error dict. Every fact is
        # unreachable, and none of them is the model's fault.
        assert ev.evidence_carries("crasher", []) == ["endpoint", "dependency", "failure"]

    def test_punctuation_survives_normalisation(self):
        assert "db:5432" in ev.normalise("  Could not connect to DB:5432:  ")


class TestParaphrasesSeenInRealAnswers:
    def test_the_host_is_renamed_and_the_port_is_not(self):
        verdict = ev.read("crasher", "crasher cannot reach db-service:5432.")
        assert verdict["read"]
        assert verdict["facts"]["endpoint"] == "5432"
        assert verdict["facts"]["dependency"] == "db-service"

    def test_the_refusal_without_the_port(self):
        # A diagnosis that names the refused database connection has read the
        # line, whether or not it repeats the number.
        verdict = ev.read("crasher", "The container exits because the database "
                                     "connection is refused at startup.")
        assert verdict["read"]
        assert verdict["matched"] == ["dependency", "failure"]
        assert not verdict["complete"]

    def test_the_503_error(self):
        # `upstream returned 503` comes back as "the 503 error" -- the code
        # survives the paraphrase and the word `upstream` does not.
        verdict = ev.read("nightly-sync", "The job fails on the 503 error from its dependency.")
        assert verdict["read"]
        assert verdict["facts"]["code"] == "503"
        assert verdict["facts"]["upstream"] is None

    def test_service_unavailable_spelled_out(self):
        assert ev.read("nightly-sync", "the upstream returned Service Unavailable")["complete"]

    def test_postgres_inferred_from_the_port(self):
        # An inference, and one the log is the only route to.
        assert ev.read("crasher", "Postgres on 5432 is refusing connections")["complete"]


class TestTheAnswerThatReadNothing:
    def test_the_52_character_ungrounded_diagnosis(self):
        # The run this instrument was built for: the log was in the prompt and
        # the answer engaged with none of it.
        verdict = ev.read("crasher", "The pod is crashing repeatedly and needs investigation.")
        assert not verdict["read"]
        assert verdict["matched"] == []

    def test_a_fluent_restatement_of_the_status_is_not_a_reading(self):
        verdict = ev.read(
            "crasher",
            "crasher is in CrashLoopBackOff, has restarted 14 times, and its "
            "container exits with exit code 1 each time.",
        )
        assert not verdict["read"]
        assert verdict["status_only"]
        assert verdict["decoys"] == ["exit", "phase", "restarts"]

    def test_an_empty_answer_is_neither_read_nor_status_only(self):
        verdict = ev.read("crasher", "")
        assert not verdict["read"]
        assert not verdict["status_only"]
        assert verdict["decoys"] == []


class TestBoundaries:
    def test_a_longer_number_is_not_the_port(self):
        assert not ev.read("crasher", "the pid was 54321")["read"]

    def test_the_port_is_not_found_inside_another_number(self):
        assert not ev.read("nightly-sync", "the container used 1503 MB")["read"]

    def test_a_different_port_does_not_match(self):
        assert not ev.read("crasher", "the readiness probe on port 8080 fails")["read"]

    def test_the_port_matches_next_to_punctuation_on_both_sides(self):
        for text in ("port 5432.", "(5432)", "db:5432:", "5432,", "port-5432"):
            assert ev.read("crasher", text)["facts"]["endpoint"] == "5432", text

    def test_connect_is_not_connection_pooling(self):
        assert ev.read("crasher", "connectivity looks fine")["facts"]["failure"] is None


class TestOnlyTheQualifyingWorkloads:
    def test_memory_hog_has_no_facts_because_ignoring_its_log_is_correct(self):
        # Its cause is OOMKilled and a 64Mi limit, both in the status; its log
        # is stress output. Scoring it would measure obedience, not reading.
        assert "memory-hog" not in ev.FACTS
        assert ev.read("memory-hog", "OOMKilled against a 64Mi limit")["read"] is False

    def test_an_unknown_workload_is_never_complete(self):
        # `complete` over an empty fact set must not be vacuously true, or a
        # typo'd workload name reports a perfect score.
        assert ev.read("bad-image", "anything at all")["complete"] is False


probe = _load("probe_evidence_read")


class TestTheProbeScoresTheRightText:
    """
    `score()` reads `draft`, and the reason is not stylistic.

    `verify()` rewrites unsupported values in the prose and `annotate()`
    appends a footer quoting them back, so the published text can gain or lose
    a phrase this instrument looks for without the model having written or
    omitted anything. Three of five live runs re-scored differently against
    `answer` when this was last measured for grounding; the same hazard
    applies here.
    """

    def test_the_verdict_follows_the_draft_not_the_published_answer(self):
        result = {"draft": "crasher cannot reach db:5432; connection refused."}
        finding = {"diagnosis": "crasher cannot reach its database.\n---\n"
                                "**Evidence audit.** 1 of 4 stated values could "
                                "not be traced to any tool result"}
        record = probe.score("crasher", result, prefetched(CRASHER_LOG), finding)
        assert record["draft"]["facts"]["endpoint"] == "5432"
        assert record["published"]["facts"]["endpoint"] is None
        assert record["rewritten"]

    def test_an_unrewritten_answer_is_not_flagged(self):
        result = {"draft": "the database refused the connection"}
        finding = {"diagnosis": "the database refused the connection"}
        record = probe.score("crasher", result, prefetched(CRASHER_LOG), finding)
        assert not record["rewritten"]
        assert not record["void"]

    def test_an_empty_capture_voids_the_run(self):
        result = {"draft": "the pod is crashing"}
        record = probe.score("crasher", result, [], {"diagnosis": "the pod is crashing"})
        assert record["void"]

    def test_a_capture_that_missed_the_line_voids_the_run(self):
        # A pod that has started but not yet logged: the capture succeeded and
        # carries nothing the answer could have read.
        record = probe.score(
            "crasher",
            {"draft": "the pod is crashing"},
            prefetched("connecting to database...\n"),
            {"diagnosis": "the pod is crashing"},
        )
        assert record["void"]
        assert record["missing_from_evidence"] == ["endpoint", "failure"]

    def test_only_the_two_decidable_workloads_are_probed(self):
        assert probe.WORKLOADS == ("crasher", "nightly-sync")
