"""
Tests for the paired A/B harness.

It was broken for longer than anyone used it: grade() gained a third return
value in 2781d7e and evals/ab_prompt.py kept unpacking two, so the first graded
run raised before one record was written. Nothing covered it. These tests are
what would have caught that, and they cover the one property an A/B has to
have before its number means anything -- that the variable reached the model.

No cluster and no model: agent._chat is replaced by a stub that returns a
canned reply, and agent.ask by one that calls it the way the loop does.
"""

import argparse
import importlib.util
import os

import pytest

EVALS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evals")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(EVALS, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ab = _load("ab_prompt")
agent = ab.agent

# A sentence that is in SYSTEM_PROMPT and occurs once, for the replace-mode
# tests. It is prompt content, so it moves when the prompt does -- decision 46
# replaced "137 is SIGKILL:" with this on 2026-09-16 and five tests here failed
# on text that no longer existed. If that happens again, pick another unique
# sentence; the assertion below says which.
PROMPT_SENTENCE = "An exit code above 128 is a signal:"


def test_the_sentence_these_tests_edit_is_in_the_prompt():
    assert agent.SYSTEM_PROMPT.count(PROMPT_SENTENCE) == 1, (
        f"{PROMPT_SENTENCE!r} is no longer a unique sentence of SYSTEM_PROMPT; "
        "the replace-mode tests below need a sentence that is")


CASE = {
    "name": "synthetic",
    "question": "Why is the thing broken?",
    "expect_any": ["finalizer"],
}


def _args(**overrides):
    base = {"marker": ab.DEFAULT_MARKER, "replace": None, "new": None,
            "target": "prompt", "variant_question": None, "variant_env": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def _deliver(monkeypatch, sends=None):
    """
    Stub the model loop. `sends` decides what the stub puts in the system
    message: by default the live SYSTEM_PROMPT, which is what agent._stream
    does; pass a fixed string to simulate a loop that captured the prompt
    before the harness changed it.
    """
    calls = []

    def fake_chat(model, messages, think, timeout=None):
        calls.append(messages)
        return {"message": "ok"}, think

    def fake_ask(question, model=None, evidence=False):
        system = agent.SYSTEM_PROMPT if sends is None else sends
        agent._chat(model, [{"role": "system", "content": system},
                            {"role": "user", "content": question}], None)
        result = {"answer": "held by a finalizer", "tool_calls": [],
                  "confidence": "grounded", "unverified": [],
                  "timing": {"rounds": 1, "model_ms": 900.0, "tool_ms": 24.0,
                             "wall_ms": 930.0}}
        # The real contract: agent.ask drops "evidence" and "draft" unless
        # asked for them. The first version of this stub returned both
        # unconditionally, so the test asserting a record carries the draft
        # passed while the harness never requested it and every real record
        # came back without one.
        if evidence:
            result.update({"evidence": [{"id": "tool-1", "result": "{}"}],
                           "draft": "held by a finalizer"})
        return result

    monkeypatch.setattr(agent, "_chat", fake_chat)
    monkeypatch.setattr(agent, "ask", fake_ask)
    monkeypatch.setattr(ab, "resident", lambda model: True)
    monkeypatch.setattr(ab.k8s, "active_context", lambda: "test")
    return calls


@pytest.fixture(autouse=True)
def _restore_prompt(monkeypatch):
    # run() assigns agent.SYSTEM_PROMPT; monkeypatch puts it back afterwards.
    monkeypatch.setattr(agent, "SYSTEM_PROMPT", agent.SYSTEM_PROMPT)


class TestBuildSetup:
    def test_replace_changes_exactly_that_text_in_the_variant(self):
        old = PROMPT_SENTENCE
        setup = ab.build_setup(_args(replace=old, new="SIGNAL SENTENCE:"))
        control, variant = setup["prompts"]["control"], setup["prompts"]["variant"]
        assert control == agent.SYSTEM_PROMPT
        assert old in control and old not in variant
        assert variant == control.replace(old, "SIGNAL SENTENCE:")

    def test_text_that_is_not_unique_is_refused(self):
        # "the" occurs many times; replacing the first would edit a sentence
        # nobody chose.
        with pytest.raises(SystemExit, match="exactly once"):
            ab.build_setup(_args(replace="the", new="a"))

    def test_absent_text_is_refused(self):
        with pytest.raises(SystemExit, match="0 times"):
            ab.build_setup(_args(replace="no such sentence anywhere", new="x"))

    def test_a_tool_target_edits_the_docstring_and_not_the_prompt(self):
        doc = agent.TOOLS["list_deployments"].__doc__
        old = "Returns deployments with desired, ready and available replica counts."
        setup = ab.build_setup(_args(replace=old, new="NEW TEXT.",
                                     target="tool:list_deployments"))
        assert setup["tool"] == "list_deployments"
        assert setup["prompts"]["control"] == setup["prompts"]["variant"]
        assert setup["docs"]["control"] == doc
        assert "NEW TEXT." in setup["docs"]["variant"]
        # build_setup must not mutate the live function; run() does, per arm.
        assert agent.TOOLS["list_deployments"].__doc__ == doc

    def test_two_variables_at_once_are_refused(self):
        with pytest.raises(SystemExit, match="one variable"):
            ab.build_setup(_args(replace=PROMPT_SENTENCE, new="x",
                                 variant_question="another question?"))


class TestArrival:
    def _capture(self, *sent):
        capture = ab.Capture()
        capture.sent = list(sent)
        return capture

    def test_a_variant_that_carries_the_new_text_arrived(self):
        assert ab.arrival(self._capture("... NEW ...", "... NEW ..."), "variant",
                          "OLD", "NEW") is None

    def test_a_variant_missing_the_new_text_on_any_round_is_void(self):
        why = ab.arrival(self._capture("... NEW ...", "... OLD ..."), "variant",
                         "OLD", "NEW")
        assert why and "missing" in why

    def test_a_control_carrying_the_new_text_is_void(self):
        why = ab.arrival(self._capture("OLD and NEW"), "control", "OLD", "NEW")
        assert why and "still present" in why

    def test_a_run_that_called_no_model_is_void_rather_than_delivered(self):
        # all() over nothing is True; this is the case that would otherwise
        # score an arm that never asked anything.
        assert ab.arrival(self._capture(), "variant", "OLD", "NEW")

    def test_a_variant_that_contains_the_original_may_keep_it(self):
        # An appended sentence: the new text includes the old one.
        assert ab.arrival(self._capture("OLD plus more"), "variant",
                          "OLD", "OLD plus more") is None

    def test_marker_mode_voids_a_variant_that_still_has_the_paragraph(self):
        assert ab.arrival(self._capture("prompt MARKER tail"), "variant", "MARKER", "")
        assert ab.arrival(self._capture("prompt"), "variant", "MARKER", "") is None


class TestRun:
    def test_a_delivered_variant_is_graded_and_records_the_checker_inputs(self, monkeypatch):
        calls = _deliver(monkeypatch)
        setup = ab.build_setup(_args(replace=PROMPT_SENTENCE, new="SIGNAL SENTENCE:"))
        setup["questions"] = {"control": CASE["question"], "variant": CASE["question"]}

        record = ab.run(CASE, "variant", setup, "qwen3")

        assert calls, "the stubbed loop never reached the model call"
        assert "SIGNAL SENTENCE:" in calls[0][0]["content"]
        assert record.get("void") is None
        # The regression: grade() returns three values and this used to raise.
        assert record["passed"] is True
        assert record["notes"] == []
        assert record["draft"] == "held by a finalizer"
        assert record["evidence"], "a record without evidence cannot be replayed"
        assert record["rounds_sent"] == 1
        # A latency A/B is read off the loop's clocks, not the harness's.
        assert record["timing"] == {"rounds": 1, "model_ms": 900.0,
                                    "tool_ms": 24.0, "wall_ms": 930.0}

    def test_the_stub_follows_the_real_ask_contract(self):
        # The stub above only means something if agent.ask really does make
        # the checker's inputs opt-in; if that default ever flips, this says so
        # rather than letting the stub drift from what it imitates.
        import inspect

        parameter = inspect.signature(agent.ask).parameters.get("evidence")
        assert parameter is not None and parameter.default is False

    def test_a_variable_that_never_reached_the_model_is_void_not_scored(self, monkeypatch):
        # The mechanism under test, broken on purpose: the loop sends the
        # prompt as it was before the harness changed it. The answer still
        # passes the case, so without the arrival check this run would be
        # counted as the variant arm scoring a pass.
        _deliver(monkeypatch, sends=agent.SYSTEM_PROMPT)
        setup = ab.build_setup(_args(replace=PROMPT_SENTENCE, new="SIGNAL SENTENCE:"))
        setup["questions"] = {"control": CASE["question"], "variant": CASE["question"]}

        record = ab.run(CASE, "variant", setup, "qwen3")

        assert record["void"] is True
        assert record["passed"] is None
        assert "missing" in record["void_reason"]

    def test_a_provider_that_dropped_the_connection_is_void_not_a_failure(self, monkeypatch):
        # The recorded 2026-09-15 shape: the prompt reached the model on the
        # round that failed, so arrival passes, then the server disconnected
        # and agent.ask raised. That run was scored FAIL on the variant arm.
        _deliver(monkeypatch)

        def dropped(question, model=None, evidence=False):
            agent._chat(model, [{"role": "system", "content": agent.SYSTEM_PROMPT}], None)
            raise RuntimeError("Server disconnected without sending a response.")

        monkeypatch.setattr(agent, "ask", dropped)
        setup = ab.build_setup(_args(replace=PROMPT_SENTENCE, new="SIGNAL SENTENCE:"))
        setup["questions"] = {"control": CASE["question"], "variant": CASE["question"]}

        record = ab.run(CASE, "variant", setup, "qwen3")

        assert record["rounds_sent"] == 1, "the payload must have arrived for this to test anything"
        assert record["void"] is True
        assert record["passed"] is None
        assert "disconnected" in record["void_reason"].lower()

    def test_the_control_arm_is_delivered_unchanged(self, monkeypatch):
        calls = _deliver(monkeypatch)
        setup = ab.build_setup(_args(replace=PROMPT_SENTENCE, new="SIGNAL SENTENCE:"))
        setup["questions"] = {"control": CASE["question"], "variant": CASE["question"]}

        record = ab.run(CASE, "control", setup, "qwen3")

        assert record.get("void") is None
        assert calls[0][0]["content"] == setup["prompts"]["control"]


class TestRenderedDescription:
    """What goes on the wire, for both provider shapes."""

    def test_ollama_parses_the_callable_and_the_edit_arrives(self, monkeypatch):
        func = agent.TOOLS["list_deployments"]
        monkeypatch.setattr(func, "__doc__", "EDITED LINE.\n\n" + (func.__doc__ or ""))
        description = ab.rendered_description(list(agent.TOOLS.values()), "list_deployments")
        assert description is not None and "EDITED LINE." in description

    def test_openai_shape_reads_the_schema_dict(self):
        import tool_schema

        schemas = tool_schema.schemas_for(agent.TOOLS)
        description = ab.rendered_description(schemas, "list_deployments")
        assert description == tool_schema._description(agent.TOOLS["list_deployments"])

    def test_an_unknown_tool_is_none_rather_than_empty(self):
        assert ab.rendered_description(list(agent.TOOLS.values()), "no_such_tool") is None


class TestAnEnvironmentSwitchArm:
    """
    For a behaviour switch rather than text -- the target prefetch -- the
    variant runs with KEY=VALUE set and the control with it absent, and the
    record says what the loop was handed.
    """

    def test_only_the_variant_sees_the_variable_and_it_is_restored(self, monkeypatch):
        import os

        seen = {}

        def fake_chat(model, messages, think, timeout=None):
            return {"message": "ok"}, think

        def fake_ask(question, model=None, evidence=False):
            seen[os.environ.get("TRIAGE_PREFETCH_TARGET")] = True
            agent._chat(model, [{"role": "system", "content": agent.SYSTEM_PROMPT}], None)
            calls = ([{"name": "describe_pod", "arguments": {}, "prefetched": True}]
                     if os.environ.get("TRIAGE_PREFETCH_TARGET") == "on" else [])
            return {"answer": "held by a finalizer", "tool_calls": calls,
                    "confidence": "grounded", "unverified": [], "evidence": [{}],
                    "draft": "x"}

        monkeypatch.setattr(agent, "_chat", fake_chat)
        monkeypatch.setattr(agent, "ask", fake_ask)
        monkeypatch.setattr(ab, "resident", lambda model: True)
        monkeypatch.setattr(ab.k8s, "active_context", lambda: "test")
        monkeypatch.setenv("TRIAGE_PREFETCH_TARGET", "was-set-before")

        setup = ab.build_setup(_args(variant_env="TRIAGE_PREFETCH_TARGET=on"))
        setup["questions"] = {"control": CASE["question"], "variant": CASE["question"]}

        variant = ab.run(CASE, "variant", setup, "qwen3")
        assert os.environ["TRIAGE_PREFETCH_TARGET"] == "was-set-before"
        control = ab.run(CASE, "control", setup, "qwen3")
        assert os.environ["TRIAGE_PREFETCH_TARGET"] == "was-set-before"

        assert set(seen) == {"on", None}
        assert variant["prefetched"] == 1 and variant["env"] == {"TRIAGE_PREFETCH_TARGET": "on"}
        assert control["prefetched"] == 0 and control["env"] == {"TRIAGE_PREFETCH_TARGET": None}
        assert variant.get("void") is None and control.get("void") is None

    def test_a_malformed_switch_is_refused(self):
        with pytest.raises(SystemExit, match="KEY=VALUE"):
            ab.build_setup(_args(variant_env="TRIAGE_PREFETCH_TARGET"))

    def test_an_env_arm_is_one_variable(self):
        with pytest.raises(SystemExit, match="one variable"):
            ab.build_setup(_args(variant_env="K=V", variant_question="another?"))
