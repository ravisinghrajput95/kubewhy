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
