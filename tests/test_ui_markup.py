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

The same defect has a second half, added on 2026-09-01: markup handed to a
widget that *does* render it, with the flag that renders it turned off.
`st.markdown(x, unsafe_allow_html=False)` escapes exactly as `st.error` does
and reaches the reader as the same visible angle brackets. Mutation testing
found all eleven of ui.py's `unsafe_allow_html=True` flipped to False without
a single test noticing. `TestMarkupIsNotHandedToAWidgetToldToEscapeIt` below
is the static half; `TestRenderedMarkupIsRenderedAsMarkup` in `test_ui.py` is
the rendered half, and it is possible because the element tree *can* see this
one -- `allow_html` is a field on the markdown proto, unlike `st.error`, which
has no such flag at all and escapes in the browser.
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


def markdown_calls(path):
    """(line, call node, source) for every st.markdown(...) call."""
    source = open(os.path.join(ROOT, path), encoding="utf-8").read()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "markdown":
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "st"):
            continue
        yield node.lineno, node, ast.get_source_segment(source, node) or ""


def allow_html_of(node):
    """True, False, or None where the argument is absent."""
    for keyword in node.keywords:
        if keyword.arg == "unsafe_allow_html":
            if isinstance(keyword.value, ast.Constant):
                return keyword.value.value
            return "not-a-constant"
    return None


class TestMarkupIsNotHandedToAWidgetToldToEscapeIt:
    """
    The other half of the defect above. `st.markdown` renders markup only
    because the call says so, and a call that says otherwise escapes it.

    This is static for the same reason the check above is: it covers every
    call site rather than the ones a test happens to drive. The rendered
    counterpart in `test_ui.py` covers what the exercised paths paint.
    """

    @pytest.mark.parametrize("path", SURFACES)
    def test_no_markdown_call_is_told_to_escape(self, path):
        """
        `unsafe_allow_html=False` is the default, so writing it means either a
        deliberate escape -- which belongs in a widget that escapes -- or a
        flag that has been flipped. Neither is right here.
        """
        offenders = [
            f"{path}:{line} st.markdown(..., unsafe_allow_html={value!r})"
            for line, node, _ in markdown_calls(path)
            for value in [allow_html_of(node)]
            if value is not None and value is not True
        ]

        assert not offenders, (
            "these escape their own markup and render as visible angle "
            "brackets:\n  " + "\n  ".join(offenders)
        )

    @pytest.mark.parametrize("path", SURFACES)
    def test_markup_written_inline_asks_for_markup(self, path):
        """
        A tag in the call itself, with the flag missing entirely. The default
        escapes, so this is the same defect written a different way.
        """
        offenders = [
            f"{path}:{line} st.markdown passes {TAG.search(segment).group(0)!r}"
            for line, node, segment in markdown_calls(path)
            if TAG.search(segment) and allow_html_of(node) is None
        ]

        assert not offenders, (
            "markup with no unsafe_allow_html=True:\n  " + "\n  ".join(offenders)
        )

    @pytest.mark.parametrize("path", SURFACES)
    def test_the_check_is_looking_at_something(self, path):
        """
        The counter. A walker that matched no st.markdown call would pass
        both tests above on any source at all, including source with the
        defect in it. The console renders its own markup throughout, so a
        low count here means the walker broke, not that the page got simpler.
        """
        calls = list(markdown_calls(path))
        flagged = [node for _, node, _ in calls if allow_html_of(node) is True]

        assert len(calls) >= 10
        assert len(flagged) >= 10, "nothing renders markup; the walker is blind"

    def test_the_check_would_catch_a_flipped_flag(self):
        """
        Proved against the mutation the survey actually made, rather than
        against an invented example: site 42 of ui.py, `True` -> `False`.
        """
        flipped = ast.parse(
            "st.markdown(\n"
            "    f\"<div class='kw-next'><b>Recommended next step</b></div>\",\n"
            "    unsafe_allow_html=False,\n"
            ")"
        ).body[0].value

        assert allow_html_of(flipped) is False
