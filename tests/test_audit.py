"""
The per-investigation audit trail.

Weighted deliberately towards what must NOT be in a record. An audit log is
shipped somewhere central, kept for a long time and read by more people than
the console is; a record that carried the evidence would be a second copy of
the most sensitive thing this project handles, under weaker controls than the
original, created in the name of protecting it.

The other half is completeness. The runs worth auditing most are the ones that
never reach an answer -- a model that raised, a deadline that fired, a caller
that walked away -- so those are tested by name rather than assumed to follow
from the happy path.
"""

import json
import logging

import pytest

import agent
import audit
import inference


SECRET_LOG = "db password=hunter2 and AKIAIOSFODNN7EXAMPLE"


def events(with_logs=True, answer=True):
    """A run's events, in the shape stream() yields them."""
    yield {"type": "tool_call", "run_id": "run-1", "name": "list_pods",
           "arguments": {"namespace": "demo", "only_unhealthy": True}}
    yield {"type": "tool_result", "run_id": "run-1", "name": "list_pods",
           "result": '{"pods":[]}', "duration_ms": 11.0}
    if with_logs:
        yield {"type": "tool_call", "run_id": "run-1", "name": "get_pod_logs",
               "arguments": {"name": "crasher-1", "namespace": "payments"}}
        yield {"type": "tool_result", "run_id": "run-1", "name": "get_pod_logs",
               "result": SECRET_LOG, "duration_ms": 40.0}
    if answer:
        yield {"type": "answer", "run_id": "run-1",
               "target": {"kind": "deployment", "name": "crasher",
                          "namespace": "demo"},
               "answer": "crashed because " + SECRET_LOG,
               # Top level, because that is where the loop puts it: the answer
               # event spreads grounding.check()'s verdict with **verdict, and
               # "rca" is contract() output which has no confidence in it.
               # An earlier version of this fixture put it under "rca" and the
               # test passed against a shape no real run produces.
               "confidence": "grounded",
               "rca": {"observations": [], "unknowns": []}}


@pytest.fixture
def run(monkeypatch, caplog):
    """
    Drive stream() over canned events and return the emitted record.

    Patches _stream, not stream: the wrapper under test is what turns events
    into a record, and patching stream would test nothing.
    """
    def drive(generator=None, question="why is crasher failing?", drain=True):
        source = events() if generator is None else generator
        monkeypatch.setattr(
            agent, "_stream",
            lambda *a, **k: iter(source))
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="triage.audit"):
            stream = agent.stream(question)
            if drain:
                for _ in stream:
                    pass
            else:
                next(stream)
                stream.close()
        return record(caplog)
    return drive


def record(caplog):
    lines = [r for r in caplog.records if r.message == "investigation"]
    assert len(lines) == 1, f"expected one audit record, got {len(lines)}"
    return lines[0]


def as_text(line):
    """The record as it would be serialised, for 'is X anywhere in it' checks."""
    builtin = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
    return json.dumps(
        {k: v for k, v in line.__dict__.items() if k not in builtin},
        default=str)


class TestTheRecordDoesNotCarryTheEvidence:
    """The half that matters. Each of these is a disclosure if it fails."""

    def test_no_tool_output_appears_anywhere(self, run):
        assert "hunter2" not in as_text(run())

    def test_not_even_a_recognisable_secret_shape(self, run):
        assert "AKIAIOSFODNN7EXAMPLE" not in as_text(run())

    def test_the_answer_text_is_not_recorded(self, run):
        """It quotes the evidence, so recording it recreates the problem."""
        text = as_text(run())
        assert "crashed because" not in text

    def test_the_size_of_a_result_is_recorded_but_not_its_content(self, run):
        line = run()
        logs = next(t for t in line.tools if t["tool"] == "get_pod_logs")

        assert logs["result_chars"] == len(SECRET_LOG)
        assert "result" not in logs

    def test_the_inference_endpoint_is_not_recorded(self, run):
        """
        An endpoint can carry a token in its userinfo or its query string --
        the same reason telemetry.py refuses it as a label.
        """
        assert "endpoint" not in as_text(run())

    def test_a_secret_pasted_into_the_question_is_redacted(self, run):
        line = run(question="why did the pod with password=hunter2 fail?")

        assert "hunter2" not in line.question
        assert "REDACTED" in line.question

    def test_a_very_long_question_is_capped(self, run):
        line = run(question="x" * 50_000)
        assert len(line.question) <= 2000


class TestTheRecordAnswersTheQuestionsAnAuditAsks:
    def test_who_asked(self, run):
        audit.actor("sre@example.com", surface="console", auth="proxy")
        line = run()

        assert line.principal == "sre@example.com"
        assert line.auth == "proxy"
        assert line.surface == "console"

    def test_an_unattributed_run_says_so_rather_than_omitting_the_field(self, run):
        """
        A record with no principal field reads as "nobody asked", which is
        never what happened, and is invisible to the query an audit review
        actually runs.
        """
        audit._PRINCIPAL.set("anonymous")
        audit._AUTH.set("anonymous")
        audit._SURFACE.set("unknown")
        line = run()

        assert line.principal == "anonymous"
        assert line.auth == "anonymous"

    def test_which_namespaces_were_touched(self, run):
        assert run().namespaces == ["demo", "payments"]

    def test_whose_logs_were_read(self, run):
        """
        Separate from the tool list, because the query an auditor runs is
        "whose logs were read" and answering it should not require knowing
        which of thirteen tools returns application output.
        """
        reads = run().sensitive_reads

        assert reads == [{"tool": "get_pod_logs", "pod": "crasher-1",
                          "namespace": "payments"}]

    def test_a_run_that_read_no_logs_has_no_sensitive_reads(self, run):
        assert run(generator=events(with_logs=False)).sensitive_reads == []

    def test_what_was_investigated(self, run):
        line = run()
        assert line.target["name"] == "crasher"
        assert line.verdict == "grounded"
        assert line.outcome == "answered"

    def test_the_tool_arguments_are_kept(self, run):
        """
        "Which namespace did it read" is the argument, not the output. This is
        the line between auditing a run and copying its evidence.
        """
        listed = next(t for t in run().tools if t["tool"] == "list_pods")
        assert listed["arguments"]["namespace"] == "demo"

    def test_the_run_id_ties_the_record_to_the_investigation(self, run):
        assert run().run_id == "run-1"


class TestEveryRunIsRecorded:
    """
    The runs worth auditing most are the ones that never reach an answer.
    """

    def test_a_run_that_raises_is_still_recorded(self, run, monkeypatch, caplog):
        def exploding(*a, **k):
            yield {"type": "tool_call", "run_id": "run-1", "name": "list_pods",
                   "arguments": {"namespace": "demo"}}
            raise ConnectionError("ollama refused at http://secret-host:11434")

        monkeypatch.setattr(agent, "_stream", exploding)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="triage.audit"):
            with pytest.raises(ConnectionError):
                for _ in agent.stream("why?"):
                    pass

        line = record(caplog)
        assert line.outcome == "error"
        assert line.error == "ConnectionError"

    def test_the_exception_message_is_not_recorded(self, run, monkeypatch, caplog):
        """
        The class, never the message. A provider's error text can quote the
        request, and the request carries the evidence -- inference.py applies
        the same rule when it logs a failover.
        """
        def exploding(*a, **k):
            raise ConnectionError("refused at http://secret-host:11434")
            yield

        monkeypatch.setattr(agent, "_stream", exploding)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="triage.audit"):
            with pytest.raises(ConnectionError):
                for _ in agent.stream("why?"):
                    pass

        assert "secret-host" not in as_text(record(caplog))

    def test_a_caller_that_walks_away_is_still_recorded(self, run):
        """
        Closing the generator raises GeneratorExit, which an `except
        Exception` does not catch. Only `finally` does -- and a browser tab
        closed mid-investigation is the ordinary way this happens.

        Recorded as abandoned rather than as an error: an audit trail that
        files a closed tab as a failure has people chasing incidents that did
        not happen.
        """
        line = run(drain=False)
        assert line.outcome == "abandoned"

    def test_a_terminated_run_records_why(self, run):
        terminated = iter([
            {"type": "answer", "run_id": "run-1", "target": None,
             "answer": "gave up", "termination": "deadline_exceeded"},
        ])
        line = run(generator=terminated)

        assert line.outcome == "terminated"
        assert line.termination == "deadline_exceeded"

    def test_exactly_one_record_per_run(self, run):
        run()  # record() asserts len == 1


class TestWhetherEvidenceCouldHaveLeftTheNetwork:
    """
    Derived from policy, not observed, because policy is provable: the gateway
    holds one `active` target for the whole process, so reading it at the end
    of a run is right with one investigation in flight and wrong with two.
    """

    def local(self):
        return inference.Target(mode="local", model="qwen3")

    def hosted(self):
        return inference.Target(mode="api", provider="openai",
                                endpoint="https://api.openai.com/v1",
                                model="gpt-4o-mini", api_key="k")

    def test_policy_forbidding_external_inference_is_a_proof(self):
        config = inference.Config(self.local())
        assert audit._egress(config) is False

    def test_everything_external_and_permitted_is_also_a_proof(self):
        config = inference.Config(
            self.hosted(),
            policy=inference.Policy(allow_external=True))
        assert audit._egress(config) is True

    def test_permission_granted_but_nothing_points_off_network(self):
        config = inference.Config(
            self.local(), policy=inference.Policy(allow_external=True))
        assert audit._egress(config) is False

    def test_a_mixed_configuration_says_possible_rather_than_guessing(self):
        """
        An auditor can act on "possible". They cannot act on a `false` that
        meant "probably not".
        """
        config = inference.Config(
            self.local(), fallback=self.hosted(),
            policy=inference.Policy(allow_external=True, fallback_enabled=True))
        assert audit._egress(config) == "possible"

    def test_the_record_carries_it(self, run):
        assert run().inference["evidence_left_network"] is False


class TestTheSwitchesAndTheSink:
    def test_auditing_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_AUDIT", raising=False)
        assert audit.enabled() is True

    def test_it_can_be_turned_off(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_AUDIT", "0")
        assert audit.enabled() is False

    def test_turning_it_off_emits_nothing(self, monkeypatch, caplog):
        monkeypatch.setenv("TRIAGE_AUDIT", "0")
        monkeypatch.setattr(agent, "_stream", lambda *a, **k: iter(events()))
        with caplog.at_level(logging.INFO, logger="triage.audit"):
            for _ in agent.stream("why?"):
                pass

        assert not [r for r in caplog.records if r.message == "investigation"]

    def test_the_file_sink_writes_one_json_line(self, monkeypatch, tmp_path, caplog):
        path = tmp_path / "audit.log"
        monkeypatch.setenv("TRIAGE_AUDIT_LOG", str(path))
        monkeypatch.setattr(agent, "_stream", lambda *a, **k: iter(events()))
        for _ in agent.stream("why?"):
            pass

        written = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(written) == 1
        assert written[0]["event"] == "investigation"
        assert written[0]["run_id"] == "run-1"

    def test_the_sink_does_not_carry_the_evidence_either(self, monkeypatch, tmp_path):
        path = tmp_path / "audit.log"
        monkeypatch.setenv("TRIAGE_AUDIT_LOG", str(path))
        monkeypatch.setattr(agent, "_stream", lambda *a, **k: iter(events()))
        for _ in agent.stream("why?"):
            pass

        assert "hunter2" not in path.read_text()

    def test_an_unwritable_sink_does_not_take_down_the_investigation(
            self, monkeypatch, tmp_path, caplog):
        """
        Errors are data here as everywhere else. Losing a record is a smaller
        problem than losing the diagnosis -- provided the loss is logged.
        """
        monkeypatch.setenv("TRIAGE_AUDIT_LOG", str(tmp_path / "nope" / "a.log"))
        monkeypatch.setattr(agent, "_stream", lambda *a, **k: iter(events()))

        with caplog.at_level(logging.WARNING, logger="triage.audit"):
            drained = list(agent.stream("why?"))

        assert drained  # the investigation completed
        assert any(r.message == "audit_sink_failed" for r in caplog.records)
