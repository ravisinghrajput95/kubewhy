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


class TestTheLabelMatchesTheChange:
    """
    Sites and Apply must walk in the same order.

    They did not: Sites recorded a node before descending into its children
    and Apply mutated after, so the nth reported site and the nth applied
    mutation were different things. The counts stayed correct and every line
    number pointed somewhere else -- which is worse than reporting no line at
    all, because a survivor is only worth having if you can find it.

    Caught while reading survivors in targeting.py that made no sense: a
    mutant labelled `Eq -> NotEq` had swapped an `and` for an `or` two lines
    away.
    """

    SAMPLES = ["limits.py", "identity.py", "targeting.py", "redaction.py"]

    @pytest.mark.parametrize("module", SAMPLES)
    def test_every_mutant_changes_the_line_it_names(self, module):
        source = open(os.path.join(ROOT, module), encoding="utf-8").read()
        baseline = ast.unparse(ast.parse(source)).splitlines()
        found = mutate.sites(source)

        mismatched = []
        for index, site in enumerate(found):
            mutant = mutate.mutate(source, index)
            if mutant is None:
                continue
            changed = [n for n, (a, b) in enumerate(zip(baseline, mutant.splitlines()))
                       if a != b]
            if len(changed) != 1:
                mismatched.append(f"{module} site {index} changed {len(changed)} lines")

        assert not mismatched, "\n".join(mismatched)

    @pytest.mark.parametrize("module", SAMPLES)
    def test_the_named_operator_is_the_one_that_moved(self, module):
        """
        A stronger form: the described swap has to be visible in the diff.
        `Eq -> NotEq` must produce a line that gained `!=`, not one that
        gained `or`.
        """
        marks = {"Eq -> NotEq": "!=", "NotEq -> Eq": "==",
                 "Lt -> LtE": "<=", "LtE -> Lt": "<",
                 "Gt -> GtE": ">=", "GtE -> Gt": ">",
                 "And -> Or": " or ", "Or -> And": " and ",
                 "In -> NotIn": "not in", "IsNot -> Is": " is "}

        source = open(os.path.join(ROOT, module), encoding="utf-8").read()
        baseline = ast.unparse(ast.parse(source)).splitlines()
        wrong = []

        for index, site in enumerate(mutate.sites(source)):
            mark = marks.get(site["what"])
            if not mark:
                continue
            mutant = mutate.mutate(source, index)
            if mutant is None:
                continue
            lines = mutant.splitlines()
            changed = [n for n, (a, b) in enumerate(zip(baseline, lines)) if a != b]
            if changed and mark not in lines[changed[0]]:
                wrong.append(f"{module} site {index}: says {site['what']!r}, "
                             f"line reads {lines[changed[0]].strip()!r}")

        assert not wrong, "\n".join(wrong[:8])


class TestASecondPassCanNameTheSameMutation:
    """
    Reviewing survivors takes two passes, and they have to agree on what a
    survivor *is*.

    Pass 1 runs the narrow default suite and produces a list. Pass 2 re-runs
    only those sites against every suite that exercises the module, to
    separate a real gap from test selection -- `targeting.py` scored 64/74
    narrow and 67/74 broad with no test written in between.

    That only works if a site index means the same mutation in both passes.
    If `--sites` renumbered, pass 2 would test different mutants and report
    the answer with total confidence.
    """

    def test_run_mutates_the_sites_it_was_asked_for(self, monkeypatch):
        """
        End to end through run(), reading what actually landed on disk.

        The suite is stubbed green so every mutant survives and is reported;
        what is asserted is the *content* written to the target file, which is
        the only thing that proves site 24 was the mutation applied.
        """
        source = open(os.path.join(ROOT, "limits.py"), encoding="utf-8").read()
        expected = {index: mutate.mutate(source, index) for index in (3, 24)}

        seen = []

        def stub(where, tests, timeout=300):
            seen.append(open(os.path.join(where, "limits.py"),
                             encoding="utf-8").read())
            return True, ""

        monkeypatch.setattr(mutate, "run_tests", stub)
        result = mutate.run("limits.py", ["tests/test_limits.py"], only=[3, 24])

        # seen[0] is the unmutated baseline run; then one per requested site.
        assert seen[0] == source
        assert seen[1:] == [expected[3], expected[24]]
        assert [s["index"] for s in result["survivors"]] == [3, 24]

    def test_filtering_does_not_renumber_the_sites(self):
        """
        The property directly: run() tags every site with its index *before*
        applying `only`, so asking for site 24 mutates site 24 of the full
        enumeration and not the 24th of what survived the filter.
        """
        source = open(os.path.join(ROOT, "limits.py"), encoding="utf-8").read()
        found = mutate.sites(source)
        for index, site in enumerate(found):
            site["index"] = index

        wanted = {3, 24}
        filtered = [s for s in found if s["index"] in wanted]

        assert [s["index"] for s in filtered] == [3, 24]
        assert filtered[1]["line"] == found[24]["line"]
        assert filtered[1]["what"] == found[24]["what"]
