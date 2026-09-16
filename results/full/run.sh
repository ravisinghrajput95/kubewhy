#!/bin/zsh
# The full 38-case set on one tree, one cluster, prefetch on by default.
# One case at a time, 3 repeats each, the preemption case first: its Preempted
# event expires with the event TTL (an hour), and run_eval interleaves repeats
# across cases, which would ask its later repeats after the evidence was gone.
set -u
cd /Users/ravirajput/Projects/AIOps-agent
unset TRIAGE_PREFETCH_TARGET
export TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3
echo "head=$(git rev-parse --short HEAD) dirty=$(git status --porcelain -- agent.py routers evals grounding.py contradiction.py demo | wc -l | tr -d ' ') start=$(date -u +%H:%M:%S)"
for case in "$@"; do
  .venv/bin/python -u evals/run_eval.py --case $case --repeat 3 --context kind-kubewhy-full \
    --json results/full/$case-2026-09-16.json
  echo "CASE-DONE $case exit=$? $(date -u +%H:%M:%S)"
done
echo "ALL-DONE $(date -u +%H:%M:%S)"
