# Handoff prompt — paste this to start the next session

I'm working on kubewhy at /Users/ravirajput/Projects/AIOps-agent
(github.com/ravisinghrajput95/kubewhy, public, MIT). Air-gapped Kubernetes
root-cause analysis: a local model via Ollama chains read-only tools to explain
*why* a workload is broken. Six surfaces share one tool set — CLI (agent.py,
`--scan`), REST (app.py), MCP (mcp_server.py), watch controller (controller.py),
Streamlit UI (ui.py), Slack via Socket Mode (slack_socket.py).

**State: `main` at `9df3a87`, tree clean, 399 tests pass, tags through v0.1.6.
Updated 2026-08-17 (evening). No clusters running anywhere; Docker is stopped
and the model is unloaded.**

**Start with `cluster_wide_scan` dropping a workload from its own summary.**
It is the only case not at 10/10 on the current 100-run set, and unlike the
last "highest-value defect" it has been shown to be real: the tool handed the
model eight failing workloads and the answer listed six. See open defects.

**The wrong-workload substitution was not the agent.** It was the grader
matching a forbidden name anywhere in the answer, so "healthy-web is running
normally; bad-image and memory-hog are unhealthy" scored as a substitution.
Measured before touching anything, which is the only reason the projection
was not rewritten to fix a defect that did not exist. Details below.

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
.venv/bin/python -m pytest              # 399 tests, no cluster, no model needed
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

Evals (need a cluster and a model, never run in CI). **Run them under
`caffeinate`** — on battery this Mac is set to `sleep 1`, so an unattended
benchmark suspends after a minute without keystrokes and the run reports the
nap as its own slowness. That was the stall defect; see below.

```bash
caffeinate -is env OLLAMA_KEEP_ALIVE=24h .venv/bin/python evals/run_eval.py \
    --context kind-triage-demo --repeat 10 --json results/baseline.json
caffeinate -is env OLLAMA_KEEP_ALIVE=24h .venv/bin/python evals/run_controller_eval.py --repeat 3
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

## What was settled on 2026-08-17, evening

**The wrong-workload substitution was a grading defect.** Three sets, all on
kind + qwen3, all re-scored under the corrected grader:

| set | n | old grader | re-scored |
| --- | --- | --- | --- |
| replay, full `list_deployments` projection | 30 | 28/30 | 30/30 |
| replay, projection filtered to `healthy-web` | 30 | 30/30 | 30/30 |
| live, real loop | 20 | 18/20 | 20/20 |

Every failure recorded with its answer text was the same shape: the correct
verdict on `healthy-web`, followed by a true remark that the neighbours are
unhealthy. `forbid` matched the neighbour's name as a substring anywhere in
the answer. The two replay arms are indistinguishable (Fisher exact p=0.49),
so **the projection is not the lever either** — the filtered arm only "wins"
by removing the model's ability to mention a name. Round-1 thinking on the
failing runs shows no substitution intent at all.

`forbid` now reads against whether the case's own expectations were met, the
same way `tools_named_but_not_called` reads against the root cause: unmet it
fails the run, met it is a note that is printed and recorded but not scored.
A case declaring no expectations keeps the unconditional behaviour, so a
forbid-only case cannot quietly become a check that never fails.

**The 2026-08-17 morning baseline's two failures on this case stay
unverified.** Same two reasons, but they predate answer text being kept.

**A new 100-run baseline exists on current `main`:
`results/baseline-n10-2.json`.** 99/100, 95% [95-100], median 54.0s, p95
132.7s, grounded 86/100, `slept_ms` zero on every run, `context` recorded on
every run. `cluster_wide_scan` 7/10 -> 9/10 (the nudge correction),
`healthy_workload_not_substituted` 8/10 -> 10/10 (the grader), everything
else 10/10. **The README table has been published from this set** — that
decision is closed.

**`scan_cluster` rejected the `namespaces` list the model sends.** 2 of 20
live runs called `namespaces=['demo']` and got
`{"error": "AttributeError: 'list' object has no attribute 'split'"}` back.
The loop survived, but those runs took three rounds instead of two, median
38.4s against 18.7s. Lists are accepted now.

**An eval can no longer measure the wrong cluster silently.** A 100-run set
started against kind and, one case in, `current-context` moved to a GKE
cluster something else on the machine had just created; every tool call after
that answered honestly about an empty cluster. `run_eval.py` takes
`--context`, preflights the demo namespace before spending any model time,
prints `context=... demo pods=N` in its header and records the context on
every run. **Run it as `--context kind-triage-demo`**, or with a `KUBECONFIG`
holding only that cluster.

## What was settled on 2026-08-17, earlier

**The stalls were the laptop sleeping.** Pending item 3, below, with the
`pmset` evidence. Two hypotheses died on this before it was measured, and the
answer was the third outcome the timing probe was built to distinguish: the
delay sat outside both the model and the tools.

**A diagnosis that names a tool now calls it.** `crashloop_root_cause` was
failing by writing "Next Step: get_pod_logs" instead of running it —
8/10 before, 10/10 after. The guard cost `cluster_wide_scan` two runs on its
first outing (it answered about the pod it had just drilled into) and the
correction was to quote the question back; 9/10 after that.

**The first honest baseline exists.** 100 runs, ten cases, no naps, 95%
[89-98]. Pending item 2.

**One measurement, three defects.** The baseline separated them cleanly by
whether the run had been nudged: the loop guard's own regression (nudged), the
wrong-workload substitution (never nudged), and a summary that drops one fault
from its own list. They would have read as one flaky suite without the
`nudges` field.

## What was settled the session before, and how

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
- ~~**Ollama stalls, 371s–2217s against a ~62s median.**~~ — **solved
  2026-08-17, and it was never Ollama.** The host sleeps mid-run. A 725s run
  accounted for 180.0s of model and 0.05s of tools; `pmset -g log` puts the
  machine asleep for 548s inside that window (`Idle Sleep` 184s, then
  `Maintenance Sleep` 364s) against 545s unaccounted. `pmset -g custom` on
  battery: `sleep 1`. macOS idle sleep counts HID input, not CPU load, so an
  unattended benchmark suspends *because* nobody is typing — which is why the
  stalls preferred idle machines, arrived in adjacent runs (one nap spans
  several), and left the model resident throughout. Every loop timer was
  monotonic, and a monotonic clock does not advance through a suspend, so the
  nap could only ever appear as a hole. `timing` now carries `wall_ms`,
  `unaccounted_ms` and `slept_ms`; `run_eval` prints `[host asleep Ns]`. Run
  evals under `caffeinate -is`. **A stall with `slept_ms` near zero would be a
  new animal and is worth reporting.**
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
- ~~**`crashloop_root_cause` stops before `get_pod_logs`.**~~ — **fixed
  2026-08-17.** The model was not confused about where the cause lives: it
  read `describe_pod`, saw `exit_code: 1`, and ended with "Next Step: check
  the container logs (`get_pod_logs`)" — naming the tool holding
  `FATAL: could not connect to db:5432` rather than calling it. The loop now
  sends a run back once when its answer names a registered tool the run never
  called, with that fact and no hint about where to look. n=10 before: 8/10
  (17/23 pooled with the earlier sets). n=10 after: 9/10, `get_pod_logs`
  called 10/10. The count is on the answer event as `nudges`, so a run that
  got there alone can be told from one that was sent back.
- ~~**Wrong-workload substitution.**~~ — **not an agent defect; the grader
  was scoring a true aside as a wrong answer. Closed 2026-08-17 evening**,
  10/10 on the current baseline and 50/50 across two probes. The original
  entry is kept below because its reasoning was sound and its conclusion was
  wrong, which is the part worth remembering: every clue pointed at the
  projection, and the projection was innocent.

  One instance remains genuinely unexplained: an earlier session saw a run
  asked about `crasher` answer about `log-shipper`, on `crashloop_root_cause`
  rather than this case. That case is 10/10 in both baselines and no such run
  has been recorded since answer text was kept, so there is nothing to read.
  Do not treat it as closed; treat it as unobserved.

- **The original entry, kept for its reasoning:** `healthy_workload_not_substituted` scored **8/10** in the
  2026-08-17 baseline: asked what is wrong with the healthy `healthy-web`
  deployment, two runs reported `memory-hog` and `bad-image` instead. A third
  instance turned up on `crashloop_root_cause`, where a run asked about
  `crasher` diagnosed `log-shipper`. **Both baseline failures had
  `nudges: 0`**, so this is not the loop guard, and the prompt already forbids
  it in as many words ("Answering about a different workload is worse than
  saying nothing") — wording is not the lever. Both failures called only
  `list_deployments`, which returns every deployment in the namespace, so the
  wrong workload is right there in the tool output next to the right one.
  Worth measuring first: whether the substitution survives a projection that
  answers about the named workload alone.
- **`cluster_wide_scan` drops workloads from its own summary — now the
  highest-value open defect, and the mechanism is measured.** 9/10 in both
  the morning and evening baselines. The evening failure is on the record in
  full: the model called `scan_cluster(only_unhealthy=True)`, and its answer
  was a numbered list of six workloads. `scan_cluster` returned **eight** at
  that moment — verified by calling it directly against the same cluster
  minutes later — so `crasher` and `log-shipper` were dropped from a complete
  list. Nothing was missed by the tools and nothing was invented.

  What is not yet known is *why* two, and why those two: they were adjacent
  in the tool's output, which is suggestive at n=1 and nothing more. Worth
  measuring before designing anything: run that case alone at n=20 with the
  tool result kept alongside the answer, and see whether the drops cluster by
  position, by entry count, or by fault type. The morning failure dropped
  `crasher` too, which is the one thing the two have in common.

  Do not reach for the prompt first. The last defect on this list that looked
  like model behaviour was a grader, and the one before that was the laptop.
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

**2. ~~Get a real baseline.~~** **Taken 2026-08-17: `results/baseline-n10.json`,
100 runs, all ten cases at n=10, under `caffeinate` at bf1f571.**

95% [89-98] 95% CI, median 59.9s, p95 138.6s, grounded 89/100, and
`slept_ms` zero on every run -- so the latency is the agent's rather than
partly the laptop's, and the 176.2s maximum is real rather than a nap.

Per case: `oomkill_root_cause`, `crashloop_root_cause`, `image_pull_failure`,
`service_unreachable_chain`, `service_selector_typo`,
`healthy_not_reported_broken`, `inference_is_marked` and `host_not_cluster`
all 10/10. `cluster_wide_scan` 7/10 and `healthy_workload_not_substituted`
8/10.

Re-measured after the correction, n=10 each: `cluster_wide_scan` 9/10 (from
7/10) and `crashloop_root_cause` 10/10 (held).

**Superseded the same evening by `results/baseline-n10-2.json`**, 100 runs on
current `main` at 1186c1f, and the README table is published from that set.
Both files are kept: this one is the only measurement of the nudge as it was
before 5678f11, and deleting it would leave the improvement unattributable.

**3. ~~Settle the stalls.~~** **Answered 2026-08-17 — and the answer was the
third of the three outcomes that probe was built to distinguish: the delay
was outside both the model and the tools.**

A 725s run against a 62s median reported `model_ms` 180.0s and `tool_ms`
0.05s. `pmset -g log` over the same window:

| event | at | asleep |
| --- | --- | --- |
| run starts | 10:43:57 | |
| `Idle Sleep` -> `DarkWake` | 10:44:34 -> 10:47:38 | 184s |
| `Maintenance Sleep` -> `Wake` | 10:47:48 -> 10:53:52 | 364s |
| run ends | 10:56:02 | **548s** |

548s asleep, 545s unaccounted. The host naps mid-run; every timer in the loop
was monotonic, and monotonic clocks do not advance through a suspend, so the
nap could only ever show up as missing time. `pmset -g custom` on battery says
`sleep 1`.

It explains every earlier observation, including the ones that killed the
other two hypotheses. Idle machines because macOS idle sleep counts HID input
and not CPU load, so an unattended run sleeps *because* nobody is typing.
Adjacent runs because one nap spans several. Model resident throughout because
nothing unloaded it. The cheapest case because a nap lands where it lands.

`timing` now carries `wall_ms`, `unaccounted_ms` and `slept_ms`; `run_eval`
prints `[host asleep Ns]` and keeps `slept_ms` per run. Run under
`caffeinate -is`. Numbers published before 2026-08-17 contain naps that cannot
be separated out after the fact.

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
