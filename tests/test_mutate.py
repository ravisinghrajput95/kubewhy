"""
The mutation harness.

A tool that makes a claim about the tests needs tests of its own, and this one
fails silently in both directions: a harness whose mutants never reach the
suite reports every mutation as surviving, and one whose suite never really
runs reports every mutation as killed. Neither looks like a broken tool.

The property tested hardest is that it never touches the working tree. The
alternative design -- mutate in place, restore in a `finally` -- leaves a
comment-stripped source file behind the first time the process is killed at
the wrong moment.
"""

import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "evals"))

import mutate                                    # noqa: E402

SOURCE = '''
def f(a, b):
    if a < b and not b:
        return True
    return a + 1
'''


class TestItFindsSomethingToBreak:
    def test_it_finds_the_comparison(self):
        kinds = [s["what"] for s in mutate.sites(SOURCE)]
        assert "Lt -> LtE" in kinds

    def test_it_finds_the_boolean_operator(self):
        assert any(s["kind"] == "boolop" for s in mutate.sites(SOURCE))

    def test_it_finds_the_negation(self):
        assert any(s["kind"] == "not" for s in mutate.sites(SOURCE))

    def test_it_finds_constants_and_arithmetic(self):
        kinds = {s["kind"] for s in mutate.sites(SOURCE)}
        assert {"bool", "int", "binop"} <= kinds

    def test_every_site_carries_a_line_number(self):
        """A survivor without a line number is a survivor nobody can look at."""
        assert all(s["line"] > 0 for s in mutate.sites(SOURCE))


class TestEachMutationIsExactlyOne:
    def test_a_mutation_changes_the_source(self):
        assert mutate.mutate(SOURCE, 0) != ast.unparse(ast.parse(SOURCE))

    def test_it_changes_one_thing_at_a_time(self):
        """
        Two mutations at once cannot be attributed: if the suite catches the
        pair, you do not know which half it caught.
        """
        original = ast.unparse(ast.parse(SOURCE)).splitlines()
        mutated = mutate.mutate(SOURCE, 0).splitlines()
        differing = [i for i, (a, b) in enumerate(zip(original, mutated)) if a != b]

        assert len(differing) == 1

    def test_the_result_still_parses(self):
        """A mutant that cannot be imported is not a test of anything."""
        for index in range(len(mutate.sites(SOURCE))):
            mutant = mutate.mutate(SOURCE, index)
            if mutant is not None:
                ast.parse(mutant)

    def test_an_index_past_the_end_changes_nothing(self):
        assert mutate.mutate(SOURCE, 9999) is None

    def test_a_mutation_that_changes_nothing_is_reported_as_such(self):
        """
        Rather than counted as a survivor. An unchanged file passes the suite
        and would read as a gap in the tests, which is the opposite of true.
        """
        assert mutate.mutate("x = 1\n", 9999) is None


class TestItDoesNotTouchTheWorkingTree:
    def test_the_source_it_mutates_is_left_byte_identical(self):
        target = os.path.join(ROOT, "limits.py")
        before = open(target, "rb").read()

        mutate.run("limits.py", ["tests/test_limits.py"], limit=2)

        assert open(target, "rb").read() == before

    def test_the_copy_leaves_out_the_heavy_directories(self, tmp_path):
        where = mutate.working_copy(str(tmp_path / "repo"))

        assert not os.path.exists(os.path.join(where, ".git"))
        assert not os.path.exists(os.path.join(where, ".venv"))
        assert os.path.exists(os.path.join(where, "limits.py"))

    def test_results_is_linked_rather_than_copied(self, tmp_path):
        """8MB that one test module reads and none writes."""
        where = mutate.working_copy(str(tmp_path / "repo"))
        assert os.path.islink(os.path.join(where, "results"))


class TestItRefusesToReportOnARedBaseline:
    def test_a_failing_suite_aborts_before_any_mutation(self, tmp_path, monkeypatch):
        """
        The silent direction: if the suite is already failing, every mutant
        dies and the run reports perfect coverage.
        """
        monkeypatch.setattr(mutate, "run_tests",
                            lambda where, tests, timeout=300: (False, "1 failed"))
        with pytest.raises(SystemExit) as exit:
            mutate.run("limits.py", ["tests/test_limits.py"], limit=1)

        assert "baseline is already failing" in str(exit.value)


class TestTheSelfCheck:
    def test_it_fails_when_a_known_lethal_mutation_survives(self, monkeypatch):
        """
        The other silent direction: a harness whose suite never really runs
        reports every mutation as killed.
        """
        monkeypatch.setattr(mutate, "run_tests",
                            lambda where, tests, timeout=300: (True, ""))
        with pytest.raises(SystemExit) as exit:
            mutate.self_check()

        assert "not running the code it thinks it is" in str(exit.value)


class TestTheReport:
    def test_it_names_the_line_of_every_survivor(self):
        text = mutate.report({
            "module": "limits.py", "tests": [], "killed": 3,
            "survivors": [{"kind": "compare", "line": 42, "what": "Lt -> LtE"}],
            "equivalent_skipped": 1,
        })

        assert "limits.py:42" in text
        assert "Lt -> LtE" in text

    def test_it_does_not_print_a_score(self):
        """
        Deliberate. Some mutants cannot change behaviour, so a percentage
        invites someone to raise it by writing tests for equivalent mutants.
        """
        text = mutate.report({
            "module": "m.py", "tests": [], "killed": 9,
            "survivors": [], "equivalent_skipped": 0,
        })

        assert "%" not in text
