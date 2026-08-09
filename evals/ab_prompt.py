"""
Paired A/B for a system-prompt change.

CONTRIBUTING asks for a before/after whenever the prompt changes, and
run_eval.py can only measure one arm at a time. Running it twice puts the arms
twenty minutes apart, which on a laptop that throttles makes an arm-level
difference indistinguishable from the machine warming up. Here both arms of
each run are adjacent and which one leads alternates, so ordering effects
cancel.

    python evals/ab_prompt.py --repeat 3
    python evals/ab_prompt.py --case crashloop_root_cause --repeat 40

The variable is the final paragraph of agent.SYSTEM_PROMPT, sliced off at
runtime -- so the arms differ by exactly that text, on one cluster, in one
process. Point --marker at a different paragraph to measure a different change.

Pass rate is the regression check. It cannot show a prompt worked, only that
it did no harm, so this also counts hedge markers and answer length: the
effect a wording change is usually reaching for, and the cost it usually pays.
"""

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402
from ollama_state import resident  # noqa: E402
from cases import CASES  # noqa: E402
from run_eval import grade  # noqa: E402

DEFAULT_MARKER = "Never state an inference as if you measured it."

HEDGES = [
    "likely", "probably", "worth checking", "appears", "appear to", "suggests",
    "suggest that", "may be", "might be", "possibly", "presumably", "seems",
    "unclear", "cannot confirm", "can't confirm", "not measured", "assuming",
]


def hedges(text):
    low = text.lower()
    return [h for h in HEDGES if re.search(rf"\b{re.escape(h)}", low)]


def run(case, arm, prompts, model):
    agent.SYSTEM_PROMPT = prompts[arm]
    # Before the clock starts: a run that has to load the weights is not
    # measuring the same thing as one that does not.
    was_resident = resident(model)
    started = time.time()
    try:
        result = agent.ask(case["question"], model=model)
    except ConnectionError as exc:
        # Infrastructure, not the model. run_eval.py aborts here for the same
        # reason: scoring it reports a plausible-looking low number for a run
        # where nothing happened, which is worse than no number at all.
        print(f"\nollama is unreachable: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:  # noqa: BLE001
        result = {"answer": f"ERROR: {exc}", "tool_calls": [], "confidence": "ungrounded"}

    ok, why = grade(case, result)
    answer = result.get("answer", "")
    return {
        "case": case["name"],
        # summarise.py groups on "model", so the arms come out as two rows of
        # the table it already prints.
        "model": f"{model}-{arm}",
        "arm": arm,
        # None means the probe failed, which is not the same as "absent".
        "model_resident": was_resident,
        "passed": bool(ok),
        "seconds": round(time.time() - started, 1),
        "confidence": result.get("confidence"),
        "unverified": result.get("unverified", []),
        "tools": [c["name"] for c in result.get("tool_calls", [])],
        "failures": why,
        "answer": answer,
        "chars": len(answer),
        "hedges": hedges(answer),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=agent.MODEL)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--case", help="only this case, for a deeper n on one fault")
    parser.add_argument("--marker", default=DEFAULT_MARKER, help="paragraph under test")
    parser.add_argument("--json", default="results/prompt-ab.json")
    args = parser.parse_args()

    after = agent.SYSTEM_PROMPT
    if args.marker not in after:
        print(f"marker not in the prompt: {args.marker!r}", file=sys.stderr)
        return 2
    prompts = {"after": after, "before": after[: after.index(args.marker)].rstrip()}

    cases = [c for c in CASES if not args.case or c["name"] == args.case]
    if not cases:
        print(f"no case named {args.case!r}", file=sys.stderr)
        return 2

    records = []
    total = len(cases) * args.repeat * 2
    done = 0
    print(f"model={args.model} cases={len(cases)} repeat={args.repeat} runs={total}", flush=True)

    for rep in range(args.repeat):
        for index, case in enumerate(cases):
            # Alternate which arm leads, so a warm-cache or thermal advantage
            # does not always land on the same one.
            arms = ("after", "before") if (rep + index) % 2 else ("before", "after")
            for arm in arms:
                record = run(case, arm, prompts, args.model)
                records.append(record)
                done += 1
                print(
                    f"[{done:>3}/{total}] {arm:<6} {record['case']:<32} "
                    f"{'pass' if record['passed'] else 'FAIL'} {record['seconds']:>6.1f}s "
                    f"{record['confidence']:<9} hedges={len(record['hedges'])}",
                    flush=True,
                )
                with open(args.json, "w") as fh:
                    json.dump(records, fh, indent=1)

    for arm in ("before", "after"):
        rows = [r for r in records if r["arm"] == arm]
        print(
            f"\n{arm:<6} {sum(r['passed'] for r in rows)}/{len(rows)} passed  "
            f"grounded={sum(r['confidence'] == 'grounded' for r in rows)}/{len(rows)}  "
            f"hedged={sum(bool(r['hedges']) for r in rows)}/{len(rows)}"
        )
    print(f"\nwrote {len(records)} runs to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
