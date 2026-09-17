#!/bin/zsh
# Re-measurement on the gated, tool-result prefetch (3152e9a, harness c27c52c).
# Part 1: the at-risk cases plus the deployer case, n=3, prefetch on (default).
# Part 2: defect 55's seven cases, paired, n=5, variant arm TRIAGE_PREFETCH_TARGET=off.
set -u
cd /Users/ravirajput/Projects/AIOps-agent
unset TRIAGE_PREFETCH_TARGET
export TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3
echo "head=$(git rev-parse --short HEAD) dirty=$(git status --porcelain -- agent.py backends.py inference.py routers evals grounding.py contradiction.py demo | wc -l | tr -d ' ') start=$(date -u +%H:%M:%S)"
for case in insufficient_cause_not_in_cluster injection_in_image_ref_is_data injection_in_logs_is_data \
    same_name_different_namespace healthy_workload_not_substituted scoping_holds_among_many_broken \
    scoping_quiet_workload_beside_loud_one insufficient_no_such_workload unhealthy_question_about_a_healthy_pod \
    leading_question_oomkill_is_not_app_error leading_question_image_pull_is_not_oom inference_is_marked; do
  .venv/bin/python -u evals/run_eval.py --case $case --repeat 3 --context kind-kubewhy-wire \
    --json results/recheck/$case-2026-09-17.json
  echo "CASE-DONE part1 $case exit=$? $(date -u +%H:%M:%S)"
done
for case in oomkill_root_cause crashloop_root_cause never_ready_readiness_probe poststart_hook_not_the_app \
    unschedulable_unbound_pvc image_never_pulled_by_policy healthy_not_reported_broken; do
  .venv/bin/python -u evals/ab_prompt.py --case $case --repeat 5 \
    --variant-env TRIAGE_PREFETCH_TARGET=off --context kind-kubewhy-wire \
    --json results/recheck/ab-$case-2026-09-17.json
  echo "CASE-DONE part2 $case exit=$? $(date -u +%H:%M:%S)"
done
echo "ALL-DONE $(date -u +%H:%M:%S)"
