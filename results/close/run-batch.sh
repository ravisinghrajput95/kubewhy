#!/bin/zsh
# One batch of the close-out measurements: each named case at n=5, in order.
# $1 batch name; the rest are case names. Records carry the commit they ran on.
set -u
cd /Users/ravirajput/Projects/AIOps-agent
export TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3
batch=$1; shift
echo "batch=$batch head=$(git rev-parse --short HEAD) dirty=$(git status --porcelain -- routers agent.py grounding.py contradiction.py evals/cases.py evals/run_eval.py demo | wc -l | tr -d ' ') start=$(date -u +%H:%M:%S)"
for case in "$@"; do
  .venv/bin/python -u evals/run_eval.py --case $case --repeat 5 --context kind-kubewhy-close \
    --json results/close-$batch-$case-2026-09-15.json
done
echo "BATCH-$batch-DONE $(date -u +%H:%M:%S)"
