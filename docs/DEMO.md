# kubewhy — demo environment and walkthrough

Everything here runs against a real cluster. There is **no demo mode and no
canned model output**: the faults are real Kubernetes objects, the evidence is
read live by the same read-only tools the product ships, and the diagnosis is
generated at demo time. A recorded answer replayed as though it were live would
undo the only claim this project makes.

That decision is the reason section 4 of the phase brief ("if appropriate,
provide a deterministic demo mode") is answered with *not appropriate*. The
determinism it asks for comes from the fixtures, which are pinned YAML, rather
than from pre-writing what the model says about them.

## Standing it up

```bash
kind create cluster --name kubewhy-ui
kubectl apply -f demo/broken-pods.yaml      # the core fault set
kubectl apply -f demo/config-faults.yaml    # ConfigMap/Secret faults
kubectl apply -f demo/tricky-pods.yaml      # relational + scheduling faults
kubectl apply -f demo/adversarial.yaml      # injection + same-name fixtures

ollama serve && ollama pull qwen3           # local inference, nothing leaves the host
streamlit run ui.py
```

Roughly two minutes for images to pull and the faults to reach their steady
state. `kubectl wait --for=condition=Ready pod --all -n demo --timeout=150s`
will time out — that is correct, several of these pods are never going to be
ready.

## The fault set

Every workload below is deliberate, and its expected condition is stable: these
are the conditions the evaluation corpus scores against. Ground truth was read
from the cluster's own events on 2026-08-25 rather than from the manifests'
intent.

| workload | namespace | expected condition | expected evidence | expected RCA |
|---|---|---|---|---|
| `memory-hog` | demo | OOMKilled, restarting | `last_termination.reason=OOMKilled`, `limits.memory=64Mi` | exceeded its 64Mi limit and was killed by the kernel |
| `crasher` | demo | CrashLoopBackOff | `exit_code=1`, logs `could not connect to db:5432` | exits because it cannot reach the database |
| `bad-image` | demo | ImagePullBackOff | `waiting_reason`, image `nginx:this-tag-does-not-exist` | the image tag does not exist in the registry |
| `slow-starter` | demo | CrashLoopBackOff | `BackOff` event on container `web` | restarts before it finishes starting |
| `log-shipper` | demo | Error (DaemonSet) | `last_termination.reason=Error` | container exits non-zero |
| `needs-db` | demo | Init:CrashLoopBackOff | init container `wait-for-db` back-off | never starts; its init container fails |
| `never-ready` | demo | Running, never Ready | `Readiness probe failed: … connection refused` on `:8080/healthz` | passes liveness, fails readiness — the container never restarts |
| `nightly-sync` | demo | Error (CronJob) | per-run pod `last_termination` | each scheduled run fails; the runs are one workload, not many |
| `crasher-svc` | demo | 0 ready endpoints | `ready_endpoints=[]`, `not_ready_endpoints=[…]` | a pod matches the selector but never becomes ready |
| `typo-svc` | demo | 0 endpoints | `selector`, empty endpoints | the selector matches nothing |
| `healthy-web` | demo | **healthy control** | `2/2 ready` | nothing is wrong with it |
| `missing-configmap-key` | config-faults | CreateContainerConfigError | event `couldn't find key LOGLEVEL in ConfigMap config-faults/app-settings` | the ConfigMap exists, the key does not |
| `missing-secret-key` | config-faults | CreateContainerConfigError | event `couldn't find key STRIPE_SECRET_KEY in Secret config-faults/api-keys` | the Secret exists, the key does not |
| `missing-configmap-volume` | config-faults | stuck ContainerCreating | mount event | a volume references a ConfigMap that does not exist |
| `missing-volume-key` | config-faults | stuck ContainerCreating | mount event | a volume references a key that does not exist |
| `projected-source-missing` | config-faults | stuck ContainerCreating | mount event | a projected source is absent |
| `frozen-config` | config-faults | **Running and silently wrong** | no abnormal status | subPath mount does not update — a status-only view calls this healthy |
| `correctly-configured` | config-faults | **healthy control** | `1/1 ready` | nothing is wrong with it |
| `gpu-scoring` | shop | Pending, unschedulable | `FailedScheduling: … didn't match Pod's node affinity/selector` | requests a node label no node carries |
| `archive` | shop | Pending, unschedulable | `FailedScheduling: pod has unbound immediate PersistentVolumeClaims` | its PVC never binds |
| `pricing` | shop | CreateContainerConfigError | event `configmap "pricing-config" not found` | references a ConfigMap that does not exist |
| `catalog` | shop | Ready, unreachable | endpoints populated, `target_port` mismatch | the Service's targetPort does not match the container port |
| `basket` | shop | Service blackholes | selector matches nothing | selector typo |
| `payments` | adversarial / adversarial-b | same name, two namespaces | namespace of the described pod | the answer must be about the namespace asked for |
| `quiet-and-fine` | adversarial | **healthy, emits no logs** | pod status | healthy; absence of logs is not a fault |

Nine of these are healthy or must-not-be-reported-broken. That balance is
deliberate: a diagnostic tool that calls everything broken is not diagnosing.

## The 5–10 minute walkthrough

1. **Open the console.** The header names four things at once: the cluster, the
   inference mode, the provider and model, and where evidence goes.
   `EVIDENCE on-network` in local mode; `external` in api mode. That word is the
   difference between a local tool and one that ships pod logs to a third party,
   and it is on screen rather than in a settings page.
2. **The scan.** `scan_cluster(only_unhealthy=True, limit=20)` is printed above
   the table, so the reader knows exactly which call produced it. The table says
   *where*, never *why*.
3. **Pick a workload.** `demo/memory-hog` is the cleanest story; `config-faults/
   missing-configmap-key` is the most surprising, because nothing in
   `kubectl get configmap` looks wrong.
4. **Ask "why is this failing?"** and watch. The tool chain appears as it
   happens — `scan_cluster`, then `describe_pod`, then `get_pod_events` or
   `get_pod_logs` — with elapsed time in the label rather than only in a
   spinner.
5. **Read the panel top down.** Verdict strip, root cause, then *What the
   evidence says* in three columns: Observed (each claim carrying the
   `tool.field` it came from), Inferred, Unknown.
6. **Open the Timeline.** Every call with its actual arguments and scope.
7. **Open Evidence.** The raw tool results the answer was built from.
8. **Then ask about a healthy one** — `demo/healthy-web`. One call, and the
   answer is "it is running normally". A tool that can only find problems cannot
   tell you a thing is fine.

The point the walkthrough should make: kubewhy does not ask an LLM about
Kubernetes. It collects Kubernetes evidence, reasons over that evidence, checks
each claim back against the evidence, and shows its work.

## What to expect on timing

On a laptop with local `qwen3`, an investigation takes **90–110 seconds** and
2–5 model rounds. Against a hosted API it is roughly 4–10 seconds. Both are
measured; see `docs/AI_EVALUATION.md`. If you are demonstrating live, warm the
model first (`ollama run qwen3 ok`) so the first click does not pay for a cold
load.
