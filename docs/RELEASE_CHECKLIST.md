# kubewhy — release checklist

Run through this before tagging. Each line is a command, not a memory.

## State

- [ ] **Tests pass** — `pytest` (expect 977)
- [ ] **Working tree clean** — `git status --porcelain` returns nothing
- [ ] **Level with origin** — `git status -sb | head -1` shows no ahead/behind

## Version consistency

Four places must agree. `version.py` and `Chart.yaml` are bumped **together**;
the container tag defaults to `.Chart.AppVersion` and is not set independently.

- [ ] `grep __version__ version.py`
- [ ] `grep -E '^version|^appVersion' deploy/chart/Chart.yaml`
- [ ] `grep -n 'tag:' deploy/chart/values.yaml` — image tag stays `""`
- [ ] No hard-coded version in README or docs

## Documentation

- [ ] **README** — capability table, validation table and evaluation numbers
      match the latest run
- [ ] **ARCHITECTURE.md** — reflects the current module layout
- [ ] **SECURITY.md** — every control listed is implemented
- [ ] **INFERENCE.md** — modes and provider status current
- [ ] **VALIDATION.md** — status words correct, defect list complete
- [ ] **AI_EVALUATION.md** — corpus size, n, and results match `results/`
- [ ] **DEMO.md** — fault table matches `demo/*.yaml`
- [ ] **UI.md** — functional PROVEN, browser paint NOT TESTED
- [ ] **FUTURE.md** — nothing in it is described as current capability

## Claims audit

Search the repository and confirm each hit is still true:

```bash
grep -rniE "production.ready|100% accurate|multi-cloud|vllm validated" \
  README.md docs/*.md
grep -rn "EKS\|AKS" README.md docs/*.md      # must not read as "supported"
grep -rnE "[0-9]{3} (tests|passing)" README.md docs/*.md NEXT-SESSION.md
```

- [ ] No "production ready"
- [ ] No generalized AI accuracy claim
- [ ] No vLLM validation claim
- [ ] No EKS support claim
- [ ] Test counts current
- [ ] Screenshots current and not misleading

## Secrets

- [ ] `git log -p | grep -iE 'sk-proj|sk-[a-zA-Z0-9]{20}'` returns nothing
- [ ] `.env` is gitignored and untracked
- [ ] No credential in any committed screenshot or GIF

## Artifacts

- [ ] Chart lints — `helm lint deploy/chart`
- [ ] Chart templates — `helm template deploy/chart >/dev/null`
- [ ] Container builds and `/healthz` answers on the base image
- [ ] `/_stcore/health` answers on the UI image

The `target:base` trap is the one that shipped Streamlit as the base image once.
CI's `docker run` step is what catches it; do not skip it.

## Tag

- [ ] Release version agreed and justified against the change set
- [ ] `git tag -a vX.Y.Z -m "..."` and push the tag
- [ ] Release notes name what changed and what is still NOT TESTED
