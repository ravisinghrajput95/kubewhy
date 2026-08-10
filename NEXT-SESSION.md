# Handoff prompt — paste this to start the next session

I'm working on kubewhy at /Users/ravirajput/Projects/AIOps-agent
(github.com/ravisinghrajput95/kubewhy, public, MIT). Air-gapped Kubernetes
root-cause analysis: a local model via Ollama chains read-only tools to explain
*why* a workload is broken. Six surfaces share one tool set — CLI (agent.py,
--scan), REST (app.py), MCP (mcp_server.py), watch controller (controller.py),
Streamlit UI (ui.py), Slack via Socket Mode (slack_socket.py).

**State: main, everything pushed, tree clean, 318 tests pass, tags through
v0.1.6. No clusters running anywhere — local or cloud.**

Read README.md and CONTRIBUTING.md first. Three rules are non-negotiable: tools
stay read-only (no writes to the cluster, ever); tool output stays projected (a
raw pod is ~1,700 tokens); errors come back as {"error": ...} data, never
raised.

## What was settled last session, and how

Two of the six open items closed outright, and two more had their stated
cause overturned by measuring it. That is the pattern worth carrying forward:
every one of these arrived with a plausible story attached, and in three cases
the story was wrong — including one where the planned fix would have weakened
the security posture to detect something that is not a fault.

Read item 1 carefully. Its cause is now known and its symptom is not fixed.

**1. `nightly-sync` got a plan instead of a diagnosis — it was a race, not a
prompt.** The handoff said this was model behaviour needing an A/B via
ab_prompt.py. It is not. The watch reports a pod and the diagnosis runs a
minute or two later; `nightly-sync` runs every minute with
`failedJobsHistoryLimit: 2`, so the pod is collected before the model asks
about it. Every tool call came back `{"error": "kubernetes API error 404: pods
"nightly-sync-29772408-4bn2r" not found"}` — three runs of three — and in one
run a pod died *between* `describe_pod` and `get_pod_logs`. Writing out an
investigation plan is the sane response to having no data; it was useless in
an alert, and no prompt change would have fixed it.

`Controller.still_there()` now re-resolves to a live pod of the same workload
and the same fault before asking, and posts nothing if the workload has gone
quiet. **That did not fix the case, and it is still open — see pending item 1
below.** Measured before and after on the same cluster with `--repeat 3`: 11/16
both times, `nightly-sync` 0/3 both times. The substitution demonstrably fires
(`diagnosing_a_replacement_pod, requested ...z9g9h, using ...fk5fw`) and the
replacement is collected mid-diagnosis as well, because those runs took 126.4s,
89.2s and 110.3s against a pod that lives about two minutes.

What is settled is the *cause*: it is a race, not a prompt, so do not spend an
A/B on the wording. What is not settled is the fix.

That work also found `count_affected` had been silently returning 1 for every
finding the controller eval ever produced — it built a bare
`client.CoreV1Api()`, which only has a kubeconfig when `run()` loaded one, and
swallowed the failure. Both now use `_api()`.

**2. Ollama stalls — the keep-alive mitigation was a no-op, and the unload
hypothesis is contradicted.** `OLLAMA_KEEP_ALIVE` is a server-side setting and
the Ollama Python client never reads the environment, so the command
CONTRIBUTING documented set a variable nothing on the path looked at.
Measured: unload the model, run one chat through the client with it exported,
and the model comes back expiring in **5.0 minutes**, the server default.
agent.py now forwards it; the same measurement reports **1440 minutes**.
*Every latency figure this project published before that was taken under a
five-minute unload window by someone who believed they had disabled
unloading.*

With keep-alive genuinely held, two adjacent runs came in at 113.8s and 106.3s
against a 60.0s median — **and both had `model_resident: True`**. That is the
outcome ollama_state.py names as the one that sends the search elsewhere. Be
careful with this result: those two runs coincided with a full pytest run
started by hand on the same laptop, so contention is the obvious suspect and
that particular pair is a confound rather than a finding. Runs now record
`started_at` and load average either side of the call, so the next set can
settle it. Note also that a five-minute idle timer could never have fired
between back-to-back eval runs anyway.

**3. Secret existence — do not build the PartialObjectMetadata path.** The
premise was wrong on both halves. It is not undetectable: the kubelet cannot
construct the container and puts the name in the pod's own status, so
`describe_pod` returns `CreateContainerConfigError` with `secret "x" not
found`, and the volume form leaves a `FailedMount` event naming it — both with
no Secrets grant of any kind. The only invisible case is `optional: true`,
which is not a fault. And it would not have been safe: content negotiation
happens after authorization, so RBAC cannot tell a metadata request from a
full one, and `list` cannot be narrowed by `resourceNames`. Demonstrated with
one token granted exactly `list secrets` — names with the
PartialObjectMetadataList header, the plaintext password without it.
Written up in SECURITY.md.

**4. Test-suite order dependence — fixed.** `import ui` executes the Streamlit
script in bare mode and leaves `FormData(form_id='ask')` on the process-wide
main `DeltaGenerator`. One attribute, found by diffing singleton state around
the import, so an autouse fixture in tests/test_ui.py clears it. Two tests pin
the mechanism so a Streamlit rename cannot quietly turn the cleanup into dead
code.

Also fixed along the way, none of it planned: all three `evals/ask_ai` scripts
failed from the repo root where their own README says to run them
(`build_investigation.py` opened `findings.yaml`, which does not exist — it is
`example-findings.yaml`); `run_eval.py` wrote its JSON only at the end, so a
stopped run left nothing on disk; and the README had drifted badly — 13 tools
of 14, no mention of `scan_references` or `GET /references`, "five surfaces"
followed by a list of six, a Slack section still saying replies were
impossible when slack_socket.py ships exactly that, and no entry for
`TRIAGE_STATE_DB`.

## Pending work, roughly in priority order

**1. `nightly-sync` — still failing, but now for a known reason.** The cause is
a race and the cheap half of the fix is in; what remains is that a diagnosis
taking 89–126s cannot be performed on a pod that lives ~120s. Re-resolving
once at the start only narrows the window. The options, none of them free:

- **Capture the evidence at enqueue time.** Read logs and termination state
  while the pod is provably alive, and hand that to the diagnosis instead of a
  pod name. Correct, and the largest change: the agent currently fetches its
  own evidence through tools, so this means feeding pre-fetched material into
  the loop.
- **Diagnose Job/CronJob pods from the Job rather than the pod.** The Job
  object outlives its pods and carries the failure count and conditions,
  though not the logs, which is where "FATAL: upstream returned 503" lives.
- **Raise `failedJobsHistoryLimit` in the demo fixture.** Makes the eval pass
  and fixes nothing real — worth naming only so nobody does it by accident.

Note the eval's own timing: `nightly-sync` runs took 126.4s, 89.2s and 110.3s,
well above the ~60s median of other cases, because the model spends calls
re-orienting after a 404. A faster fix makes the race rarer at the same time.

**2. Get a real baseline.** This is now the biggest gap. The 100-run set was
started and stopped at n=19 (results/keepalive-partial-2cases.json, two cases
only) because it was blocking higher-priority work for 80 minutes. It is the
first data taken with keep-alive actually held, and it is not a baseline. Run
`OLLAMA_KEEP_ALIVE=24h python evals/run_eval.py --repeat 10 --json
results/baseline.json` on an otherwise idle machine and *leave the machine
alone* — the load-average fields exist precisely so you can prove you did.

Note run_eval.py iterates case-major, so an interrupted run has full data on
the first cases and none on the rest. Repeat-major would degrade gracefully;
worth changing before the next long run.

**2. Ollama stalls, with the confound removed.** The instrumentation is in
place and the unload story is already contradicted. The next run should show
whether slow runs land on high `load_before`. If they do, the answer is
contention and the defect is about how the benchmark is run rather than about
Ollama. If they do not, that is the interesting outcome and the search
continues — nothing so far explains 1013s.

**4. Ground truth for the new fixtures.** demo/tricky-pods.yaml gained three
objects: `billing-worker` (missing Secret via envFrom), `cert-rotator` (missing
Secret via volume) and `experiment-runner` (absent but `optional: true`, a
control that must never be reported). evals/ask_ai/example-findings.yaml does
**not** cover them. Do not hand-write entries into that file: it records one
real investigation and states that every figure came from a command run during
it. Re-run the investigation against the updated fixture and record what is
actually observed.

**5. The chart never sets `TRIAGE_STATE_DB`.** store.py exists to make the
controller survive a restart, and the Helm chart does not enable it — so an
in-cluster controller still re-announces every failure after a rollout, which
is the exact problem store.py was written to fix. The container runs with a
read-only root filesystem and only an emptyDir for /tmp, and an emptyDir does
not survive rescheduling, so doing this properly means a PVC and a
`persistence.enabled` value. Documented in the README config table for now.

**6. Untested surfaces.** Slack reply path (Socket Mode connects, but replying
needs a real SLACK_BOT_TOKEN). EKS. store.py under more than one replica —
note the controller Deployment hardcodes `replicas: 1` with no value to
override it, so this is theoretical rather than reachable by configuration.

**7. Frozen benchmarks are provisional.** The 30 cases in
evals/ask_ai/tiers.yaml were chosen on judgement. A benchmark must
*discriminate* — show both passes and failures — and that cannot be known
until the suite has run once. `validate.py` now actually runs from the repo
root, which it did not before, so the CI gate is available: 427 prompts, 27
categories, 19 controls, clean.

## Work style

Verify against real systems rather than asserting. State the measurement
method when publishing a number. Report intervals, not point estimates. Commit
each piece once verified and tested; **no Co-Authored-By or any assistant
attribution in commit messages.** Be blunt about what is untested — and when
evidence contradicts your own hypothesis, say so rather than working around
it. Three of last session's items were closed by doing exactly that, and two
of them turned out not to need the work that had been planned for them.

Budget note: ~₹260 of ₹300 GCP credit remains. A 1-node e2-small zonal cluster
is ~₹11/hour; e2-standard-4 plus a LoadBalancer is ~₹20–25/hour. Delete in the
same session, and remember a reserved static IP keeps billing after the cluster
is gone.
