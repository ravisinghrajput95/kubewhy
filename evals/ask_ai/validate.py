"""
Schema and integrity check for the Ask AI evaluation suite.

An eval suite is code. If it can drift into duplicate IDs, cases with no
prompt, or categories with no controls, then the numbers it produces are not
trustworthy either. This runs in CI.
"""
import os
import sys
from collections import Counter

import yaml

# Resolved against this file, not the working directory. The documented
# command is `python evals/ask_ai/validate.py` from the repo root, which
# failed outright on a bare open("suite.yaml") -- so the CI gate could only
# ever have passed from inside its own directory.
HERE = os.path.dirname(os.path.abspath(__file__))

REQUIRED_CASE = ("id", "prompt")
REQUIRED_CAT = ("id", "name", "intent", "risk", "defaults", "cases")
RISKS = {"critical", "high", "medium", "low"}
ADVERSARIAL = {"C06", "C07", "C08", "C09", "C10", "C11", "C12"}


def load(path):
    with open(os.path.join(HERE, path)) as fh:
        return yaml.safe_load(fh)


def check_suite(doc, errors):
    cats = doc["categories"]
    for cat in cats:
        for field in REQUIRED_CAT:
            if field not in cat:
                errors.append(f"{cat.get('id', '?')}: missing category field {field!r}")
        if cat.get("risk") not in RISKS:
            errors.append(f"{cat['id']}: risk {cat.get('risk')!r} not one of {sorted(RISKS)}")

        d = cat.get("defaults", {})
        for field in ("expected_behaviour", "guardrail", "failure_conditions",
                      "pass_criteria", "checker", "n"):
            if field not in d:
                errors.append(f"{cat['id']}: defaults missing {field!r}")

        for case in cat["cases"]:
            for field in REQUIRED_CASE:
                if field not in case:
                    errors.append(f"{cat['id']}: a case is missing {field!r}")
            blank = not str(case.get("prompt", "")).strip()
            if blank and not case.get("intentionally_blank"):
                # Empty and whitespace prompts are real test cases, but they
                # have to say so, or a case that lost its prompt looks the same.
                errors.append(f"{case.get('id')}: empty prompt without "
                              f"intentionally_blank marker")

        # Every adversarial category needs at least one over-refusal control,
        # or its safety number is unfalsifiable.
        if cat["id"] in ADVERSARIAL:
            if not any(c.get("control") for c in cat["cases"]):
                errors.append(f"{cat['id']}: adversarial category has no control case")

    ids = [c["id"] for cat in cats for c in cat["cases"]]
    for cid, count in Counter(ids).items():
        if count > 1:
            errors.append(f"duplicate case id {cid} ({count}x)")
    return cats, ids


def check_redteam(doc, errors):
    fams = doc["families"]
    ids = []
    for fam in fams:
        if fam.get("risk") not in RISKS:
            errors.append(f"{fam['id']}: bad risk {fam.get('risk')!r}")
        for field in ("expected_behaviour", "failure_conditions"):
            if field not in fam:
                errors.append(f"{fam['id']}: missing {field!r}")
        for case in fam["cases"]:
            if "id" not in case or "prompt" not in case:
                errors.append(f"{fam['id']}: case missing id/prompt")
            ids.append(case.get("id"))
    for cid, count in Counter(ids).items():
        if count > 1:
            errors.append(f"duplicate redteam id {cid} ({count}x)")
    if not any(f["id"] == "RT-CTRL" for f in fams):
        errors.append("redteam has no over-refusal control family")
    return fams, ids


def main():
    errors = []
    suite = load("suite.yaml")
    red = load("redteam.yaml")

    cats, sids = check_suite(suite, errors)
    fams, rids = check_redteam(red, errors)

    attacks = [i for i in rids if not i.startswith("RT-CTRL")]
    controls_red = [i for i in rids if i.startswith("RT-CTRL")]
    controls_suite = [c["id"] for cat in cats for c in cat["cases"] if c.get("control")]

    print("=" * 62)
    print("Ask AI Evaluation Suite — integrity report")
    print("=" * 62)
    print(f"  functional categories      {len(cats)}")
    print(f"  functional cases           {len(sids)}")
    print(f"  red team families          {len(fams)}")
    print(f"  red team attack prompts    {len(attacks)}")
    print(f"  over-refusal controls      {len(controls_suite) + len(controls_red)}"
          f"  ({len(controls_suite)} functional + {len(controls_red)} red team)")
    print(f"  TOTAL PROMPTS              {len(sids) + len(rids)}")
    print()

    by_risk = Counter()
    for cat in cats:
        for case in cat["cases"]:
            by_risk[case.get("risk", cat["risk"])] += 1
    for fam in fams:
        for case in fam["cases"]:
            by_risk[case.get("risk", fam["risk"])] += 1
    print("  severity distribution")
    for r in ("critical", "high", "medium", "low"):
        print(f"    {r:<10} {by_risk[r]:>4}")
    print()

    # Estimated runtime cost, because n per case is what actually decides
    # whether this suite is runnable per-release or per-quarter.
    runs = 0
    for cat in cats:
        for case in cat["cases"]:
            runs += case.get("n", cat["defaults"].get("n", 20))
    runs += len(rids) * red["meta"].get("n_per_case", 200)
    print(f"  total model invocations per full run: {runs:,}")
    print(f"  at 8s/invocation, 20-way parallel:    {runs * 8 / 20 / 3600:.1f} hours")
    print()

    if errors:
        print(f"FAILED — {len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("PASSED — schema, uniqueness and control coverage all clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
