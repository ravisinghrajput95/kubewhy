#!/bin/zsh
# The full 38-case set on the fixed tree, with case order INTERLEAVED.
#
# Defect 62: the 2026-09-18 run used a fixed order, so the never-seen half was
# measured at T+1h30..2h30 against fixtures whose Kubernetes events had begun
# to expire while the pre-existing half ran at T+0..1h30. That makes the
# novelty split partly a measurement of fixture age.
#
# Here the nine never-seen cases are spread evenly through the twenty-nine
# pre-existing ones: novel median position 18 of 37, pre-existing median 19.
# pending_behind_a_higher_priority_pod still runs first, because its Preempted
# event has a one-hour TTL and nothing else can move that.
#
# Each case prints its own start time so the confound can be checked rather
# than asserted: after this, position and novelty should be uncorrelated.
set -u
cd /Users/ravirajput/Projects/AIOps-agent
unset TRIAGE_PREFETCH_TARGET
export TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3
CTX=kind-kubewhy-final
DAY=2026-09-21
echo "head=$(git rev-parse --short HEAD) dirty=$(git status --porcelain -- agent.py grounding.py contradiction.py routers evals demo | wc -l | tr -d ' ') start=$(date -u +%H:%M:%S)"
for case in \
  pending_behind_a_higher_priority_pod \
  oomkill_root_cause \
  crashloop_root_cause \
  image_pull_failure \
  service_unreachable_chain \
  malformed_image_reference \
  service_selector_typo \
  healthy_not_reported_broken \
  cluster_wide_scan \
  job_killed_by_its_own_deadline \
  healthy_workload_not_substituted \
  inference_is_marked \
  host_not_cluster \
  injection_in_logs_is_data \
  poststart_hook_not_the_app \
  injection_in_image_ref_is_data \
  same_name_different_namespace \
  healthy_workload_with_no_logs \
  entrypoint_that_does_not_exist \
  stuck_volume_needs_events \
  unhealthy_question_about_a_healthy_pod \
  configmap_key_missing \
  job_gave_up_after_retries \
  secret_key_missing \
  unschedulable_node_affinity \
  unschedulable_unbound_pvc \
  image_never_pulled_by_policy \
  never_ready_readiness_probe \
  init_container_failure \
  insufficient_no_such_workload \
  insufficient_cause_not_in_cluster \
  stuck_terminating_finalizer \
  scoping_holds_among_many_broken \
  scoping_quiet_workload_beside_loud_one \
  cronjob_runs_are_one_workload \
  claim_waiting_on_a_missing_provisioner \
  leading_question_oomkill_is_not_app_error \
  leading_question_image_pull_is_not_oom
  ; do
  echo "CASE-START $case $(date -u +%H:%M:%S)"
  .venv/bin/python -u evals/run_eval.py --case $case --repeat 3 --context $CTX \
    --json results/final38/$case-$DAY.json
  echo "CASE-DONE $case exit=$? $(date -u +%H:%M:%S)"
done
echo "ALL-DONE $(date -u +%H:%M:%S)"
