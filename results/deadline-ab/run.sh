#!/bin/zsh
# Defect 61, paired: does the target prefetch stop the model calling list_jobs?
#
# Today's record is 0/3 with it on against 18/19 across 19 runs with it off,
# Fisher p = 0.0026 -- but those 19 are other days and other trees, so that is a
# before/after and not an effect. This is the paired arm.
#
# unset means ON since defect 55, so the VARIANT arm is the one that sets off,
# and the analysis is run with --prefetch-arm control.
set -u
cd /Users/ravirajput/Projects/AIOps-agent
unset TRIAGE_PREFETCH_TARGET
export TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3
echo "head=$(git rev-parse --short HEAD) start=$(date -u +%H:%M:%S)"
.venv/bin/python -u evals/ab_prompt.py --case job_killed_by_its_own_deadline \
  --repeat 5 --variant-env TRIAGE_PREFETCH_TARGET=off \
  --context kind-kubewhy-recheck \
  --json results/deadline-ab/ab-job_killed_by_its_own_deadline-2026-09-18.json
echo "DONE exit=$? $(date -u +%H:%M:%S)"
