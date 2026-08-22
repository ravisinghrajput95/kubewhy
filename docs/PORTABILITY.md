# Managed Kubernetes portability

**Status: static audit complete, GKE live validation NOT started.**
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

## 5. Portability matrix

**Every GKE cell is unvalidated.** The audit says the code should work; that
is a different claim from evidence that it does, and this table records only
the second kind.

| Capability | kind | AKS | GKE | EKS |
| --- | --- | --- | --- | --- |
| Pod diagnostics | validated | validated | **not yet tested** | not tested |
| Logs | validated | validated | **not yet tested** | not tested |
| Events | validated | validated | **not yet tested** | not tested |
| Deployments | validated | validated | **not yet tested** | not tested |
| Services | validated | validated | **not yet tested** | not tested |
| EndpointSlices | validated | validated | **not yet tested** | not tested |
| Nodes | validated | validated | **not yet tested** | not tested |
| RBAC | validated | validated | **not yet tested** | not tested |
| Agentic RCA | validated | validated | **not yet tested** | not tested |
| Controller | validated | validated | **not yet tested** | not tested |
| Noise reduction | validated | validated | **not yet tested** | not tested |

EKS is *not validated due to cost* and no EKS cluster should be created to
fill the table. The audit found no EKS-specific dependency, which is a
statement about the source and not about EKS.

## 6. Portability level

- **Level 1** — kind/local only
- **Level 2** — one managed platform
- **Level 3** — multiple managed platforms on standard Kubernetes APIs
- **Level 4** — cloud-neutral core with optional provider extensions

**Assessed today: Level 2, with the code audited as Level 3/4-shaped.**

AKS is validated; GKE is not yet. The audit found a cloud-neutral core with no
provider extensions at all, which is the *shape* of Level 4, but a level is a
claim about evidence and the evidence for a second managed platform does not
exist yet. It becomes Level 3 when the GKE sections below are measured.

## 7. What remains, and why it has not been done

The live validation — scenarios, tool compatibility, the agentic loop, entity
scoping, events, EndpointSlices, RBAC with a real ServiceAccount, prompt
injection, the controller and noise reduction — needs a GKE cluster **and**
several hours of local Ollama inference, since the model runs on the
workstation and only the cluster is remote.

It has not been started because the workstation was at 15% battery on battery
power when the audit finished. Beginning it there would risk a billable
cluster outliving the machine driving it.
