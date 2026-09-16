"""
Paired A/B for a change to what the model is told.

CONTRIBUTING asks for a before/after whenever the prompt changes, and
run_eval.py can only measure one arm at a time. Running it twice puts the arms
twenty minutes apart, which on a laptop that throttles makes an arm-level
difference indistinguishable from the machine warming up. Here both arms of
each run are adjacent and which one leads alternates, so ordering effects
cancel.

Exactly one variable per invocation, in one of three shapes:

    # a paragraph sliced off the end of SYSTEM_PROMPT (the original mode)
    python evals/ab_prompt.py --repeat 3

    # one sentence of SYSTEM_PROMPT, or of one tool's docstring, rewritten
    python evals/ab_prompt.py --case job_killed_by_its_own_deadline \\
        --replace "137 is SIGKILL:" --with "An exit code above 128 is a signal:"
    python evals/ab_prompt.py --case image_never_pulled_by_policy \\
        --target tool:list_deployments --replace "..." --with "..."

    # the same case asked about a different object, graded identically
    python evals/ab_prompt.py --case image_never_pulled_by_policy \\
        --variant-question "The ledger-api deployment in ... Why?"

**Every run proves its variable arrived.** `agent._chat` is wrapped for the
duration and captures the system prompt and the rendered tool description the
provider was actually handed, on every round. A variant run whose captured text
lacks the new wording, or a control run that carries it, is recorded as void
and never scored -- without that, "the change made no difference" and "the
change never reached the model" are the same number. The original harness had
no such check; see defect 50.

**It was broken from 2781d7e until 2026-09-14 and nothing noticed.** grade()
gained a third return value and this file still unpacked two, so the first
graded run raised ValueError before a single record was written.

Pass rate is the regression check. It cannot show a prompt worked, only that
it did no harm, so this also counts hedge markers and answer length: the
effect a wording change is usually reaching for, and the cost it usually pays.
"""

import argparse
import datetime as dt
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cases import CASES  # noqa: E402
from ollama_state import resident  # noqa: E402
from run_eval import (  # noqa: E402
    fixtures_present,
    grade,
    provider_failed,
    write_records,
)

import agent  # noqa: E402
import routers.k8s_pods_info as k8s  # noqa: E402

DEFAULT_MARKER = "Never state an inference as if you measured it."

HEDGES = [
    "likely", "probably", "worth checking", "appears", "appear to", "suggests",
    "suggest that", "may be", "might be", "possibly", "presumably", "seems",
    "unclear", "cannot confirm", "can't confirm", "not measured", "assuming",
]


def hedges(text):
    low = text.lower()
    return [h for h in HEDGES if re.search(rf"\b{re.escape(h)}", low)]


def rendered_description(tools, name):
    """
    The description of tool `name` as the provider will send it.

    The two backends take different things: Ollama is handed the callables and
    builds each schema by parsing the docstring itself, the OpenAI-compatible
    one is handed finished dicts. Either way this reads the text that goes on
    the wire rather than the docstring it came from, because the two parsers
    keep different amounts of it -- tool_schema keeps the first paragraph,
    Ollama's keeps everything before `Args:` -- and a sentence edited into the
    part one of them drops would otherwise look delivered.
    """
    for tool in tools:
        if isinstance(tool, dict):
            fn = tool.get("function", {})
            if fn.get("name") == name:
                return fn.get("description", "")
        elif getattr(tool, "__name__", None) == name:
            from ollama._utils import convert_function_to_tool
            return convert_function_to_tool(tool).function.description or ""
    return None


class Capture:
    """
    Wraps agent._chat for one run and keeps what each round was actually sent.

    `seen(text)` answers the only question an arm needs answered: did every
    model call in this run carry `text`? Every, not any -- a run is one
    conversation, and a variable that reached round one and not round three
    is not the arm it claims to be.
    """

    def __init__(self, tool=None):
        self.tool = tool
        self.sent = []

    def __enter__(self):
        self.original = agent._chat

        def capturing(model, messages, think, timeout=None):
            system = next(
                (m.get("content", "") for m in messages
                 if isinstance(m, dict) and m.get("role") == "system"), "")
            text = system
            if self.tool:
                text = rendered_description(
                    agent._backend().tools(agent.TOOLS), self.tool) or ""
            self.sent.append(text)
            return self.original(model, messages, think, timeout=timeout)

        agent._chat = capturing
        return self

    def __exit__(self, *exc):
        agent._chat = self.original
        return False

    def seen(self, text):
        return bool(self.sent) and all(text in sent for sent in self.sent)

    def absent(self, text):
        return all(text not in sent for sent in self.sent)


def arrival(capture, arm, old, new):
    """
    None when the arm's variable reached the model, else why it did not.

    `old` may be empty (marker mode, where the variant is a deletion); `new`
    may be empty for the same reason. A check against an empty string is
    skipped rather than trivially passed -- `"" in text` is always true.
    """
    if not capture.sent:
        return "no model call was made, so nothing was delivered"
    want, avoid = (new, old) if arm == "variant" else (old, new)
    if want and not capture.seen(want):
        return f"{arm} arm: {want[:60]!r} missing from what the model was sent"
    # The replacement may legitimately contain the original (an appended
    # sentence), in which case its absence cannot be demanded of the variant.
    if avoid and not (arm == "variant" and avoid in want) and not capture.absent(avoid):
        return f"{arm} arm: {avoid[:60]!r} still present in what the model was sent"
    return None


def run(case, arm, setup, model):
    question = setup["questions"][arm]
    agent.SYSTEM_PROMPT = setup["prompts"][arm]
    tool = setup.get("tool")
    if tool:
        agent.TOOLS[tool].__doc__ = setup["docs"][arm]

    env = setup.get("env")
    saved = None
    if env:
        saved = os.environ.get(env[0])
        if arm == "variant":
            os.environ[env[0]] = env[1]
        else:
            os.environ.pop(env[0], None)

    was_resident = resident(model)
    load_before = os.getloadavg()[0]
    began_at = dt.datetime.now(dt.UTC).isoformat()
    started = time.time()
    with Capture(tool=tool) as capture:
        try:
            # evidence=True is what puts `draft` and `evidence` in the result;
            # ask() drops both by default. Without it every record this file
            # wrote before 2026-09-15 carries neither, so none of the defect 50
            # or 46 A/B runs can be replayed against a future checker.
            result = agent.ask(question, model=model, evidence=True)
        except ConnectionError as exc:
            # Infrastructure, not the model. run_eval.py aborts here for the
            # same reason: scoring it reports a plausible-looking low number
            # for a run where nothing happened, which is worse than no number.
            print(f"\nollama is unreachable: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
        except Exception as exc:  # noqa: BLE001
            result = {"answer": f"ERROR: {exc}", "tool_calls": [],
                      "confidence": "ungrounded"}

    record = {
        "case": case["name"],
        # summarise.py groups on "model", so the arms come out as two rows of
        # the table it already prints.
        "model": f"{model}-{arm}",
        "arm": arm,
        "variable": setup["variable"],
        "question": question,
        "context": k8s.active_context(),
        "think": agent.THINK,
        # None means the probe failed, which is not the same as "absent".
        "model_resident": was_resident,
        "started_at": began_at,
        "load_before": round(load_before, 2),
        "load_after": round(os.getloadavg()[0], 2),
        "seconds": round(time.time() - started, 1),
        "rounds_sent": len(capture.sent),
    }

    # Infrastructure first. On 2026-09-15 Ollama answered one round with a 500
    # and restarted; the run's answer became "ERROR: Server disconnected
    # without sending a response." with no tool called, and this file scored
    # it FAIL on the variant arm. Its prompt *had* been delivered on the round
    # that failed, so the arrival check alone passed it. run_eval.py already
    # voids exactly this shape; the harness now uses the same rule.
    if env:
        if saved is None:
            os.environ.pop(env[0], None)
        else:
            os.environ[env[0]] = saved
        record["env"] = {env[0]: env[1] if arm == "variant" else None}
    # What the loop was handed rather than went and got. Recorded for every
    # arm: a control run carrying any is a switch that leaked, and a variant
    # run carrying none is one whose question gave it nothing to fetch.
    record["prefetched"] = sum(
        1 for call in result.get("tool_calls", []) if call.get("prefetched"))

    why_void = provider_failed(result) or (None if env else arrival(
        capture, arm, setup["old"], setup["new"]))
    if why_void:
        record.update({"void": True, "void_reason": why_void, "passed": None,
                       "answer": result.get("answer", "")})
        return record

    ok, why, notes = grade(case, result)
    answer = result.get("answer", "")
    record.update({
        "passed": bool(ok),
        "confidence": result.get("confidence"),
        "unverified": result.get("unverified", []),
        "tools": [c["name"] for c in result.get("tool_calls", [])],
        "arguments": [c.get("arguments", {}) for c in result.get("tool_calls", [])],
        "failures": why,
        "notes": notes,
        # The checker's inputs, so the set can be replayed offline against a
        # future grader -- replay `draft`, never `answer`.
        "evidence": result.get("evidence", []),
        "draft": result.get("draft"),
        "contradictions": result.get("contradictions", []),
        "nudges": result.get("nudges", 0),
        "policies": result.get("policies", 0),
        "answer": answer,
        "chars": len(answer),
        "hedges": hedges(answer),
    })
    return record


def build_setup(args):
    """Both arms' prompt, tool docstring and question, differing in one thing."""
    prompt = agent.SYSTEM_PROMPT
    variant_env = getattr(args, "variant_env", None)
    modes = [bool(args.replace is not None), bool(args.variant_question),
             bool(variant_env)]
    if sum(modes) > 1:
        raise SystemExit(
            "one variable per A/B: --replace, --variant-question or --variant-env")

    setup = {"prompts": {"control": prompt, "variant": prompt},
             "questions": {}, "tool": None, "old": "", "new": "", "env": None}

    if variant_env:
        # A behaviour switch rather than text: the variant runs with KEY=VALUE
        # in the environment, the control with KEY absent. Whether it arrived
        # is not a property of the prompt, so arrival() is not asked; the
        # record carries what the run actually did instead (see run()).
        key, sep, value = variant_env.partition("=")
        if not sep or not key:
            raise SystemExit("--variant-env takes KEY=VALUE")
        setup["env"] = (key, value)
        setup["variable"] = {"kind": "env", "key": key, "value": value}
        return setup

    if args.variant_question:
        setup["variable"] = {"kind": "question", "variant": args.variant_question}
        setup["variant_question"] = args.variant_question
        return setup

    if args.replace is None:
        # The original mode: the paragraph from --marker to the end removed.
        if args.marker not in prompt:
            raise SystemExit(f"marker not in the prompt: {args.marker!r}")
        setup["prompts"]["variant"] = prompt[: prompt.index(args.marker)].rstrip()
        setup["old"] = args.marker
        setup["variable"] = {"kind": "marker", "removed_from": args.marker}
        return setup

    if args.new is None:
        raise SystemExit("--replace needs --with")

    if args.target == "prompt":
        source = prompt
    elif args.target.startswith("tool:"):
        tool = args.target.split(":", 1)[1]
        if tool not in agent.TOOLS:
            raise SystemExit(f"no tool named {tool!r}")
        source = agent.TOOLS[tool].__doc__ or ""
        setup["tool"] = tool
    else:
        raise SystemExit("--target is prompt or tool:<name>")

    # Exactly once. str.replace with a count hits the first match in the text,
    # which is not necessarily the sentence the author was looking at.
    count = source.count(args.replace)
    if count != 1:
        raise SystemExit(f"--replace text occurs {count} times in {args.target}; "
                         "it must occur exactly once")
    changed = source.replace(args.replace, args.new)
    if setup["tool"]:
        setup["docs"] = {"control": source, "variant": changed}
    else:
        setup["prompts"]["variant"] = changed
    setup["old"], setup["new"] = args.replace, args.new
    setup["variable"] = {"kind": "replace", "target": args.target,
                         "old": args.replace, "new": args.new}
    return setup


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=agent.MODEL)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--case", help="only this case, for a deeper n on one fault")
    parser.add_argument("--marker", default=DEFAULT_MARKER,
                        help="marker mode: the variant drops the prompt from here on")
    parser.add_argument("--replace", help="text to rewrite in the variant arm")
    parser.add_argument("--with", dest="new", help="what the variant arm says instead")
    parser.add_argument("--target", default="prompt", help="prompt, or tool:<name>")
    parser.add_argument("--variant-question",
                        help="the variant arm asks this instead of the case's question")
    parser.add_argument("--variant-env",
                        help="KEY=VALUE set for the variant arm and unset for the control")
    parser.add_argument("--context", help="kubeconfig context to measure against")
    parser.add_argument("--json", default="results/prompt-ab.json")
    args = parser.parse_args()

    setup = build_setup(args)

    cases = [c for c in CASES if not args.case or c["name"] == args.case]
    if not cases:
        print(f"no case named {args.case!r}", file=sys.stderr)
        return 2
    if setup.get("variant_question") and len(cases) != 1:
        print("--variant-question needs --case", file=sys.stderr)
        return 2

    if args.context:
        k8s.use_context(args.context)
    absent = fixtures_present(cases)
    if absent:
        for needs, where in absent.items():
            print(f"{needs} has not been applied: no pods in {', '.join(where)}",
                  file=sys.stderr)
        return 2

    records = []
    total = len(cases) * args.repeat * 2
    done = 0
    print(f"model={args.model} cases={len(cases)} repeat={args.repeat} runs={total} "
          f"context={k8s.active_context()} think={'on' if agent.THINK else 'off'} "
          f"variable={setup['variable']}", flush=True)

    try:
        for rep in range(args.repeat):
            for index, case in enumerate(cases):
                setup["questions"] = {
                    "control": case["question"],
                    "variant": setup.get("variant_question") or case["question"],
                }
                # Alternate which arm leads, so a warm-cache or thermal
                # advantage does not always land on the same one.
                arms = ("variant", "control") if (rep + index) % 2 else ("control", "variant")
                for arm in arms:
                    record = run(case, arm, setup, args.model)
                    records.append(record)
                    done += 1
                    verdict = ("VOID" if record.get("void")
                               else "pass" if record["passed"] else "FAIL")
                    print(
                        f"[{done:>3}/{total}] {arm:<7} {record['case']:<32} "
                        f"{verdict} {record['seconds']:>6.1f}s "
                        f"{record.get('confidence') or '-':<9} "
                        f"{record.get('void_reason') or record.get('failures') or ''}",
                        flush=True,
                    )
                    write_records(args.json, records)
    finally:
        if setup.get("tool"):
            agent.TOOLS[setup["tool"]].__doc__ = setup["docs"]["control"]

    for arm in ("control", "variant"):
        rows = [r for r in records if r["arm"] == arm and not r.get("void")]
        voids = sum(1 for r in records if r["arm"] == arm and r.get("void"))
        print(
            f"\n{arm:<7} {sum(r['passed'] for r in rows)}/{len(rows)} passed  "
            f"grounded={sum(r['confidence'] == 'grounded' for r in rows)}/{len(rows)}  "
            f"hedged={sum(bool(r['hedges']) for r in rows)}/{len(rows)}  void={voids}"
        )
    write_records(args.json, records, final=True)
    print(f"\nwrote {len(records)} runs to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
