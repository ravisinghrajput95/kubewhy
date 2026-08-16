# Handoff prompt — paste this to start the next session

I'm working on kubewhy at /Users/ravirajput/Projects/AIOps-agent
(github.com/ravisinghrajput95/kubewhy, public, MIT). Air-gapped Kubernetes
root-cause analysis: a local model via Ollama chains read-only tools to explain
*why* a workload is broken. Six surfaces share one tool set — CLI (agent.py,
`--scan`), REST (app.py), MCP (mcp_server.py), watch controller (controller.py),
Streamlit UI (ui.py), Slack via Socket Mode (slack_socket.py).

**State: `main` at `bc2ac8c`, tree clean, 318 tests pass, tags through v0.1.6.
Eleven commits from 2026-08-10 are committed but NOT pushed. No clusters
running anywhere — local or cloud.**

Read README.md and CONTRIBUTING.md first.

## Three rules that are not negotiable

1. **Tools stay read-only.** Nothing creates, updates, patches, deletes,
   scales or evicts. The whole security posture rests on this.
2. **Tool output stays projected.** A raw pod is ~1,700 tokens; never return a
   raw API object. New projections need a token-ceiling assertion.
3. **Errors come back as `{"error": ...}` data, never raised.** The agent loop
   has to survive a failing tool.

## Environment

```bash
cd /Users/ravirajput/Projects/AIOps-agent
.venv/bin/python -m pytest              # 318 tests, no cluster, no model needed
ollama list                             # qwen3 (5.2GB) is the default model
```

Everything below assumes `.venv/bin/python`. Docker Desktop must be running
before `kind`; it is usually not.

```bash
kind create cluster --name triage-demo
kubectl apply -f demo/broken-pods.yaml   # namespace demo — the eval fixtures
kubectl apply -f demo/tricky-pods.yaml   # namespace shop — relational faults
.venv/bin/python agent.py --scan
```

**Always `kind delete cluster --name triage-demo` before finishing**, and
unload the model afterwards if you set a long keep-alive:
`curl -s http://localhost:11434/api/generate -d '{"model":"qwen3","prompt":"x","stream":false,"keep_alive":0}'`

Evals (need a cluster and a model, never run in CI):

```bash
OLLAMA_KEEP_ALIVE=24h .venv/bin/python evals/run_eval.py --repeat 10 --json results/baseline.json
OLLAMA_KEEP_ALIVE=24h .venv/bin/python evals/run_controller_eval.py --repeat 3
.venv/bin/python evals/summarise.py results/*.json
.venv/bin/python evals/ask_ai/validate.py        # CI gate, no model needed
```

**Do not pipe a long eval through `tail` or a narrow `grep`.** Both were done
last session and both destroyed detail that had cost an hour to produce —
`tail -40` ate three cases' results, and a `grep` for `PASS|FAIL` dropped every
failure reason. Redirect to a file and read that.

**`demo/broken-pods.yaml` is what the evals assert against.** Adding pods to
the `demo` namespace changes results — the `cluster_wide_scan` case asks about
the whole cluster, so even the `shop` namespace can contaminate it. A run
started before such a change is void.

## What was settled last session, and how

Two of the six open items closed outright, and two more had their stated cause
overturned by measuring it. Every one arrived with a plausible story attached,
and in three cases the story was wrong — including one where the planned fix
would have weakened the security posture in order to detect something that is
not a fault. **Read pending item 1 carefully: its cause is now known and its
symptom is not fixed.**

**`nightly-sync` got a plan instead of a diagnosis — a race, not a prompt.**
The previous handoff said this needed an A/B via ab_prompt.py. It did not. The
watch reports a pod and the diagnosis runs a minute or two later; `nightly-sync`
runs every minute with `failedJobsHistoryLimit: 2`, so the pod is collected
before the model asks about it. Every tool call returned
`{"error": "kubernetes API error 404: pods "nightly-sync-29772408-4bn2r" not
found"}` — three runs of three — and in one run a pod died *between*
`describe_pod` and `get_pod_logs`. Writing out an investigation plan is the sane
response to having no data. **An A/B on the wording would have measured noise.**

`Controller.still_there()` now re-resolves to a live pod of the same workload
and fault before asking, and posts nothing if the workload has gone quiet. That
did **not** fix the case — 11/16 before and after, `nightly-sync` 0/3 both
times. The substitution fires (`diagnosing_a_replacement_pod`) and the
replacement is collected mid-diagnosis too, because runs take 89–126s against a
pod that lives ~120s.

That work also found **`count_affected` had been returning 1 for every finding
the controller eval ever produced** — it built a bare `client.CoreV1Api()`,
which only has a kubeconfig once `run()` has loaded one, and swallowed the
failure. Both use `_api()` now.

**`OLLAMA_KEEP_ALIVE` was read by nothing.** It is a server-side setting and
the Ollama Python client never reads the environment, so the command
CONTRIBUTING documented set a variable nothing on the path looked at. Measured:
unload the model, run one chat through the client with it exported → the model
comes back expiring in **5.0 minutes**. agent.py now forwards it; the same
measurement reports **1440 minutes**. *Every latency figure published before
2026-08-10 was taken under a five-minute unload window by someone who believed
they had disabled unloading.*

**Secret existence — the PartialObjectMetadata path was dropped, not built.**
Wrong on both halves. It is not undetectable: the kubelet cannot construct the
container and puts the name in the pod's status, so `describe_pod` returns
`CreateContainerConfigError` with `secret "x" not found`, and the volume form
leaves a `FailedMount` event naming it — both with **no Secrets grant of any
kind**. The only invisible case is `optional: true`, which is not a fault. And
it would not have been safe: content negotiation happens *after* authorization,
so RBAC cannot tell a metadata request from a full one, and `list` cannot be
narrowed by `resourceNames`. Demonstrated with one token granted exactly
`list secrets` — names with the `PartialObjectMetadataList` header, the
plaintext password without it. Written up in SECURITY.md.

**Test-suite order dependence — fixed.** `import ui` executes the Streamlit
script in bare mode and leaves `FormData(form_id='ask')` on the process-wide
main `DeltaGenerator`. An autouse fixture in tests/test_ui.py clears it; two
tests pin the mechanism so a Streamlit rename cannot turn the cleanup into dead
code.

Unplanned fixes along the way: all three `evals/ask_ai` scripts failed from the
repo root where their own README says to run them (`build_investigation.py`
opened `findings.yaml`, which does not exist — it is `example-findings.yaml`);
`run_eval.py` wrote its JSON only at the end, so a stopped run left nothing on
disk; and the README had drifted — 13 tools of 14, no `scan_references` or
`GET /references`, "five surfaces" followed by six, a Slack section still
claiming replies were impossible when slack_socket.py ships exactly that, and
no entry for `TRIAGE_STATE_DB`.

## Open defects

- **`nightly-sync` still fails, 0/3.** Cause known (above), fix outstanding —
  see pending item 1. Do not spend an A/B on the prompt wording.
- **Ollama stalls, 371s–1013s against a ~70s median.** The unload hypothesis is
  contradicted: with keep-alive genuinely held, the slow runs landed on
  `model_resident: True`. Contention is the live suspect and is now
  instrumented. **Caveat: the one measured pair (113.8s, 106.3s vs a 60.0s
  median) coincided with a pytest run started by hand on the same laptop, so it
  is a confound, not a result.** Nothing so far explains 1013s.
- ~~**The Helm chart never sets `TRIAGE_STATE_DB`**~~ — fixed.
  `persistence.enabled=true` wires a PVC, the env var and
  `podSecurityContext.fsGroup: 1000`; without the fsGroup the volume arrives
  root-owned and the controller crashloops on
  `sqlite3.OperationalError: unable to open database file`, so the chart now
  refuses to render persistence without one on a non-root pod. Verified
  against `csi-driver-host-path` (`fsGroupPolicy: File`, as Azure Disk has).
  **kind's default StorageClass cannot test this** — local-path hands out a
  world-writable 0777 directory, so the bug does not reproduce, and the
  kubelet does not apply fsGroup to it at all.
- ~~**`run_eval.py` iterates case-major**~~ — fixed 2026-08-15, the day it
  cost a run. The machine died 61 runs in and four of ten cases had never
  executed once, so the suite-wide number was unusable while the early cases
  were oversampled. Now repeat-major: an interruption leaves every case
  sampled roughly equally. It also prints one line per run, because an hour of
  silence is indistinguishable from the hang this suite exists to measure.
- ~~**A vanished workload still spends its budget.**~~ — fixed.
  `Budget.spend()` returns a receipt, carried through the queue, and
  `refund()` hands the slot back when `diagnose()` finds nothing left to look
  at. The old reasoning ("nothing carries the fault, so nothing is being
  suppressed") held for that workload's cooldown and not for the hourly
  ceiling, which is global — twelve collected CronJob pods in an hour
  silenced every other workload in the cluster. A queue overflow refunds for
  the same reason. **A failed diagnosis deliberately does not**: the fault is
  still real, and the spent slot is the only thing pacing retries while
  Ollama is down.
- ~~**The plan detector conflates two behaviours.**~~ — settled.
  `planned_instead_of_looking` is now `tools_named_but_not_called`, which
  reports the fact and takes no view. `grade()` supplies the verdict, because
  it is the only caller that knows the case's root cause: tools named but not
  called **with the root cause missing** is the GKE failure and fails the run;
  **with the root cause present** it is a postscript, and is printed as a
  `~` note rather than scored. The old behaviour could fail a correct answer
  for ending with a suggestion.
- ~~**The controller never reports a volume-referenced ConfigMap or Secret.**~~
  Found and fixed 2026-08-15; evidence in
  `evals/ask_ai/config-reference-findings.yaml`. `STUCK_WHEN_SLOW`
  (`ContainerCreating`, `PodInitializing`) plus `TRIAGE_STUCK_AFTER` (300s,
  `watch.stuckAfterSeconds` in the chart) qualifies those statuses by
  duration instead of adding them to `WATCHED`, which would have diagnosed
  every image pull.

  Two mechanism checks made this work rather than guesswork. **The watch
  re-delivers stuck pods**: each 300s cycle re-lists and replays them as
  `ADDED` — measured over three cycles, both test pods re-delivered every
  time — so the threshold does fire; worst-case detection is the threshold
  plus one cycle. And **`status.start_time` is set on stuck pods** (all four
  fixtures), with `metadata.creation_timestamp` as the fallback.

  The default came from measurement, not taste: 22 healthy pods on the demo
  cluster reached Ready in a median of 21s, max 52s. Verified live afterwards
  — all four fixtures now report, a freshly created pod stays unreported
  through its `ContainerCreating` window, and `nightly-sync` at 17s still
  reports immediately, so the threshold does not delay real faults.
- **Eval `n=3` per case hides flaky cases.** `crashloop_root_cause` really
  passes ~85%, so at n=3 it reads 3/3 about 61% of the time.
- **Grounding cannot check reasoning.** Speculation next to measured facts
  still reads `grounded`. `inference_is_marked` at least tests the labelling.
- ~~**UI search filters only what the scan returned**, not the cluster.~~ —
  fixed 2026-08-15. It still filters the page first, but on no match it falls
  through to `scan_cluster(workload=...)`, which reports one workload by name
  whether or not anything is wrong with it. That is what separates "not on
  this page" from "not in this cluster" — the old message was honest about its
  scope and unusable as an answer, since the workload could be outside the
  limit or healthy and therefore never scanned.

## Pending development, in priority order

**1. Make `nightly-sync` diagnosable.** A diagnosis taking 89–126s cannot be
performed on a pod that lives ~120s; re-resolving once at the start only
narrows the window. Options, none free:

- **Capture the evidence at enqueue time** — read logs and termination state
  while the pod is provably alive and hand that to the diagnosis instead of a
  pod name. Correct, and the largest change: the agent fetches its own evidence
  through tools today, so this means feeding pre-fetched material into the loop.
- **Diagnose Job/CronJob workloads from the Job**, which outlives its pods and
  carries failure counts and conditions — but not the logs, and the logs are
  where `FATAL: upstream returned 503` lives.
- **Raise `failedJobsHistoryLimit` in the demo fixture** — makes the eval pass
  and fixes nothing real. Named only so nobody does it by accident.

**2. Get a real baseline.** Still open, and closer.
`results/interrupted-6cases-n10.json` has **61 runs, six cases complete at
n=10** — taken 2026-08-15 with keep-alive genuinely held (verified: Ollama
reported `expires_at` 24h out during the run). The machine **shut down at
22:32** mid-run, three minutes after the last recorded run started, so four
cases never ran.

**Use the correctness data, not the latency.** Pass/fail does not depend on
wall clock, so 58/60 across the six complete cases stands. The timing was
taken across a window where the machine reached load 15–17 and then died;
that median is not publishable and the README table is untouched.

The useful new number: `crashloop_root_cause` scored **8/10**, which is the
~85% your own note predicted and which n=3 was hiding behind a flattering
3/3. That alone justifies `--repeat 10`.

To finish: rerun all ten cases. Note that `run_eval.py` now iterates
repeat-major, so an interruption leaves every case partially sampled rather
than a few cases complete and the rest absent — which is exactly what went
wrong here.

**3. Settle the stalls.** *Contention is refuted. This is the interesting
outcome, and the search continues.*

Measured 2026-08-15 over 61 runs, `results/interrupted-6cases-n10.json`.
Nine runs exceeded 200s, the worst at **2217s** against a 62s median. All
nine had `model_resident: True`, so the unload hypothesis stays dead — and
`load_before` does not explain them either:

| | median `load_before` |
| --- | --- |
| stalls (>200s) | 2.73 |
| normal runs | 2.06 |

Nothing like an 11× ratio, and **three stalls landed on a demonstrably idle
machine** — 611s at load 1.06, 322s at 1.20, 248s at 1.83, on a 15-CPU box.
So the defect is not about how the benchmark is run.

Two shapes worth chasing next. The stalls hit
`healthy_not_reported_broken`, the **cheapest** case in the suite (median
26s), which then returned 39s, 19s, 20s immediately afterwards — so it is not
the case's difficulty. And they arrive **consecutively**, reproducing the
known "adjacent pairs" observation as adjacent triples. Something with
hysteresis, not per-run randomness.

**The next probe is already built** — it just needs a run. `agent.stream` now
reports `timing` on the answer event and `run_eval` records it: `model_ms`
against `tool_ms`, `round_ms` per model round, `slowest_round_ms`,
`model_share`. Read `slowest_round_ms` first. One round carrying nearly all
the time is a hung request; time spread evenly across rounds is systemic; and
`model_ms + tool_ms` falling well short of the run's `seconds` means the
delay is outside both, which would be the most interesting outcome of the
three.

**4. ~~Ground truth for the new fixtures.~~** Done, as a *separate*
investigation — `evals/ask_ai/config-reference-findings.yaml`
(INV-2026-08-15-002), not appended to `example-findings.yaml`, whose header
describes a different cluster and whose integrity claim covers only its own
run. Covers `billing-worker`, `cert-rotator`, `experiment-runner` and all of
`config-faults`, measured on kind v1.36.1.

The result that matters: these faults split by **env-var versus volume**, not
by ConfigMap-versus-Secret and not by missing-object-versus-missing-key. An
env reference puts the name in the container's waiting message, so
`describe_pod` is sufficient. A volume reference leaves the pod in
`ContainerCreating` with **no waiting message at all** and the name only in a
`FailedMount` event, so `get_pod_events` is required. Any eval case for
`cert-rotator` must therefore expect `get_pod_events` in the trace.

**5. ~~Wire `TRIAGE_STATE_DB` into the chart.~~** Done — `persistence.enabled`
adds the PVC, the env var and `podSecurityContext.fsGroup: 1000`. The fsGroup
is the part that is easy to miss: the volume arrives root-owned, the pod is
UID 1000, and without it the install succeeds and the controller crashloops.
Test it on `csi-driver-host-path`, never on kind's default StorageClass.

**6. Frozen benchmarks are provisional.** The 30 cases in
`evals/ask_ai/tiers.yaml` were chosen on judgement. A benchmark must
*discriminate* — show both passes and failures — and that cannot be known until
the suite has run once. `validate.py` now runs from the repo root, so the CI
gate is available: 427 prompts, 27 categories, 19 controls, clean.

## Not verified

- **Slack reply path has never run.** Socket Mode connects to real Slack, but
  replying needs a real `SLACK_BOT_TOKEN`. The token is shared on request and
  revoked straight afterwards, so **batch every reply-path check into one
  run** — there is no second attempt without asking for it again.
- **EKS.** Wanted, but deliberately gated on cost: run it only when the change
  under test is expected to pass, so it confirms rather than debugs. AKS and
  GKE have both been run against for real, including GKE's exec credential
  plugin and a live token expiry.
- **GKE is available on demand** and is the right cloud for anything needing a
  real managed cluster. AKS is not — that subscription is empty and its
  billing state is `Warned`.
- **`store.py` under more than one replica.** Note the controller Deployment
  hardcodes `replicas: 1` with no value to override it, so this is theoretical
  rather than reachable by configuration.

## Backlog

- **On-demand AI log analysis** (deliberately parked): one model call over an
  already-fetched log, shown *beside* the raw log. Must NOT go in `TOOLS`, and
  needs its own character budget.
- Tighter benchmark `n` for the published README table — 21 runs cannot
  distinguish a perfect agent from one that fails 15% of the time.
- ~~A version constant~~ — done. `version.py` holds `__version__`, the MCP
  server passes it, and a test asserts it matches `Chart.yaml`'s `version` and
  `appVersion`, since Helm cannot read Python and two files carrying one
  number drift silently. Bump all three together when tagging.
- ~~`/ask` is still synchronous.~~ — `/ask/jobs` already detaches it and was
  shipped without the docs catching up: the README claimed in three places
  that no job API existed. Corrected 2026-08-15, with round-trip tests added
  (submit → poll → `done` carrying the answer, and a raising job surfacing as
  `failed` rather than vanishing). `/ask` stays synchronous on purpose — it is
  the obvious thing to curl. What remains genuinely open is that a job result
  lives in one process's store, so it is one replica or nothing.

## Settled — do not reopen without new evidence

- **Removing the hedging sentence: the answer is NO.** It looked compelling at
  p=0.056 and did not replicate. The prompt is unchanged in `agent.py` and
  `mcp_server.py`.
- **PartialObjectMetadata for Secret existence: do not build it** (above).
- Slack credential rotation was dropped from tracking on my instruction. Do not
  re-raise it unprompted.

## Work style

Verify against real systems rather than asserting. State the measurement method
when publishing a number. Report intervals, not point estimates. Commit each
piece once verified and tested; **no Co-Authored-By, no Claude-Session, no
assistant attribution of any kind in commit messages.** Be blunt about what is
untested — and when evidence contradicts your own hypothesis, say so rather
than working around it.

Two sessions running, a symptom filed as "model behaviour" turned out to be
infrastructure. Measure the mechanism before designing a prompt experiment.

Budget note: ~₹260 of ₹300 GCP credit remains. A 1-node e2-small zonal cluster
is ~₹11/hour; e2-standard-4 plus a LoadBalancer is ~₹20–25/hour. Delete in the
same session, and remember a reserved static IP keeps billing after the cluster
is gone.
