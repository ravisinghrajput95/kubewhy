#!/bin/zsh
# Defect 46, 2026-09-14: the prompt's "137 is SIGKILL" against a numeral-free
# version of the same lesson, paired at n=5 on the case it cost and the case it
# was written for. Outcome criterion frozen beforehand in defect46-criterion.py.
set -u
cd /Users/ravirajput/Projects/AIOps-agent
export TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3
OLD="137 is SIGKILL: it says"
NEW="An exit code above 128 is a signal: it says"

.venv/bin/python -u evals/ab_prompt.py --case job_killed_by_its_own_deadline --repeat 5 \
  --context kind-kubewhy-r2 --target prompt --replace "$OLD" --with "$NEW" \
  --json results/ab/defect46-job-deadline.json

.venv/bin/python -u evals/ab_prompt.py --case scoping_quiet_workload_beside_loud_one --repeat 5 \
  --context kind-kubewhy-r2 --target prompt --replace "$OLD" --with "$NEW" \
  --json results/ab/defect46-liveness-kill.json

echo DEFECT46-ALL-DONE
