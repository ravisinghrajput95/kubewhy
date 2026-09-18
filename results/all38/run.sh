#!/bin/zsh
# The full 38-case set on one tree, 2026-09-18.
#
# Why this exists: the headline generalization figure ("53.3% on never-seen
# fault types, 90.8% on the rest") was measured 2026-09-13. Since then the
# fixtures were renamed (defect 50), three case bars changed (defect 54),
# describe_pod gained scheduling/claims/preemption (defect 53), the
# contradiction checker was fixed twice (defects 52, 56), and the target
# prefetch went on by default and was then gated and moved into tool results
# (defects 55, 56). No set has run across all 38 cases on one tree since.
#
# One case per run_eval invocation, NOT a single 38-case call: run_eval
# interleaves repeats across cases, and `pending_behind_a_higher_priority_pod`
# rests on a Preempted event whose TTL is one hour. That case runs first, and
# the preemption is re-triggered immediately before this script starts.
set -u
cd /Users/ravirajput/Projects/AIOps-agent
unset TRIAGE_PREFETCH_TARGET          # unset means ON since defect 55
export TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3
CTX=kind-kubewhy-recheck
DAY=2026-09-18
echo "head=$(git rev-parse --short HEAD) dirty=$(git status --porcelain -- agent.py backends.py inference.py routers evals grounding.py contradiction.py demo | wc -l | tr -d ' ') start=$(date -u +%H:%M:%S)"
for case in \
  pending_behind_a_higher_priority_pod \
  oomkill_root_cause crashloop_root_cause image_pull_failure service_unreachable_chain \
  service_selector_typo healthy_not_reported_broken cluster_wide_scan \
  healthy_workload_not_substituted inference_is_marked host_not_cluster \
  injection_in_logs_is_data injection_in_image_ref_is_data same_name_different_namespace \
  healthy_workload_with_no_logs stuck_volume_needs_events \
  unhealthy_question_about_a_healthy_pod configmap_key_missing secret_key_missing \
  unschedulable_node_affinity unschedulable_unbound_pvc never_ready_readiness_probe \
  init_container_failure insufficient_no_such_workload insufficient_cause_not_in_cluster \
  scoping_holds_among_many_broken scoping_quiet_workload_beside_loud_one \
  cronjob_runs_are_one_workload leading_question_oomkill_is_not_app_error \
  leading_question_image_pull_is_not_oom malformed_image_reference \
  job_killed_by_its_own_deadline poststart_hook_not_the_app entrypoint_that_does_not_exist \
  job_gave_up_after_retries image_never_pulled_by_policy stuck_terminating_finalizer \
  claim_waiting_on_a_missing_provisioner; do
  .venv/bin/python -u evals/run_eval.py --case $case --repeat 3 --context $CTX \
    --json results/all38/$case-$DAY.json
  echo "CASE-DONE $case exit=$? $(date -u +%H:%M:%S)"
done
echo "ALL-DONE $(date -u +%H:%M:%S)"
