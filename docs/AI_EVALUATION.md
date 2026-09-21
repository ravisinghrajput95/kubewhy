# kubewhy — AI evaluation methodology

This describes how kubewhy's diagnostic behaviour is measured, what the numbers
mean, and — at least as importantly — what they do not.

## The corpus

`evals/cases.py`, 38 scenarios. The first 29 are the published baseline below;
the other 9 are fault types the system prompt was never written against, and
`evals/split_by_novelty.py` scores the two halves separately — **96.6% against
55.6% when both were last run on one tree**, which is why the split exists. Each declares:

| field | meaning |
|---|---|
| `name` | scenario id |
| `category` | the fault class it exercises |
| `ground_truth` | the cause, stated as fact about the cluster |
| `required_evidence` | what a defensible answer has to have read |
| `question` | what is asked |
| `expect_all` / `expect_any` | the diagnosis, as substance not wording |
| `forbid` | claims that must not appear |
| `false_statements` | sentences untrue of the fixture, which fail a run even when the expectation is met |
| `expect_tools` / `forbid_tools` | which tools the answer must be built from |
| `expected_grounding` | the verdicts this scenario may legitimately produce |
| `require_grounded` | the answer must carry no unverified claim |
| `needs` | the fixture file the scenario depends on |
| `payload` | text that must provably have reached the model |

Categories: oomkill (1), crashloop (3), imagepull (1), imagename (2), config (3),
scheduling (3), storage (1), service (2), readiness (1), lifecycle (2),
startfailure (1), deadline (1), backofflimit (1), entity-scoping (4),
grounding (3), healthy (3), adversarial (2), insufficient-evidence (2),
scan (1), scope (1).

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
live runs after that. The corpus under `results/` holds 2921 recorded runs, 1876
of them with `draft` and `evidence` retained, which is what makes replay
possible at all.

Replay the recorded **`draft`**, never the **`answer`** — the answer has already
been through `grounding.verify()` and rewritten.

## Sample size and what it supports

The baseline is **29 scenarios × 5 runs = 145 runs per configuration**.

n=5 was chosen over n=3 deliberately: the interesting differences between
configurations are per-scenario, and only n=5 gives enough resolution to test one
(a 0/5 versus 5/5 split is the smallest that reaches p < 0.05 on Fisher exact).
The cost was ~3 hours of local model time against ~2 hours for n=3.

## Paired comparison

Both configurations see the same scenarios, so the unit of analysis is the
**scenario**, not the run. Treating five repeats of one scenario as five
independent facts about the model would overstate the evidence.

`evals/compare_paired.py` pairs at scenario level and runs a two-sided sign test
on the discordant scenarios.

## Results — the generalization split, 2026-09-21

**This is the result the tool is judged on, and it comes first for that
reason.** `evals/split_by_novelty.py` scores the two halves separately, because
the headline is their average and the average describes neither: one half is a
tool that explains faults its author enumerated, the other is a tool that
explains Kubernetes.

qwen3 via Ollama, thinking on, **38 × 3 = 114 runs, 0 voids**, one kind cluster
with all six fixture files, every fault verified presenting by hand, one
`run_eval` invocation per case, **case order interleaved**.

| set | score | 95% CI |
|---|---|---|
| headline, all 38 cases | 104/114, 91.2% | [84.6–95.2] |
| the 29 the prompts were written against | 84/87, **96.6%** | [90.3–98.8] |
| the 9 they were not | 20/27, **74.1%** | [55.3–86.8] |

**Gap 22.5 points, Fisher p = 0.0015**, down from 41.0 points on 2026-09-18.

### Why the case order is interleaved, and why that changed the number

Kubernetes events expire after an hour. A set that runs one half of the corpus
first measures that half against younger fixtures, and defect 62 recorded the
consequence. Interleaved, the two halves meet fixtures of the same age:

| | median minutes into the run | range |
|---|---|---|
| never-seen cases | 68 | 0–150 |
| pre-existing cases | 76 | 5–156 |

**Correlation between novelty and elapsed minutes: −0.038**, against near +1
when the halves ran in blocks. **An earlier run of the never-seen half alone
read 85.2% on these same nine cases and the same tree; interleaved it reads
74.1%** — an 11-point swing from fixture age. Report this correlation with any
future run of the set.

### What the number does and does not support

- **It is not a significant improvement.** Against 2026-09-18's regraded 17/27,
  Fisher p = 0.5587; against defect 47's 8/15, p = 0.1935. Twenty-seven runs
  cannot settle it. This is the first *unconfounded* measurement, not a gain.
- **The lower bound is 55.3%.**
- **A prefetched call counts toward `expect_tools`**, and roughly four runs in
  ten make no tool call of their own, so for those runs this measures reading a
  scan row and one `describe_pod` rather than chaining tools to a cause.

### What the failures were

Eleven, all read by hand rather than counted. Five named the right cause and
missed a bar around it; three named the wrong cause. Reading them produced
defect 67 — a conditional clause reasoning about what *would* have been seen,
scored as a claim.

## Results — the two-configuration comparison, 2026-08-25## Results — the two-configuration comparison, 2026-08-25

| Metric | qwen3 (local) | gpt-4o-mini (API) |
|---|---|---|
| RCA correctness | **127/145** (88%) [81–92] | **132/145** (91%) [85–95] |
| Evidence-supported claims | 570 | 745 |
| Contradicted claims | 9 | 3 |
| Unsupported claims | 42 | 10 |
| `insufficient_evidence` verdicts | 14 | 32 |
| `grounded` verdicts | 106 | 101 |
| Mean tool calls | 2.5 | 2.6 |
| Median model rounds | 4 | 4 |
| Re-asks: named-tool | 27 | 0 |
| Re-asks: evidence policy | 27 | 36 |
| Median / p95 / p99 latency | 73s / 188s / 259s | 6s / 14s / 34s |
| Target extracted | 135/145 | 135/145 |
| Wrong-target rate | 0.7% | 0.0% |

### Overall comparison: UNDETERMINED

- qwen3 better on **3** scenarios, gpt-4o-mini on **7**, **19 identical**
- 10 discordant, two-sided sign test **p = 0.3438**
- *Regraded 2026-09-15 under the current checker (defects 45 and 52): qwen3
  130/145 (90%) [84–94], gpt-4o-mini 132/145, 8 discordant, p = 0.7266 — still
  undetermined. The table above is as recorded.*
- Wilson intervals overlap substantially

**The sample does not separate them on overall correctness.** 91% is a pass rate
on 29 hand-built scenarios in one environment. It is not a diagnostic accuracy
figure and must not be quoted as one.

### Four scenarios that do separate — as leads, not conclusions

| Scenario | qwen3 | gpt-4o-mini | Fisher p |
|---|---|---|---|
| `unschedulable_unbound_pvc` | 5/5 | 0/5 | 0.0079 |
| `unschedulable_node_affinity` | 4/5 | 0/5 | 0.0476 |
| `never_ready_readiness_probe` | 0/5 | 5/5 | 0.0079 |
| `scoping_quiet_workload_beside_loud_one` | 0/5 | 4/5 † | 0.0476 |

† **Corrected 2026-08-28, and the original 5/5 was wrong.** One of
gpt-4o-mini's five answers said the container was killed "due to an
out-of-memory (OOM) condition" while `last_termination.reason` read `error`.
The contradiction checker did not recognise that spelling, so the run scored
`grounded` and the case scored 5/5. Rescoring the recorded drafts against the
fixed checker — the runs themselves are unchanged — gives 4/5 and moves the
p from 0.0079 to 0.0476. The other three rows are unaffected; each was
rescored the same way and did not move. See defect 18 in
[VALIDATION.md](VALIDATION.md).

**None survives Bonferroni correction** for 29 comparisons (p < 0.0017), and at
5-versus-5 the design *cannot* reach that threshold — 0.0079 is the floor. These
are reproducible capability differences worth investigating, **not** evidence
that either model is better.

What they appear to show: qwen3 reads scheduling events and gpt-4o-mini does not
(0/2 scenarios, never calling `get_pod_events`); gpt-4o-mini handles the
readiness case and the quiet-workload scoping case where qwen3 fails 0/5.

**Two of these four were investigated on 2026-08-28 and neither turned out to
be a capability difference in the way the table reads.**
`never_ready_readiness_probe` was qwen3 never calling `get_pod_events` on a
pod whose status block cannot explain itself; a deterministic evidence policy
now sends the run for them, and the case is **5/5**. The scoping case was
partly a checker that could not see the claim it was watching for — see the
dagger above — and is **1/5** after the fix, still open. Neither number
belongs in this table, which is a record of what was measured on 2026-08-25;
they are here as a warning that a discordant row is a lead, not a finding.

### Behaviour, which matters more than the pass rate

The two configurations **fail differently**:

- gpt-4o-mini collects **more** evidence (745 observations vs 570) yet **refuses
  more often** (32 `insufficient_evidence` vs 14).
- qwen3 makes **four times more unsupported claims** (42 vs 10) and three times
  more contradicted ones (9 vs 3). It is more willing to conclude — sometimes
  wrongly: it reads exit code 137 on `slow-starter` and says OOMKilled, which
  `last_termination.reason = error` contradicts, 5/5 times.
- gpt-4o-mini needed **zero** named-tool re-asks; qwen3 needed 27.
- **More tool calls did not mean better reasoning** — mean calls were
  near-identical, 2.6 vs 2.5.

### Latency: environment-specific

~12× at the median and holding at p95 and p99. **One laptop, one cluster, one
local model.** This does not generalise: a different machine, a different local
model or a colocated API would move it. It is reported because it is real in the
tested environment, not because it is a property of the architecture.

## Running it

```bash
kubectl apply -f demo/broken-pods.yaml -f demo/config-faults.yaml \
              -f demo/tricky-pods.yaml -f demo/adversarial.yaml

TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3 \
  caffeinate -is python evals/run_eval.py --repeat 5 --json results/local.json

python evals/report_baseline.py results/local.json
python evals/compare_paired.py results/local.json results/api.json qwen3 gpt-4o-mini
```

`caffeinate -is` is not optional for an unattended run. Two months of "Ollama
stalls" turned out to be the laptop sleeping.

The runner **refuses to start** if a fixture a scenario declares in `needs` is
absent, rather than running and reporting failures that are really missing setup.

`evals/regrade.py` re-applies the current corpus to a recorded run: the answers
are the model's, unchanged, and only the expectations move. Without it, an
expectation fixed after a run started costs hours of model time to apply — and
re-running would change the answers too, which makes an expectation fix
indistinguishable from a different sample.

## Corpus defects found and fixed

Honesty about the corpus matters as much as honesty about the model:

- **Four scenarios could be passed by repeating the question.** `expect_all:
  [["log-shipper"]]` on a question that says "log-shipper" scores a parrot. The
  scoping cases were the worst — staying on the right workload is their whole
  point, and naming it was the entire expectation. Caught by
  `tests/test_eval_graders.py`.
- **Refusals were scored by phrase matching**, and the list missed three correct
  answers across two models: "could not be found", "the tools provided do not
  include", "could not find any information". Now scored on the verdict plus
  `forbid`.
- **One scenario was nondeterministic** — its CronJob pods rotated faster than an
  investigation. Fixed in the fixture.
