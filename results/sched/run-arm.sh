#!/bin/zsh
# One arm of the describe_pod scheduling measurement: both cases at n=5.
# $1 is the arm name (before|after); records carry the commit they ran on.
set -u
cd /Users/ravirajput/Projects/AIOps-agent
export TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3
echo "arm=$1 head=$(git rev-parse --short HEAD) dirty=$(git status --porcelain -- routers agent.py grounding.py contradiction.py | wc -l | tr -d ' ')"
for case in unschedulable_node_affinity unschedulable_unbound_pvc; do
  .venv/bin/python -u evals/run_eval.py --case $case --repeat 5 --context kind-kubewhy-sched \
    --json results/sched-$1-$case-2026-09-15.json
done
echo "ARM-$1-DONE"
