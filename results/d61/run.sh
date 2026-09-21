#!/bin/zsh
# Defect 61 closed: confirm the fix removes the effect, then re-measure the
# never-seen half on the fixed tree.
#
# Part 1 is the same paired A/B that found it (control = prefetch on, which is
# now the FIXED behaviour; variant = off). Before the fix: control 1/5 against
# variant 5/5, Fisher p = 0.0476. If the fix works the two arms now agree.
# Part 2 runs the never-seen half, one run_eval invocation per case, with
# pending_behind_a_higher_priority_pod first because its Preempted event has a
# one-hour TTL.
set -u
cd /Users/ravirajput/Projects/AIOps-agent
unset TRIAGE_PREFETCH_TARGET
export TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3
CTX=kind-kubewhy-d61
DAY=2026-09-21
echo "head=$(git rev-parse --short HEAD) dirty=$(git status --porcelain -- agent.py grounding.py contradiction.py routers evals | wc -l | tr -d ' ') start=$(date -u +%H:%M:%S)"
.venv/bin/python -u evals/ab_prompt.py --case job_killed_by_its_own_deadline \
  --repeat 5 --variant-env TRIAGE_PREFETCH_TARGET=off --context $CTX \
  --json results/d61/ab-job_killed_by_its_own_deadline-$DAY.json
echo "AB-DONE exit=$? $(date -u +%H:%M:%S)"
for case in pending_behind_a_higher_priority_pod malformed_image_reference \
    job_killed_by_its_own_deadline poststart_hook_not_the_app \
    entrypoint_that_does_not_exist job_gave_up_after_retries \
    image_never_pulled_by_policy stuck_terminating_finalizer \
    claim_waiting_on_a_missing_provisioner; do
  .venv/bin/python -u evals/run_eval.py --case $case --repeat 3 --context $CTX \
    --json results/d61/$case-$DAY.json
  echo "CASE-DONE $case exit=$? $(date -u +%H:%M:%S)"
done
echo "ALL-DONE $(date -u +%H:%M:%S)"
