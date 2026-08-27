"""
Markup must not be handed to a widget that escapes it.

This is the defect class docs/E2E.md names as the reason a browser suite would
exist at all: `st.error` takes no `unsafe_allow_html`, so a `<span>` in its
body reaches the reader as visible angle brackets. AppTest cannot catch it --
`element.value` is the string that was *submitted*, not the text that was
*painted*, and no assertion over the element tree can ever distinguish those.

So this reads the source instead. A static check needs no browser, no
Streamlit and no rendered contradiction, and it covers every call site rather
than the ones a test happens to exercise. It is a weaker instrument than a
screenshot and a much cheaper one, and for this defect class it is sufficient:
the bug is entirely decided by what the call site passes.

Confirmed in a browser on 2026-08-27 before it was fixed: the contradiction
panel showed `<span class='kw-dim'>rule: ...</span>` as literal text.
"""

import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Widgets whose body is escaped. None of these accepts unsafe_allow_html, so
# markup in them is always a defect rather than a choice.
ESCAPING = {"error", "warning", "info", "success", "toast", "exception"}

TAG = re.compile(r"<[a-zA-Z/][^>]*>")

SURFACES = ["ui.py"]


def escaping_calls(path):
    """(line, widget, source) for every st.<escaping widget>(...) call."""
    source = open(os.path.join(ROOT, path), encoding="utf-8").read()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in ESCAPING:
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "st"):
            continue
        yield node.lineno, func.attr, ast.get_source_segment(source, node) or ""


@pytest.mark.parametrize("path", SURFACES)
def test_no_markup_reaches_a_widget_that_escapes_it(path):
    offenders = [
        f"{path}:{line} st.{widget} passes {TAG.search(segment).group(0)!r}"
        for line, widget, segment in escaping_calls(path)
        if TAG.search(segment)
    ]

    assert not offenders, (
        "these render as visible angle brackets rather than as markup:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse markdown, or move the text to st.markdown(..., "
          "unsafe_allow_html=True)."
    )


@pytest.mark.parametrize("path", SURFACES)
def test_the_check_is_looking_at_something(path):
    """
    A scanner that found no call sites would pass this file forever. The
    console renders alerts on every error path, so zero is wrong.
    """
    assert len(list(escaping_calls(path))) >= 5


def test_the_check_would_catch_the_defect_it_was_written_for():
    """
    The exact call that shipped, as a string, so the detector is proved
    against the real thing rather than against an invented example.
    """
    shipped = (
        "st.error(\n"
        "    f\"**Contradicted** — claimed *{item['claim']}*, but \"\n"
        "    f\"{item['measured']}  \\n\"\n"
        "    f\"<span class='kw-dim'>rule: {item.get('rule','')}</span>\",\n"
        "    icon=\":material/error:\",\n"
        ")"
    )
    assert TAG.search(shipped)


def test_markdown_with_unsafe_html_is_not_flagged():
    """
    The console legitimately renders its own markup through st.markdown with
    unsafe_allow_html=True. A check that flagged those would be turned off.
    """
    assert not any(widget == "markdown" for _, widget, _ in escaping_calls("ui.py"))
