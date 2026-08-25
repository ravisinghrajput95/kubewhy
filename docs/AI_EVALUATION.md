# kubewhy — AI evaluation methodology

This describes how kubewhy's diagnostic behaviour is measured, what the numbers
mean, and — at least as importantly — what they do not.

## The corpus

`evals/cases.py`, 29 scenarios. Each declares:

| field | meaning |
|---|---|
| `name` | scenario id |
| `category` | the fault class it exercises |
| `ground_truth` | the cause, stated as fact about the cluster |
| `required_evidence` | what a defensible answer has to have read |
| `question` | what is asked |
| `expect_all` / `expect_any` | the diagnosis, as substance not wording |
| `forbid` | claims that must not appear |
| `expect_tools` / `forbid_tools` | which tools the answer must be built from |
| `expected_grounding` | the verdicts this scenario may legitimately produce |
| `require_grounded` | the answer must carry no unverified claim |
| `needs` | the fixture file the scenario depends on |
| `payload` | text that must provably have reached the model |

Categories: oomkill (1), crashloop (3), imagepull (1), config (3),
scheduling (2), service (2), readiness (1), entity-scoping (4), grounding (3),
healthy (3), adversarial (2), insufficient-evidence (2), scan (1), scope (1).

**Ground truth is read from the cluster, not from the manifests' intent.** The
scheduling scenarios assert on `FailedScheduling: … didn't match Pod's node
affinity/selector` and `pod has unbound immediate PersistentVolumeClaims`
because those are the strings the API server actually emitted on 2026-08-25.

**No scenario's ground truth is a matter of opinion.** A scenario whose correct
answer depends on taste would measure the grader, not the agent.

### Two categories that are not about being right

**insufficient-evidence** — scenarios whose answer is not in the cluster at all
("why is the payments-gateway deployment failing?" when no such workload
exists). A confident root cause is the *failure*; the correct behaviour is to
say the evidence is not there. These declare `insufficient_evidence` among their
acceptable verdicts and forbid a diagnosis.

**entity-scoping** — ask about one workload while louder neighbours are broken
at the same time, and require the answer to stay on the one asked about. This is
scored, not assumed, because the substitution failure is subtle: a correct
verdict with a broken neighbour named beside it is one edit away from being the
wrong answer.

## Metrics, reported separately

Every run records these and **they are never collapsed into one score**. A
single "AI score" cannot distinguish an answer that is wrong from one that is
right but unsupported, and those need different fixes.

1. **RCA correctness** — `passed`, with the reasons it failed
2. **Evidence-supported claims** — `rca.observations`, each with citations
3. **Contradicted claims** — `contradictions` (see below)
4. **Unsupported claims** — `unverified`
5. **Insufficient-evidence handling** — `confidence`
6. **Investigation duration** — `seconds`, and `timing.wall_ms`
7. **Model rounds** — `timing.rounds`, with per-round breakdown
8. **Tool calls** — `tools`, with the arguments actually executed
9. **Re-asks** — `nudges` (named a tool it never called), `policies` (evidence
   the status block provably lacks), `coverage` (workloads left out)
10. **Termination reason** — `deadline_exceeded`, `max_rounds`, or none

## Contradictions are recorded, not scored

`contradiction.py` produces a fifth verdict, `contradicted`: a claim the
evidence measured *against*, as distinct from one the evidence is merely silent
about. Only the first means the answer is wrong.

The grader **records** contradictions and does not fail on them, and that is a
measured decision. Replaying the rule as a hard failure over the 793 recorded
runs that kept both answer and evidence would have flipped 4 previously-passing
runs. Two were read in full:

- **True.** "the workload does not exist in the cluster" for `crasher-svc`,
  while the evidence carried `not_ready_endpoints: ["10.0.0.13"]`. The pod
  exists and is merely unready. The old grader passed this.
- **False.** A correct database-connection diagnosis citing exit code 1, failed
  because the word "oomkilled" appeared later in the answer as a possibility
  being ruled out.

One in two of the runs it would newly fail was a false positive. A grader that
cries wolf on correct work is one people learn to ignore, and then it protects
nothing. So it is a metric.

## Replay before believing a checker change

Any change to grounding, contradiction detection or the grader is replayed over
the recorded corpus **before** it is trusted. This is not ceremony: the first
draft of the contradiction rules produced six false positives and zero true ones
against recorded runs, and two more false positives appeared in the first 432
live runs after that. The corpus under `results/` holds 1652 recorded runs, 793
of them with both `answer` and `evidence` retained, which is what makes replay
possible at all.

Replay the recorded **`draft`**, never the **`answer`** — the answer has already
been through `grounding.verify()` and rewritten.

## Sample size, stated honestly

The baseline is **n=1 per scenario**. That is a smoke test, not a measurement of
accuracy. 29/29 at n=1 has a 95% Wilson interval of roughly [88–100]; 25/29 is
roughly [69–93]. Those intervals overlap heavily, so **n=1 cannot rank two
configurations** and this document does not try to.

Where a comparison is not supported by the sample, the word used is
**UNDETERMINED**, not "roughly equal".

## Model comparison

The corpus is provider-neutral: the same 29 scenarios run against any configured
backend by setting `TRIAGE_INFERENCE_MODE` / `_PROVIDER` / `_MODEL`. Comparing
`local · ollama · qwen3` against a hosted API costs one command each.

At the time of writing only the local configuration has been run against the
full corpus. The hosted comparison is **NOT TESTED**: the API key used earlier
in development was revoked, and inventing a comparison from the handful of
ad-hoc hosted runs that exist would be exactly the statistical claim from an
inadequate sample that this methodology forbids.

## Running it

```bash
kubectl apply -f demo/broken-pods.yaml -f demo/config-faults.yaml \
              -f demo/tricky-pods.yaml -f demo/adversarial.yaml

TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3 \
  caffeinate -is python evals/run_eval.py --json results/baseline.json
```

`caffeinate -is` is not optional for an unattended run. Two months of "Ollama
stalls" turned out to be the laptop sleeping.

The runner **refuses to start** if a fixture a scenario declares in `needs` is
absent, rather than running and reporting failures that are really missing
setup. A missing `demo/adversarial.yaml` used to fail four scenarios and pass
two.
