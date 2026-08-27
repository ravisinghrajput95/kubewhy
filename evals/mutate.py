"""
Mutation testing: break the code on purpose and see whether the tests notice.

A passing suite says the tests agree with the code. It does not say the tests
would have caught the code being wrong, and those are different claims. This
breaks one thing at a time and reports the breakages nothing failed on --
which is where a test is asserting the shape of an answer rather than its
substance.

FUTURE.md has listed this as NOT TESTED since the beginning: "a harness existed
during development and killed 28 guards. It was never committed, so it is not
reproducible." That is the same criticism this project made of the grounding
replay, which is now `evals/replay_grounding.py`. This is the other one.

    python evals/mutate.py limits.py
    python evals/mutate.py grounding.py --tests tests/test_grounding.py
    python evals/mutate.py --self-check
    python evals/mutate.py --all --limit 40

## It never edits your working tree

Every mutation is applied inside a throwaway copy of the repository, and the
tests run there. The alternative -- mutate in place and restore in a `finally`
-- leaves a comment-stripped source file behind the first time the process is
killed at the wrong moment, and this project has already lost an afternoon to a
harness that damaged what it was measuring. Copying costs about 4MB and a
fraction of a second.

`ast.unparse` is what writes the mutant, so the copy loses comments and
normalises formatting. That is invisible to the tests and is the reason the
real file is never the one being rewritten.

## A survivor is a question, not a score

Some mutations do not change behaviour at all -- swapping `<` for `<=` on a
bound that is never hit, incrementing a constant used only in a log line. Those
survive and are not gaps. Mutation score is therefore deliberately **not**
reported as a single number: the useful output is the list of survivors and
their line numbers, to be read one at a time. A tool that printed "87%" would
invite someone to move it to 90% by writing tests for equivalent mutants.

## The baseline check

If the suite is already failing, every mutant "dies" and the run reports
perfect coverage. So the unmutated copy is run first, and a red baseline aborts
before a single mutation is applied.
"""

import argparse
import ast
import copy
import glob
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories a working copy does not need. results/ is symlinked back rather
# than copied: it is 8MB, and tests/test_documented_measurements.py reads it.
SKIP_DIRS = {".git", ".venv", "__pycache__", "results", ".pytest_cache",
             "node_modules", ".ruff_cache"}

COMPARE_SWAPS = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt,
    ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}

BINOP_SWAPS = {ast.Add: ast.Sub, ast.Sub: ast.Add,
               ast.Mult: ast.Div, ast.Div: ast.Mult}


class Sites(ast.NodeVisitor):
    """Every place this module could be broken, in a stable order."""

    def __init__(self):
        self.found = []

    def _add(self, node, kind, description):
        self.found.append({"kind": kind, "line": getattr(node, "lineno", 0),
                           "what": description})

    # Children first, then this node -- the same order Apply uses. When these
    # two disagreed, every reported line number was attributed to the wrong
    # site: the counts were right and the labels pointed elsewhere, which is
    # worse than no labels, because a survivor is only useful if you can find
    # it. Any new operator must be added to both classes in the same shape.
    def visit_Compare(self, node):
        self.generic_visit(node)
        for op in node.ops:
            swap = COMPARE_SWAPS.get(type(op))
            if swap:
                self._add(node, "compare",
                          f"{type(op).__name__} -> {swap.__name__}")

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        other = "Or" if isinstance(node.op, ast.And) else "And"
        self._add(node, "boolop", f"{type(node.op).__name__} -> {other}")

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not):
            self._add(node, "not", "drop `not`")

    def visit_BinOp(self, node):
        self.generic_visit(node)
        swap = BINOP_SWAPS.get(type(node.op))
        if swap:
            self._add(node, "binop", f"{type(node.op).__name__} -> {swap.__name__}")

    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            self._add(node, "bool", f"{node.value} -> {not node.value}")
        elif isinstance(node.value, int) and not isinstance(node.value, bool):
            self._add(node, "int", f"{node.value} -> {node.value + 1}")


class Apply(ast.NodeTransformer):
    """Apply exactly the nth mutation, counting in the same order as Sites."""

    def __init__(self, index):
        self.index = index
        self.seen = 0

    def _take(self):
        """Whether this site is the one to break."""
        mine = self.seen == self.index
        self.seen += 1
        return mine

    def visit_Compare(self, node):
        self.generic_visit(node)
        for position, op in enumerate(node.ops):
            swap = COMPARE_SWAPS.get(type(op))
            if swap and self._take():
                node.ops[position] = swap()
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if self._take():
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        return node

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._take():
            return node.operand
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        swap = BINOP_SWAPS.get(type(node.op))
        if swap and self._take():
            node.op = swap()
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            if self._take():
                return ast.Constant(value=not node.value)
        elif isinstance(node.value, int):
            if self._take():
                return ast.Constant(value=node.value + 1)
        return node


def sites(source):
    visitor = Sites()
    visitor.visit(ast.parse(source))
    return visitor.found


def mutate(source, index):
    """The source with one mutation applied, or None if it changed nothing."""
    tree = Apply(index).visit(copy.deepcopy(ast.parse(source)))
    ast.fix_missing_locations(tree)
    mutated = ast.unparse(tree)
    return None if mutated == ast.unparse(ast.parse(source)) else mutated


def working_copy(into):
    """A copy of the repository that the tests can run in."""
    def ignore(directory, names):
        return {n for n in names if n in SKIP_DIRS}

    shutil.copytree(ROOT, into, ignore=ignore, dirs_exist_ok=True)
    # Symlinked, not copied: 8MB that one test module reads and none writes.
    link = os.path.join(into, "results")
    if not os.path.exists(link):
        os.symlink(os.path.join(ROOT, "results"), link)
    return into


def run_tests(where, tests, timeout=300):
    """True if the suite passed. -x, because a killed mutant needs no more."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-x", "-q",
         "-p", "no:cacheprovider", "--no-header"],
        cwd=where, capture_output=True, text=True, timeout=timeout,
    )
    return out.returncode == 0, out.stdout[-2000:]


def default_tests(module):
    """tests/test_<module>.py, when it exists."""
    name = f"tests/test_{os.path.basename(module)[:-3]}.py"
    return [name] if os.path.exists(os.path.join(ROOT, name)) else []


def run(module, tests, limit=None, verbose=False):
    source = open(os.path.join(ROOT, module), encoding="utf-8").read()
    found = sites(source)
    if limit:
        found = found[:limit]

    with tempfile.TemporaryDirectory(prefix="kubewhy-mutate-") as tmp:
        where = working_copy(os.path.join(tmp, "repo"))
        target = os.path.join(where, module)

        green, output = run_tests(where, tests)
        if not green:
            raise SystemExit(
                "the baseline is already failing, so every mutant would look "
                f"killed and the run would report perfect coverage:\n{output}")

        survivors, killed, skipped = [], 0, 0
        for index, site in enumerate(found):
            mutant = mutate(source, index)
            if mutant is None:
                skipped += 1
                continue

            open(target, "w", encoding="utf-8").write(mutant)
            try:
                passed, _ = run_tests(where, tests)
            except subprocess.TimeoutExpired:
                # A mutant that hangs is killed in the way that matters: the
                # tests would not let it through. Counted as killed rather
                # than reported as a gap.
                passed = False
            finally:
                open(target, "w", encoding="utf-8").write(source)

            if passed:
                survivors.append(site)
                if verbose:
                    print(f"  SURVIVED {module}:{site['line']}  {site['what']}")
            else:
                killed += 1

    return {"module": module, "tests": tests, "killed": killed,
            "survivors": survivors, "equivalent_skipped": skipped}


def report(result):
    total = result["killed"] + len(result["survivors"])
    lines = [f"{result['module']}: {total} mutants, {result['killed']} killed, "
             f"{len(result['survivors'])} survived "
             f"({result['equivalent_skipped']} produced no change and were skipped)"]
    for site in result["survivors"]:
        lines.append(f"    survived  {result['module']}:{site['line']:<5} "
                     f"[{site['kind']}] {site['what']}")
    if result["survivors"]:
        lines.append("  Each survivor is a question, not a defect: some "
                     "mutations cannot change behaviour. Read them one at a "
                     "time and decide.")
    return "\n".join(lines)


def self_check():
    """
    Prove the harness kills a mutation that the tests certainly catch.

    A harness whose mutant never reaches the tests reports every mutation as
    surviving, which reads as catastrophic coverage rather than as a broken
    tool -- and a harness whose tests never really run reports every mutation
    as killed, which reads as perfect coverage. Both are silent. This checks
    the first direction, on a module whose tests are known to be strict.
    """
    module, tests = "limits.py", ["tests/test_limits.py"]
    source = open(os.path.join(ROOT, module), encoding="utf-8").read()

    with tempfile.TemporaryDirectory(prefix="kubewhy-selfcheck-") as tmp:
        where = working_copy(os.path.join(tmp, "repo"))
        target = os.path.join(where, module)

        green, output = run_tests(where, tests)
        if not green:
            raise SystemExit(f"baseline is red before any mutation:\n{output}")

        # DEFAULT_PER_HOUR is asserted by name; changing it must fail.
        broken = source.replace("DEFAULT_PER_HOUR = 60", "DEFAULT_PER_HOUR = 61")
        if broken == source:
            raise SystemExit("self-check cannot find its landmark in limits.py")
        open(target, "w", encoding="utf-8").write(broken)
        passed, _ = run_tests(where, tests)

    if passed:
        raise SystemExit(
            "self-check FAILED: the tests passed against a deliberately broken "
            "limits.py. The harness is not running the code it thinks it is.")
    print("self-check passed: a known-lethal mutation was killed")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("modules", nargs="*", help="source files to mutate")
    parser.add_argument("--tests", nargs="*",
                        help="tests to run (default: tests/test_<module>.py)")
    parser.add_argument("--limit", type=int, help="stop after N mutation sites")
    parser.add_argument("--all", action="store_true",
                        help="every top-level module that has a matching test file")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()

    modules = args.modules
    if args.all:
        modules = [os.path.basename(p)
                   for p in sorted(glob.glob(os.path.join(ROOT, "*.py")))
                   if default_tests(os.path.basename(p))]
    if not modules:
        parser.error("name a module, or pass --all")

    worst = 0
    for module in modules:
        tests = args.tests or default_tests(module)
        if not tests:
            print(f"{module}: no test file found; name one with --tests")
            continue
        result = run(module, tests, args.limit, args.verbose)
        print(report(result))
        worst = max(worst, len(result["survivors"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
