#!/bin/zsh
# Defect 50, 2026-09-14: three paired A/Bs at n=5 pairs, one variable each.
# Correctness criterion frozen beforehand in defect50-criterion.py.
set -u
cd /Users/ravirajput/Projects/AIOps-agent
export TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3
SENT="A container that has not started has no termination to read: its waiting_reason and waiting_message are the kubelet's own account of why, and an image name read from a deployment's spec is not. Several faults with different fixes all look like a wrong image from the spec alone."
POLD="then get_pod_events or get_pod_logs for the underlying cause."
DOLD="Returns why a specific pod is in its current state."

.venv/bin/python -u evals/ab_prompt.py --case image_never_pulled_by_policy --repeat 5 \
  --context kind-kubewhy-r2 --target tool:describe_pod \
  --replace "$DOLD" --with "$DOLD $SENT" \
  --json results/ab/defect50-describe-doc.json

.venv/bin/python -u evals/ab_prompt.py --case image_never_pulled_by_policy --repeat 5 \
  --context kind-kubewhy-r2 --target prompt \
  --replace "$POLD" --with "$POLD $SENT" \
  --json results/ab/defect50-prompt.json

.venv/bin/python -u evals/ab_prompt.py --case image_never_pulled_by_policy --repeat 5 \
  --context kind-kubewhy-r2 \
  --variant-question "The billing-api deployment in the uncovered2 namespace will not start. Why?" \
  --json results/ab/defect50-neutral-image.json

echo DEFECT50-ALL-DONE
