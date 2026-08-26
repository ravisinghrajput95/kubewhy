"""
The replay harness, which is the thing that decides whether a grounding change
is believed.

Weighted towards the ways a replay has actually lied in this project: a stale
cache, a shadowed module, and a harness wired to nothing that reports "no
regressions" because it never reached the checker. A replay you cannot trust is
worse than no replay, because its output is a green tick.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "evals"))

import replay_grounding as replay          # noqa: E402
import grounding                            # noqa: E402


def record(draft, evidence, confidence, case="a_case", answer=None):
    return {
        "case": case,
        "model": "test-model",
        "draft": draft,
        # The annotated text, which must never be what is scored.
        "answer": answer if answer is not None else draft + "\n\n[annotated]",
        "evidence": evidence,
        "confidence": confidence,
    }


def written(tmp_path, rows, name="run.json"):
    path = tmp_path / name
    path.write_text(json.dumps(rows))
    return str(path)


EVIDENCE = [{"id": "tool-1", "tool": "describe_pod",
             "result": '{"pod":"web","restarts":7}'}]


class TestItScoresTheCheckersInputs:
    def test_it_replays_the_draft_not_the_answer(self, tmp_path, monkeypatch):
        """
        The one that matters. `answer` has been through verify(), which
        rewrites unsupported figures in place, and annotate(), which appends
        the audit -- so scoring it reproduces a different verdict and the
        difference is the tooling rather than the change under test.
        """
        seen = []

        def spy(answer, evidence):
            seen.append(answer)
            return {"confidence": "grounded", "unverified": [], "checked": 1,
                    "claims": []}

        monkeypatch.setattr(grounding, "check", spy)
        path = written(tmp_path, [record("THE DRAFT", EVIDENCE, "grounded",
                                         answer="THE ANNOTATED ANSWER")])
        replay.replay([path])

        assert seen == ["THE DRAFT"]

    def test_a_record_without_the_inputs_is_skipped_and_counted(self, tmp_path):
        """
        Older runs did not retain them. That is a property of the record, not
        a failure of the replay -- but a shrinking corpus has to be visible,
        or a replay of nothing reports a clean run.
        """
        rows = [record("d", EVIDENCE, "grounded"),
                {"case": "old", "answer": "a", "confidence": "grounded"}]
        result = replay.replay([written(tmp_path, rows)])

        assert result["skipped_no_inputs"] == 1
        assert result["unchanged"] + len(result["moved"]) == 1

    def test_a_moved_verdict_is_reported_with_both_values(self, tmp_path, monkeypatch):
        monkeypatch.setattr(grounding, "check",
                            lambda a, e: {"confidence": "contradicted"})
        result = replay.replay([written(tmp_path, [record("d", EVIDENCE, "grounded")])])

        assert len(result["moved"]) == 1
        assert result["moved"][0]["was"] == "grounded"
        assert result["moved"][0]["now"] == "contradicted"

    def test_a_checker_that_raises_is_recorded_not_swallowed(self, tmp_path, monkeypatch):
        """
        A checker raising on a recorded input is a defect in the checker. It
        must not be counted as "unchanged", which is what a bare except does.
        """
        def explode(answer, evidence):
            raise ValueError("boom")

        monkeypatch.setattr(grounding, "check", explode)
        result = replay.replay([written(tmp_path, [record("d", EVIDENCE, "grounded")])])

        assert result["unchanged"] == 0
        assert len(result["errors"]) == 1
        assert "ValueError" in result["errors"][0]["error"]

    def test_the_corpus_is_not_written_back(self, tmp_path):
        """
        A replay that edits what it measures against cannot be run twice.
        """
        path = written(tmp_path, [record("d", EVIDENCE, "grounded")])
        before = open(path).read()
        replay.replay([path])

        assert open(path).read() == before


class TestTheSelfCheckProvesTheHarnessIsWired:
    """
    "No regressions" from a replay that never called the checker looks exactly
    like "no regressions" from one that did.
    """

    def test_it_passes_when_a_perturbation_is_detected(self, tmp_path, capsys):
        path = written(tmp_path, [record("the pod restarted 7 times",
                                         EVIDENCE, "grounded")])
        assert replay.self_check([path]) == 0
        assert "self-check passed" in capsys.readouterr().out

    def test_it_fails_when_the_replay_reaches_no_records(self, tmp_path):
        path = written(tmp_path, [{"case": "old", "answer": "a",
                                   "confidence": "grounded"}])
        with pytest.raises(SystemExit) as exit:
            replay.self_check([path])

        assert "no records were scored" in str(exit.value)

    def test_it_fails_when_the_replay_ignores_the_checker(self, tmp_path, monkeypatch):
        """
        The failure this exists for: a replay wired to nothing. Simulated by
        making the comparison always agree, which is what a harness that never
        called the checker would produce.
        """
        monkeypatch.setattr(replay, "replay",
                            lambda paths, check=None: {
                                "moved": [], "unchanged": 5,
                                "skipped_no_inputs": 0, "errors": []})
        with pytest.raises(SystemExit) as exit:
            replay.self_check([written(tmp_path, [record("d", EVIDENCE, "grounded")])])

        assert "not exercising grounding.check()" in str(exit.value)


class TestItRefusesToRunAgainstAShadowedChecker:
    def test_a_module_loaded_from_elsewhere_is_refused(self, monkeypatch):
        """
        It happened: copies of grounding.py and contradiction.py sat beside
        the replay script, Python put that directory ahead of PYTHONPATH, and
        the replay compared the new code against itself while reporting zero
        changes.
        """
        monkeypatch.setattr(grounding, "__file__",
                            os.path.join(ROOT, "evals", "grounding.py"))
        with pytest.raises(SystemExit) as exit:
            replay._assert_not_shadowed()

        assert "shadowed" in str(exit.value)

    def test_the_real_layout_is_accepted(self):
        replay._assert_not_shadowed()

    def test_the_modules_are_fingerprinted(self):
        """
        Printed every run rather than behind a flag: both recorded harness
        failures would have been visible in one line of this.
        """
        path, digest = replay._fingerprint(grounding)

        assert path.endswith("grounding.py")
        assert len(digest) == 12


class TestTheCiGate:
    def test_it_exits_zero_when_nothing_moved(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(grounding, "check", lambda a, e: {"confidence": "grounded"})
        path = written(tmp_path, [record("d", EVIDENCE, "grounded")])

        assert replay.main([path, "--fail-on-change"]) == 0

    def test_it_exits_one_when_a_verdict_moved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(grounding, "check", lambda a, e: {"confidence": "partial"})
        path = written(tmp_path, [record("d", EVIDENCE, "grounded")])

        assert replay.main([path, "--fail-on-change"]) == 1

    def test_a_move_without_the_flag_is_reported_but_not_fatal(self, tmp_path, monkeypatch):
        """
        Replaying to see what a change did is the common use. Failing by
        default would make the tool useless for the thing it is mostly for.
        """
        monkeypatch.setattr(grounding, "check", lambda a, e: {"confidence": "partial"})
        path = written(tmp_path, [record("d", EVIDENCE, "grounded")])

        assert replay.main([path]) == 0

    def test_a_raising_checker_fails_even_without_the_flag(self, tmp_path, monkeypatch):
        def explode(answer, evidence):
            raise ValueError("boom")

        monkeypatch.setattr(grounding, "check", explode)
        path = written(tmp_path, [record("d", EVIDENCE, "grounded")])

        assert replay.main([path]) == 1
