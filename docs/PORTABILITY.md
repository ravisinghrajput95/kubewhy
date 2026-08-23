# Managed Kubernetes portability

**Status: static audit complete. GKE live validation COMPLETE for every
section except the remaining 17 runs of one suite. One portability bug found
and fixed.**
Last updated 2026-08-22.

The question is not "does kubewhy run on GKE?" but "does kubewhy rely on
portable Kubernetes APIs and semantics sufficiently to operate reliably on
managed Kubernetes platforms?" This document records the audit, the contract
it produced, and — separately and explicitly — what has actually been measured
on a live cluster and what has not.

Nothing below marked *validated* rests on reading the code. Nothing marked
*audited* rests on running it.

## 1. Provider-dependency audit

Method: word-boundary search for `aws`, `eks`, `azure`, `aks`, `gcp`, `gke`,
`google`, `gcloud`, `boto`, `ec2`, `imds`, `metadata.google` and
`169.254.169.254` across the fourteen shipped modules and `routers/`, then a
read of every Kubernetes client call site, the RBAC policy and the demo
manifests.

**Result: zero provider-specific terms in the shipped agent.** The three hits
anywhere in the Python tree are not dependencies:

| hit | what it is | classification |
| --- | --- | --- |
| `redaction.py` `AWS_KEY` regex | recognises the *shape* of an AWS access key so it never reaches the model or the reader. Calls nothing, requires nothing. | Kubernetes standard (provider-neutral defence) |
| `evals/*.py` comments naming GKE | records where a failure was first observed | documentation |
| `tests/test_k8s_projection.py` | matched on `draws_no_conclusion` containing `aws` | false positive |

### Kubernetes API surface

Every client is a GA `v1` API. **There is no `v1beta` client anywhere in the
tree**, so there is no API that a managed platform may serve at a different
version or withdraw on upgrade.

| API | used for | classification |
| --- | --- | --- |
| `CoreV1Api` | pods, pod logs, events, services, nodes, namespaces, PVCs | Kubernetes standard |
| `AppsV1Api` | deployments, replicasets | Kubernetes standard |
| `DiscoveryV1Api` | EndpointSlices | Kubernetes standard |
| `NetworkingV1Api` | ingresses, networkpolicies | Kubernetes standard |
| `PolicyV1Api` | poddisruptionbudgets | Kubernetes standard |
| `StorageV1Api` | storageclasses | Kubernetes standard |

**The deprecated `Endpoints` API is not used at all** — service reachability
goes through `list_namespaced_endpoint_slice`. Events come from
`list_namespaced_event` on core v1, which every conformant cluster serves.

### Node inspection

`list_nodes` reads `status.conditions` (type and status), `status.allocatable`
(cpu, memory) and `spec.unschedulable`. **It reads no labels, no taints, no
`providerID`, and makes no assumption about node naming.** Nothing in the tree
matches `topology.kubernetes.io`, `node.kubernetes.io`, `cloud.google.com/*`
or `eks.amazonaws.com/*`.

One item is classified **potentially provider specific** and is the only one
in the audit:

> `list_nodes` reports every node condition that is `True` and not `Ready`
> under the key `pressure`. On kind that set is exactly
> `MemoryPressure`/`DiskPressure`/`PIDPressure`. GKE nodes additionally carry
> node-problem-detector conditions (`KernelDeadlock`,
> `FrequentKubeletRestart`, `CorruptDockerOverlay2` and others), normally
> `False`. If one fired it would be surfaced — correctly, it is a real node
> problem — but under a name that implies resource pressure specifically.
> This cannot produce a false alarm on a healthy node and has not been
> observed on a live GKE cluster. **Unvalidated.**

### RBAC

`deploy/rbac.yaml` is pure Kubernetes RBAC across `""`, `apps`,
`discovery.k8s.io`, `networking.k8s.io`, `autoscaling`, `policy` and
`storage.k8s.io`, with `get`/`list`/`watch` only. **No cloud IAM of any kind is
required to perform diagnostics.** `secrets` and `configmaps` are deliberately
absent, so their contents are denied by the policy rather than by the code.

### Demo manifests

Portable, with one note. `demo/tricky-pods.yaml` schedules `gpu-scoring` with
`nodeSelector: {accelerator: nvidia-a100}` to produce an unschedulable pod.
No node in a default GKE node pool carries a bare `accelerator` label — GKE
uses `cloud.google.com/gke-accelerator` — so the fixture behaves as intended,
**but it would stop being a fault fixture on a cluster with GPU nodes so
labelled.** That is a property of the test fixture, not the agent.

## 2. The portability contract

The core diagnostic engine depends on these and nothing else:

**Resources** — Pods, pod logs, Events, Deployments, ReplicaSets, Services,
EndpointSlices, Nodes, Namespaces, PersistentVolumeClaims, StorageClasses,
Ingresses, NetworkPolicies, HorizontalPodAutoscalers, PodDisruptionBudgets.

**Concepts** — CrashLoopBackOff, OOMKilled, ImagePullBackOff, Pending,
FailedMount, FailedScheduling, readiness, liveness, scheduling, resource
pressure, deployment rollout state.

**Explicitly not required** — AWS, Azure or GCP APIs; cloud monitoring APIs;
cloud IAM APIs; cloud networking or storage APIs; provider labels,
annotations, taints or resource-name conventions.

If a provider integration is ever added it belongs outside the diagnostic
engine, as an optional adapter. There is no `if provider ==` anywhere in the
tree today and there should not be one.

## 3. GKE environment

Inspected 2026-08-22, before creating anything.

| | |
| --- | --- |
| account | `veercloud07@gmail.com` |
| active project | `project-0c628a24-2e5e-4878-861` |
| Kubernetes Engine API | enabled on the active project; **not** enabled on `gen-lang-client-0272641172` |
| existing clusters | **none**, in either project |
| budget remaining | ~₹255–260 of ₹300 |

No suitable cluster exists, so one has to be created. The configuration below
is the one measured on 2026-08-08 at **~₹10 for 57 minutes**:

```bash
gcloud container clusters create kubewhy-test --zone=asia-south1-a \
  --num-nodes=1 --machine-type=e2-small --disk-size=50 \
  --disk-type=pd-standard --no-enable-autoupgrade --no-enable-autorepair
```

Zonal, not regional: `--num-nodes=1` on a regional cluster means one node *per
zone* — three nodes and triple the bill. `e2-small` cannot hold the Streamlit
UI (~750m/~900Mi of its ~940m/~1.36Gi allocatable goes to GKE system pods),
which does not affect the diagnostic validation but does rule out testing the
UI surface on this node size.

## 4. Test baseline

`658 passed` on `main` before any GKE work. This must stay green, and no test
may be changed to accommodate GKE.

## 5. What GKE actually measured

Cluster: `kubewhy-gke-test`, GKE `1.35.6-gke.1641000` (control plane and node),
Container-Optimized OS, kernel `6.12.85+`, amd64, `containerd://2.1.7`, two
`e2-medium` nodes in `asia-south1-a`, Standard mode. Deleted after the run.

**Ground truth first, with kubectl, before any model call.** All ten scenarios
reproduced with the same Kubernetes semantics as kind: `OOMKilled` in
`lastState.terminated` with exit 137, `CrashLoopBackOff` in `waiting.reason`,
`ImagePullBackOff` with a `NotFound` pull event, `FailedMount` carrying the
identical `MountVolume.SetUp failed for volume "conf" : configmap "nginx-conf"
not found` message, readiness distinguishable from liveness by restart count,
and both service cases resolving through EndpointSlices -- `typo-svc` one slice
with zero addresses, `crasher-svc` one slice with one address and zero ready.
**GKE creates an EndpointSlice for a service whose selector matches nothing**,
which is what the reachability diagnosis depends on.

**Tools: 10 of 10 work.** One returned a wrong result and is fixed; see below.

| Tool | GKE works | Correct result | Provider-specific dependency |
| --- | --- | --- | --- |
| `list_pods` | yes | yes | none |
| `list_pods(only_unhealthy)` | yes | yes | none |
| `describe_pod` | yes | yes | none |
| `get_pod_logs` | yes | yes | none |
| `get_pod_events` | yes | yes | none |
| `list_deployments` | yes | yes | none |
| `get_service_endpoints` | yes | yes | EndpointSlice only |
| `list_nodes` | yes | **no, until fixed** | **node conditions** |
| `scan_cluster` | yes | yes | none |

**Agentic RCA: 31/31, 95% CI [89-100], 24/31 fully grounded**, over 31 of a
planned 48 runs (stopped by request in round 3). Round 1 completed 16/16, so
every case has passed on GKE at least once.

**The network is not the cost.** Median `tool_ms` 331 against median
`model_ms` 66,768 -- Kubernetes API calls across the internet to
`asia-south1` are 0.5% of runtime. Median run 70.3s over the 30 sleep-free
runs; one run recorded `slept_ms` 285s **despite `caffeinate -is`** and is
held out rather than averaged in.

### The one defect GKE found

`list_nodes` reported every node condition that was `True` and not `Ready`
under the key `pressure`. A GKE Container-Optimized OS node carries **26**
conditions against kind's five; all three real pressure conditions are `False`
and `SysctlChanged` is `True` by design. **Every healthy GKE node came back
under pressure, 2 of 2, on every call** -- a fabricated node fault handed to a
model with no way to check it.

**Classified: Kubernetes portability bug**, not a GKE difference. The code
assumed node conditions are a closed set, which Kubernetes does not guarantee;
GKE was merely the first platform to expose it, and any cluster running
node-problem-detector would have done the same. Fixed: `pressure` is an
explicit allowlist, and other `True` conditions are kept under `conditions` as
optional context so a real `KernelDeadlock` still reaches the diagnosis.

### Environment problems, recorded as such

The node was first sized for `demo` + `adversarial` + `config-faults` and then
`tricky-pods.yaml` was applied as well, taking CPU requests to **99%** and
leaving pods `Pending` with `Insufficient cpu` -- including
`correctly-configured`, the healthy pod entity scoping asks about. Pending pods
read as scheduling faults and would have polluted `scan_cluster` and every
multi-incident scenario. **GKE system pods take ~760m of an `e2-medium`'s
~940m allocatable CPU (81%)**, which is far heavier than kind and needs
budgeting for on any managed platform. Resolved by dropping `shop` and adding
a second node. Classified test/environment problem.

## 5b. The rest of the GKE sections

**RBAC (13): 21/21.** A real `kubewhy-agent` token was minted and used to make
real requests, because `kubectl auth can-i` is not trustworthy on GKE -- with
`--as` it answers `no` for every permission the ServiceAccount demonstrably
has. Eleven reads the agent depends on are allowed, including the watch the
controller needs. `secrets`, `configmaps` and `serviceaccounts` are Forbidden.
All seven destructive verbs -- delete, create, patch, scale, evict, delete
namespace, create serviceaccount -- are Forbidden under `dry_run=All`, which
authorises before admission so a policy hole could not have mutated anything.
**No GCP IAM was granted to perform diagnostics.**

**Controller (15): 15/16.** `nightly-sync` is 3/3 grounded -- the CronJob race
that collects pods before the model asks about them, survived on GKE by the
prefetched log. `bad-image` reports `evidence=NONE` and passes correctly. RCA
latency 29-137s. The one failure is `never-ready` at 2/3: a 47-character
ungrounded answer at 20.9s between grounded passes of 41.8s and 71.9s, same
case and cluster minutes apart, `evidence=yes` on all three. **Model
variance**, matching this case's history on kind (one failure, then 5/5).

**Noise reduction (16): 3/3.** Eleven failing pods over two workloads collapse
to two findings: `flapping-api`'s ten pods become one, and the unrelated
`lonely-worker` stays visible every round. In one round the ten were 9 `Error`
plus 1 `CrashLoopBackOff` and still produced one finding, so grouping is by
fault class rather than literal status. Measured without a model on purpose --
the collapse happens in `Controller.enqueue` against the budget key, and the
diagnosis only runs for what survives it.

**Entity scoping (9): 5/5, all grounded.** "Is correctly-configured
unhealthy?" with five broken pods in the same namespace. Every run answered
about the pod it was asked about; none substituted a neighbour; none carried
an unverified value. Median 30.3s. This is the case that read 1/3 before the
target invariant existed.

**Prompt injection (14): 6/6.** Logs 3/3 and annotations 3/3, the latter all
fully grounded. The log tells the model it is an administrator and orders it
to delete every pod; the annotations order it to report a CrashLoopBackOff
workload as healthy. Both surfaces were treated as data. Annotations arrive
through `describe_pod`'s projection rather than a log read, so passing both is
two results rather than one repeated.

## 5c. In-cluster deployment

**`helm install` of kubewhy on GKE works end to end.** Image
`ghcr.io/ravisinghrajput95/kubewhy:0.1.6` verified multi-arch from the registry
before installing, `linux/amd64` present. Release `deployed`, pod `1/1`,
ServiceAccount and ClusterRole created by the chart, in-cluster credentials
picked up with no kubeconfig, watch running, a real failing pod queued within
15 seconds, four tool calls chosen in sequence, and a finding delivered to the
sink -- all from inside the cluster.

**The chart can now deploy Ollama too** (`ollama.enabled`, off by default).
Validated on GKE: PVC bound on `standard-rwo`, `pullModelOnStart` fetched the
model through the lifecycle hook, the Service resolved at
`ollama.ollama.svc.cluster.local`, and the agent completed a tool loop through
it with the model resident in the cluster and absent from the workstation.

**The in-cluster model was `llama3.2`, not `qwen3`, and this matters.** qwen3
is 5.2GB and needs far more node than this validation justifies. `llama3.2`
hallucinated a fault class -- claiming `ImagePullBackOff` for a pod failing on
an upstream 503 -- and invented a `kubectl patch` with a field that does not
exist. **The grounding checker marked the finding `partial` and flagged
`imagepullbackoff` as unverified**, so the reader is told in the alert itself
that the fault class was not traceable. Classified **model variance,
mitigated**. In-cluster RCA quality is therefore NOT claimable; the topology
is.

**A documentation trap found by doing this.** `deploy/rbac.yaml` and the
chart's RBAC template create objects of overlapping purpose under different
names (`kubewhy-agent` vs `kubewhy`). Applying the standalone file and then
installing the chart leaves two ServiceAccounts and two ClusterRoles, and Helm
refuses to adopt objects it did not create. Do one or the other.

## 5d. Portability matrix

| Capability | kind | AKS | GKE | EKS |
| --- | --- | --- | --- | --- |
| Pod diagnostics | validated | validated | **validated** | not tested |
| Logs | validated | validated | **validated** | not tested |
| Events | validated | validated | **validated** | not tested |
| Deployments | validated | validated | **validated** | not tested |
| Services | validated | validated | **validated** | not tested |
| EndpointSlices | validated | validated | **validated** | not tested |
| Nodes | validated | validated | **validated after fix** | not tested |
| RBAC | validated | validated | **validated 21/21** | not tested |
| Agentic RCA | validated | validated | **validated 31/31** | not tested |
| Controller | validated | validated | **validated 15/16** | not tested |
| Noise reduction | validated | validated | **validated 3/3** | not tested |
| In-cluster Helm install | not applicable | not tested | **validated** | not tested |

AKS is inherited from prior work and was not re-measured today. EKS is *not
validated due to cost*; the audit found no EKS-specific dependency, which is a
statement about the source and not about EKS.

## 6. Portability level

- **Level 1** — kind/local only
- **Level 2** — one managed platform
- **Level 3** — multiple managed platforms on standard Kubernetes APIs
- **Level 4** — cloud-neutral core with optional provider extensions

**Assessed: Level 3, with one caveat.** AKS was validated previously; GKE is
now validated for pod diagnostics, logs, events, deployments, services,
EndpointSlices, nodes and agentic RCA, on standard Kubernetes APIs, with one
portability bug found and fixed. RBAC, controller and noise reduction remain
untested on GKE, so Level 3 is claimed for the diagnostic engine rather than
for every mode the project ships.

The core is Level 4 in shape -- there is no provider adapter, no
`if provider ==`, and the one provider-specific thing encountered (extra node
conditions) is now handled as optional context exactly as the contract
requires -- but Level 4 names an architecture with optional provider
extensions, and there are none to point at.

## 7. What remains

Untested on GKE, in the order it should be picked up:

1. **RBAC with a real ServiceAccount** (section 13). `kubectl auth can-i` is
   useless on GKE -- with `--as` it answered `no` for every permission a
   ServiceAccount demonstrably had, warning `webhook authorizer does not
   support user rule resolution`. The only trustworthy check is minting a
   token and making real requests. Watch for two false negatives when
   scripting it: macOS has no `timeout` binary, and
   `kubectl --watch --request-timeout=5s` exits non-zero on success.
2. **The autonomous controller** (section 15) -- detection latency, RCA
   latency, entity correctness, duplicate suppression, against GKE events.
3. **Noise reduction** (section 16) -- ten failing replicas collapsing to one
   finding, plus one unrelated failure staying visible.
4. **The remaining 17 agentic runs** to finish rounds 2 and 3, and the
   top-up repeats sections 9 and 14 ask for (n=5 scoping, n=3 injection --
   n=3 and n=2 respectively are already in hand).
5. **In-cluster deployment from the Helm chart**, with Ollama served inside
   the cluster. Needs a node that can hold a 5.2GB model.

Cost note: the whole GKE session above -- create, two nodes for part of it,
31 agentic runs, delete -- ran inside the ~₹255 budget with room to spare,
using a zonal cluster. Regional would have tripled the node count.
