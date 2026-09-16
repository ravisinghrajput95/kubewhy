#!/bin/zsh
# Prefetch A/B: TRIAGE_PREFETCH_TARGET=on in the variant, absent in the control,
# arms adjacent and alternating. One results file per case.
set -u
cd /Users/ravirajput/Projects/AIOps-agent
unset TRIAGE_PREFETCH_TARGET
export TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3
echo "head=$(git rev-parse --short HEAD) dirty=$(git status --porcelain -- agent.py routers evals grounding.py contradiction.py | wc -l | tr -d ' ') start=$(date -u +%H:%M:%S)"
for case in "$@"; do
  .venv/bin/python -u evals/ab_prompt.py --case $case --repeat 5 \
    --variant-env TRIAGE_PREFETCH_TARGET=on --context kind-kubewhy-prefetch \
    --json results/prefetch/$case-2026-09-16.json
  echo "CASE-DONE $case exit=$? $(date -u +%H:%M:%S)"
done
echo "ALL-DONE $(date -u +%H:%M:%S)"
