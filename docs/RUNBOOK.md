# kubewhy — operating it

What breaks, what it costs, and what to do. Written for the deployment kubewhy
is built for: **one SRE team, one cluster** — see [SECURITY.md](SECURITY.md)
for why that decision shapes everything else.

## One replica, per component, on purpose

kubewhy runs one controller and one console. That is a design position rather
than an unfinished feature, and it is enforced by the chart in both places.

**The controller** holds dedup state: which workload it last reported and when,
and how many findings it has posted this hour. Two controllers watching one
cluster deliver every finding twice — exactly the noise the cooldown and the
hourly ceiling exist to stop, reintroduced by the Deployment doing its ordinary
job. The chart pins `replicas: 1` and sets `strategy: Recreate` so a rollout
does not briefly run two. With `TRIAGE_STATE_DB` set there is also an advisory
lease (`store.claim_lease`) so a second controller sharing the state file backs
off and says so; it is advisory, and it cannot stop a controller that ignores
the answer.

**The console** keeps investigation history in the same store. Two replicas are
two histories, and which one a person sees depends on which pod their websocket
landed on — so a reconnect after a rollout shows them a sidebar that has
forgotten the investigation they were reading. `ui.replicas` above 1 fails the
install with that explanation.

**Sharing one volume between replicas is not the way out.** That is SQLite over
a shared filesystem, which corrupts. `store.py`'s interface is the seam where a
Redis or Postgres implementation would go if this ever needs to be more than
one of each; nothing above it would change.

### What that means for availability

There is no HA story here and none is claimed. A restart is an outage for the
length of a pod restart, and in-flight investigations do not survive it.

The thing that makes this acceptable is what kubewhy is: nothing depends on it
to stay up. It reads and explains; it changes nothing. A controller that is
down for two minutes posts its findings two minutes later. A console that is
down is a page someone reloads. **No workload's health depends on kubewhy
running**, which is the property that makes a single replica a reasonable
trade rather than a risk to manage.

If that stops being true for you — if a diagnosis becoming unavailable is
itself an incident — the honest answer is that kubewhy is not there yet, not
that you should run two replicas.

## What a restart costs

| State | Without `TRIAGE_STATE_DB` | With it |
|---|---|---|
| Controller dedup and hourly count | Lost. The next scan re-announces **every** failure in the cluster. | Survives |
| `/ask/jobs` results | Lost. A job id returns 404 and the caller re-asks. | Survives |
| `/ask/jobs` jobs that were *running* | Lost with everything else | Survive, and are **marked failed at startup** with a reason — see below |
| Console investigation history | Lost. The sidebar comes back empty. | Survives |
| Console session state — selected workload, filters, the answer on screen | Always lost | Always lost |
| Anything a tool read | Never stored anywhere | Never stored anywhere |

`persistence.enabled=true` in the chart provisions the volumes and sets
`TRIAGE_STATE_DB` for the controller and the console. They get **separate**
claims: one file written by two processes is the corruption case above.

### Jobs that were running

An investigation interrupted by a restart is not resumed. The thread is gone,
and re-running someone's question unasked is not a decision this process makes
quietly. At startup the API marks anything left `queued` or `running` as
`failed` with:

> the process restarted while this investigation was running; it was not
> resumed. Ask again.

That is deliberate. Before persistence such a job vanished and the 404 told the
caller to ask again; once the record survives, leaving it `running` would have
callers polling an investigation that can never finish. The count is logged as
`jobs_interrupted_by_restart`.

Verified by killing a running API with SIGKILL mid-investigation and restarting
it against the same state file: the job read `running` before the kill and
`failed` with the message above afterwards, with
`jobs_interrupted_by_restart count: 1` in the startup log.

## Restarting

### The controller

```bash
kubectl rollout restart deployment/<release> -n <namespace>
kubectl rollout status deployment/<release> -n <namespace>
```

`strategy: Recreate` means the old pod stops before the new one starts, so
there is a gap of a few seconds with nothing watching. Events that occur in the
gap are not queued anywhere — the controller watches, it does not replay. A
workload that is still broken when it comes back will be picked up on the next
event or resync; one that broke and recovered inside the gap is missed
entirely, which is usually the outcome you wanted.

**Afterwards, check:**

```bash
kubectl logs deployment/<release> -n <namespace> | grep -E 'controller_already_running|inference_'
```

- `controller_already_running` means another controller holds the lease. With
  `Recreate` this should not happen; if it does, something is running a second
  copy outside the Deployment.
- No inference line at all means the gateway has not resolved. The controller
  aborts on a misconfigured one deliberately, so check the pod is not
  crashlooping before assuming it is idle.

### The console

```bash
kubectl rollout restart deployment/<release>-ui -n <namespace>
```

Anyone with the page open gets a websocket disconnect and Streamlit's
reconnect banner. Their **selection and any answer on screen are gone** — that
lives in session state, which no setting persists. Their investigation history
comes back if `persistence.enabled=true`, and does not otherwise.

A running investigation dies with the pod and produces an audit record with
outcome `abandoned`. Nothing writes a partial answer anywhere.

### The API

Same as the console, minus the browser: in-flight `/ask` requests fail with the
connection, and `/ask/jobs` entries are closed out as described above.

## When the state volume fills or corrupts

The store is small — job records and one row per reported workload — so filling
128Mi means something is wrong rather than that you need a bigger disk. Check
`purge_jobs` is running: expiry is charged to whoever submits a job, so a
deployment that stopped receiving `/ask/jobs` requests also stopped expiring
old ones.

A corrupt SQLite file is almost always two writers. Confirm nothing else mounts
the claim, then:

```bash
kubectl scale deployment/<release> --replicas=0 -n <namespace>
# delete the file inside the volume, or delete the PVC and let the chart
# recreate it -- the PVC carries helm.sh/resource-policy: keep, so
# `helm uninstall` will not remove it for you
kubectl scale deployment/<release> --replicas=1 -n <namespace>
```

Losing the file costs one round of re-announced findings and the job history.
Nothing about the cluster is lost, because kubewhy stores nothing about the
cluster — every diagnosis is read fresh.

## When a diagnosis goes wrong

Four things go wrong in different ways, and only one of them means the answer
is wrong. Telling them apart is most of what this section is for.

**The numbers below come from 2521 recorded runs in `results/`, and they are
not a performance claim.** That corpus is a mixture of experiments — different
models, prompt configurations, some deliberately degraded to measure the
effect. It says what these failure modes look like when they occur, not how
often kubewhy is right. For that, read [AI_EVALUATION.md](AI_EVALUATION.md),
which is careful about what its numbers do and do not support.

| Verdict | Share of 2521 runs | What it means |
|---|---|---|
| `grounded` | 1840 (73.0%) | Every claim traced to a tool result |
| `partial` | 317 (12.6%) | Some claims traced, some not |
| `insufficient_evidence` | 292 (11.6%) | Nothing here could be checked — often the **correct** answer |
| `contradicted` | 39 (1.5%) | The evidence says otherwise |
| `ungrounded` | 9 (0.4%) | Nothing traced |

### The model is unreachable

**Symptoms.** `/ask` returns **503** with `inference unreachable:
ConnectionError`. `/readyz` returns 503 and names the provider it tried. The
controller aborts at startup rather than watching a cluster it cannot diagnose.
The console renders the error rather than an empty page.

503 rather than 500 is deliberate: a 500 reads as a bug and gets retried, a 503
reads as a dependency being down.

**Check, in this order:**

```bash
curl -s localhost:8000/readyz | jq             # which provider, and why not
kubectl logs deploy/<release> | grep inference_configured
```

`/readyz` is three-valued — ready, not ready, unknown — rather than assuming a
reachable endpoint serves the model you asked for. "Ready on the fallback" and
"ready on the primary" are different states of the world and it says which.

**What still works.** Everything that does not need a model: `/scan`, `/pods`,
`/nodes`, the console's cluster browser, every MCP tool. The API deliberately
does **not** refuse to start on a misconfigured gateway, because killing
working functionality to punish a setting nobody is using is worse than logging
it loudly. This is degraded, not down.

### The deadline fired

**Symptom.** The answer carries `termination: deadline_exceeded` and reads as
incomplete, because it is. The run was stopped while collecting evidence.

**Measured, over 2477 recorded runs with a duration:** median **41.4s**, p95
**176.2s**, p99 **272.4s**. The default `TRIAGE_INVESTIGATION_BUDGET` is 600s,
which is roughly 2.2× the p99.

Five runs exceeded 600s of wall clock. All five are in files from unattended
overnight runs, and the cause was **the laptop sleeping**, not the model
hanging — the budget measures elapsed time minus slept time for exactly this
reason. Run unattended evaluations under `caffeinate -is`.

**This is not a wrong answer.** It is the system refusing to keep spending on
one question. The termination reason is returned as data rather than prose, so
a caller can branch on it instead of pattern-matching a sentence.

**What to do:** narrow the question before raising the budget. A scoped
question — one naming a workload — holds the run to that entity:
`targeting.enforce()` rewrites a call that would widen the scope and refuses
one that would move it, so rounds are not spent on neighbours. The console
passes its selection as data for that reason. Raise
`TRIAGE_INVESTIGATION_BUDGET` only if a scoped question is still timing out,
which usually means the model is slow rather than the question is broad.

(How much scoping actually saves is not measured here, and the corpus cannot
settle it — the scoped and open scenarios are different faults, so their tool
counts are not comparable.)

### A contradicted verdict

**The rarest outcome, and the system working.** 28 of 2501 runs. It means a
claim in the answer disagrees with evidence the run itself collected — caught
before it reached you as fact.

**Read the `measured` field first.** It names what the evidence actually said;
trust it over the prose around it. The console renders contradictions ahead of
everything else on the page for that reason — in `contract()`'s output they are
a separate list rather than folded into `unknowns`, because "the tools did not
say" and "the tools said otherwise" are different and only the second means the
answer is wrong.

**One caveat worth knowing before you escalate.** 23 of those 39 — **59%** —
come from a single scenario, `scoping_quiet_workload_beside_loud_one`, which is
a **known open defect**: asked about a quiet workload beside a loud broken one,
the run reads exit code 137 as proof of OOM and dismisses
`last_termination.reason = error`, which says the kill came from elsewhere. If
a contradiction involves a workload with a noisy neighbour, that is the first
thing to suspect and it is a kubewhy bug rather than a cluster one.

The share grew because the checker got better, not because the agent got
worse. `_MEMORY_CAUSE` did not recognise "OOM killer" or "OOM kills" until
2026-08-28, so answers making this exact claim in those words were scored
`grounded`. The contradictions were always there; 11 of them were invisible.

**When to escalate:** contradictions clustering on one *shape* of workload
suggest a projection gap — a field a diagnosis depends on that the tools do not
return. That is a code change, not a configuration one. See ARCHITECTURE.md on
why projection is the load-bearing decision and what it costs.

### An answer full of `[unverified: ...]`

**Not a failure mode — a disclosure.** Unsupported figures are rewritten **in
place**, so a reader skimming the prose for a number finds the measured one or
an explicit marker, never a fabricated value. The alternative — printing a
correction underneath and leaving the invented figure standing — was tried and
is worse, because people skim.

`insufficient_evidence` deserves the same reading. Asked why a workload that
does not exist is failing, "the evidence is not here" **is** the right answer,
and a confident root cause would be the failure. Two scenarios in the corpus
exist to score exactly that.

If a run you expected to be grounded is not, check what it actually called: the
audit record's `tools` field lists every call and its arguments, and a run that
never called `get_pod_events` never saw the cause of a container that never
started.

## When a caller is refused with 429

Two ceilings sit in front of the model-driving endpoints. Neither applies to
`/scan`, `/pods` or the console's cluster browser, which cost no model time.

| Setting | Default | Bounds |
|---|---|---|
| `TRIAGE_MAX_INVESTIGATIONS_PER_HOUR` | 60 | Investigations per authenticated caller |
| `TRIAGE_MAX_EXTERNAL_TOKENS_PER_HOUR` | off | Tokens that actually left your network |

`GET /inference` reports both, and how much of the token budget is spent.

**The 429 carries `Retry-After`**, and it is the seconds until that caller's
window has room — not the window length. A refused request does not spend
another allowance, so a client in a retry loop does not push its own window out
forever. (When the allowance was spent in a burst, that number *is* close to
the window length, because the oldest event really is that recent.)

**An attempt counts even when it fails.** The ceiling is checked before the
handler, so a request that then 503s because the model is unreachable has still
spent an allowance. That is deliberate: recording only successes would leave a
loop against a down model unbounded, which is precisely when a client is most
likely to be looping. Verified against a running API with no model behind it —
one 503, then 429s.

**If a person hits the investigation ceiling**, either something is looping on
their credential or the ceiling is too low for how they work. Check the audit
trail before raising it — the records name the principal and the question, so
"is this a human or a script" is a query rather than a guess:

```bash
jq -r 'select(.principal == "sre@example.com") | [.at, .question] | @tsv' \
  "$TRIAGE_AUDIT_LOG" | tail -20
```

**If the token budget is spent**, every caller is refused, because the budget
is on total egress rather than per caller — a per-caller token ceiling would
let N callers each spend the maximum, which is not a budget. It refills as the
window slides.

### What these ceilings are not

**They are not billing controls.** This process can decline to start work; it
cannot recall a request in flight. Set a spend cap with your provider — that is
the control that actually bounds an invoice.

**They are per process.** The windows are in memory, so a restart clears them
(bounded by the window length), and a deployment running the API, the
controller and the console as separate pods has three independent windows
rather than one shared budget.

**The console does not enforce them.** A person clicking Diagnose is not the
runaway loop these exist to stop, and refusing an SRE mid-incident would cost
more than it saved. Only the API refuses. If you need the console bounded too,
the honest answer today is the provider's spend cap.

## The audit trail

One record per investigation on the `triage.audit` logger, `msg: investigation`,
and optionally appended to `TRIAGE_AUDIT_LOG`. It names when (`at`, UTC), which
cluster, who asked and how they authenticated, through which surface, the
question they typed, every tool called with its arguments, which pods' **logs**
were read, the verdict, and whether evidence could have left the network. It
deliberately carries none of the evidence — see [SECURITY.md](SECURITY.md).

Records are emitted for runs that failed and for runs whose caller walked away,
so a gap in the trail means the process was not running, not that nobody asked.

Use `at` rather than the log framework's `ts`: only `at` is on the copy written
to `TRIAGE_AUDIT_LOG`, which is the copy that gets shipped.

```bash
kubectl logs deployment/<release> -n <namespace> \
  | jq 'select(.logger == "triage.audit")'

# who read logs, where, and when — the query this exists to answer
jq -r 'select(.sensitive_reads | length > 0)
       | [.at, .principal, .cluster,
          (.sensitive_reads[] | .namespace + "/" + .pod)]
       | @tsv' "$TRIAGE_AUDIT_LOG"
```

Not every investigation reads logs, and the query returning nothing for one is
the correct answer rather than a broken filter — a workload OOMKilled is
diagnosable from `describe_pod` alone, and a run that never called
`get_pod_logs` has no sensitive read to report.

Audit records go to the same stream as everything else this process logs, so
they are as tamper-evident as your log pipeline and no more. `TRIAGE_AUDIT=0`
turns them off, which is a decision someone has to make rather than a default.
