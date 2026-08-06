# local-triage-agent

[![tests](https://github.com/ravisinghrajput95/local-triage-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/ravisinghrajput95/local-triage-agent/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%20--%203.13-blue.svg)](requirements.txt)
[![MCP](https://img.shields.io/badge/MCP-server-8A2BE2.svg)](#use-it-from-claude-cursor-or-any-mcp-client)

**Air-gapped Kubernetes triage.** Ask why a pod is broken and get the root
cause — read from real logs, events and resource limits by a model running on
your own hardware. No cloud API, no API key, no cluster data leaving your
network.

![demo](docs/demo.gif)

It works down the chain like a person would. Given only *"crasher-svc is
unreachable"*, it finds the service has no ready endpoints, identifies which
pod backs it, reads why that pod is restarting, and pulls the application
error out of the dead container's logs:

```
$ python agent.py "The crasher-svc service in demo is unreachable. Why?"
  -> get_service_endpoints({'name': 'crasher-svc', 'namespace': 'demo'})
  -> list_pods({'namespace': 'demo', 'only_unhealthy': True})
  -> describe_pod({'name': 'crasher-5964d99948-28p5k', ...})
  -> get_pod_logs({'name': 'crasher-5964d99948-28p5k', 'tail': 100, ...})

crasher-svc is unreachable because its pod is crashing with exit code 1,
repeatedly restarting due to a database connection failure. The logs show it
cannot connect to db:5432, which is refused.

[grounded] every figure traced to a tool result
```

## Or don't ask at all

Asking requires you to already know something is wrong, be at a terminal, and
know what to ask — and anyone who satisfies all three is faster typing
`kubectl describe`. So the controller inverts it: it watches the cluster,
notices a pod going unhealthy, diagnoses it unprompted, and delivers the root
cause somewhere people already look.

```bash
helm install triage deploy/chart --set sink.type=slack \
  --set sink.slack.existingSecret=slack-webhook
```

```
:boom: payments-api is unhealthy in prod — 3 pods affected
The pod was terminated for exceeding its 64Mi memory limit (exit 137). The
stress the container is under needs roughly 250Mi. Raise the limit or cap the
workload.
OOMKilled · grounded
```

**Inference latency stops mattering here.** A diagnosis lands about a minute
after the pod broke, and nobody was waiting on it — which removes the one
thing that makes the interactive mode hard to justify against `kubectl`.

The hard part is not the watching, it is not becoming noise. Three mechanisms:
findings are grouped by owning **workload** rather than pod, so ten crashing
replicas produce one message; each workload gets a **cooldown** (30 min by
default); and there is a **global hourly ceiling**, because during a node
failure dozens of pods break at once and none of those messages help.

Findings are also deduped by *fault*, not status — a bad image reports
`ErrImagePull` then `ImagePullBackOff`, and an OOM-killed pod restarts into
`CrashLoopBackOff`. Both were producing duplicate messages until a live run
caught it.

Three properties it is built around:

- **Read-only.** No tool scales, restarts or deletes anything, and
  [`deploy/rbac.yaml`](deploy/rbac.yaml) enforces that at the API server
  rather than trusting the code.
- **Shows its working.** Every answer returns the tool calls behind it.
- **Checks itself.** Figures and status names are verified against tool
  output; unsupported claims are flagged rather than hidden.

### Compared to [k8sgpt](https://github.com/k8sgpt-ai/k8sgpt)

k8sgpt is more mature, scans cluster-wide, and you should probably use it.
This is different in three ways that matter if they matter to you:

| | local-triage-agent | k8sgpt |
| --- | --- | --- |
| Inference | Always local | Cloud by default |
| Method | Chains tools to a root cause | Analyzers + one LLM pass |
| Output | Reports confidence, flags unverified claims | Prose |
| Coverage | One namespace at a time | Cluster-wide |
| Maturity | Early | Production, large community |

If you need cluster-wide scanning and integrations, use k8sgpt. If you cannot
send cluster state to a third party, or you want to see and verify the
reasoning, this exists for that.

## Quick start

```bash
pip install -r requirements.txt
ollama pull qwen3                    # ~10GB, needs ~12GB RAM free
python agent.py "is anything broken in the default namespace?"
```

Tool calls print to stderr as they happen; pipe stderr to `/dev/null` for the
answer alone. Requires Python 3.11+, [Ollama](https://ollama.com) running, and
a model whose `ollama show` capabilities include `tools`.

<details>
<summary><b>Want a broken cluster to try it on?</b></summary>

`demo/` deploys one workload per common failure mode — `CrashLoopBackOff`,
`OOMKilled`, `ImagePullBackOff` — two broken services (one selector matching
nothing, one whose pods never become ready), plus healthy deployments and
services as controls, so the agent has to tell broken from working.

```bash
kind create cluster --name triage-demo
kubectl apply -f demo/broken-pods.yaml
kubectl get pods -n demo             # wait for failure states

python agent.py "What is broken in the demo namespace and why?"
kind delete cluster --name triage-demo
```
</details>

## How it works

Ollama cannot reach your cluster. What it supports is *tool calling*: the
model is given tool schemas and replies asking for one; `agent.py` runs the
matching Python function, feeds the result back, and repeats until the model
has enough to answer.

Each collector is a plain function returning a JSON-able dict, so the same
function serves as a REST handler, an Ollama tool and an MCP tool with no
adapter between them. Docstrings become the tool descriptions the model reads.

**[→ Architecture, sequence diagrams and trust boundaries](docs/architecture.md)**

### Tool output is projected, deliberately

The model has a fixed context budget, and everything follows from that:

| Payload | Tokens |
| --- | --- |
| Raw `list_namespaced_pod`, 5 pods | ~7,560 |
| `list_pods(only_unhealthy=True)` | ~91 |

At ~1,500 tokens per raw pod, a 50-pod namespace would exceed qwen3's entire
40k window in one call. Every tool returns only the fields a diagnosis needs,
and a test asserts a token ceiling so it cannot silently regress. The cost is
real: projections discard information, so a fault hinging on a dropped field
is invisible.

## Use it from Claude, Cursor, or any MCP client

The tools are also an MCP server, so a client that brings its own model can
diagnose your cluster without this project's loop involved at all.

```bash
python mcp_server.py            # stdio
python mcp_server.py --http     # streamable HTTP on :8765
```

```json
{
  "mcpServers": {
    "local-triage": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/mcp_server.py"]
    }
  }
}
```

All 12 tools are exposed with schemas derived from their signatures. The
read-only guarantee and log redaction apply identically here.

## Deploying the controller

```bash
helm install triage oci://ghcr.io/ravisinghrajput95/charts/local-triage-agent \
  --namespace triage --create-namespace
kubectl logs -n triage -l app.kubernetes.io/instance=triage -f
```

The image and chart are published to GHCR on every `v*` tag, multi-arch
(`amd64`/`arm64`). To install from a checkout instead, use `deploy/chart`.

Defaults to `stdout`, so you can read what it would have said before pointing
it at a channel. The chart creates the read-only ServiceAccount and
ClusterRole, runs non-root with a read-only root filesystem, and pins one
replica — two controllers would diagnose and post everything twice, since the
dedup state is in-process.

| Value | Default | Purpose |
| --- | --- | --- |
| `sink.type` | `stdout` | `stdout` or `slack` |
| `sink.slack.existingSecret` | — | Secret holding the webhook. Preferred over `webhookUrl`, which lands in your values file and release history. |
| `model.ollamaHost` | in-cluster svc | The controller runs no model; point this at an Ollama it can reach |
| `watch.namespaces` | all | Narrow this on a large cluster |
| `watch.cooldownSeconds` | `1800` | Silence per workload after a finding |
| `watch.maxPerHour` | `12` | Global ceiling across all workloads |
| `watch.skipExisting` | `true` | Don't diagnose everything already broken at startup |
| `rbac.allowPodLogs` | `true` | Set false to diagnose without reading logs |

Run it locally against your current kubecontext with `python controller.py`.

## Managed clusters (EKS, GKE, AKS)

All three authenticate with **exec credential plugins**, which the Kubernetes
Python client supports and refreshes automatically — an expired token is
re-fetched when the next request is built. Long-lived watches reconnect every
300s, comfortably inside EKS's 15-minute token lifetime, so the controller
survives token rotation.

The plugin binary has to be on `PATH`, and authorization is a separate step
from RBAC on every provider. This trips people up: applying the ClusterRole is
not enough if your cloud identity was never mapped to a Kubernetes subject.

| | Plugin needed | Authorization step people forget |
| --- | --- | --- |
| **EKS** | `aws` CLI | Map the IAM principal via EKS access entries or the `aws-auth` ConfigMap |
| **GKE** | `gke-gcloud-auth-plugin` | Bootstrap yourself as admin before you can create ClusterRoles: `kubectl create clusterrolebinding me --clusterrole=cluster-admin --user=$(gcloud config get-value account)` |
| **AKS** | `kubelogin` (AAD clusters) | Bind the AAD group or object ID, not the username |

Check what you are pointed at before running anything:

```bash
kubectl config current-context
kubectl auth can-i --list --as=system:serviceaccount:triage:triage-agent
```

### Running against a remote cluster from your laptop

Works today, with three things to set:

- **`watch.namespaces`** — the default watches everything, and on a production
  cluster every pod event in the cluster is streamed to your machine.
- **`K8S_TIMEOUT`** — 15s is generous on localhost and tight over a VPN.
- **`rbac.allowPodLogs=false`** if production logs must not reach your
  terminal. Redaction is pattern matching, not a guarantee.

Private control planes (EKS/GKE private endpoints) need VPN, a bastion or a
peered network. No configuration here substitutes for connectivity.

### Running the controller inside a managed cluster

The controller runs no model — it needs an Ollama it can reach, which in a
managed cluster means a **GPU node pool**. That cost is the main barrier to
this mode, and it is worth being clear-eyed about:

| Shape | Trade-off |
| --- | --- |
| Controller + Ollama both in-cluster, GPU node pool | Correct, and costs real money |
| Controller on your laptop watching the remote cluster | Free, works today, stops when you close the lid |
| Controller in-cluster, Ollama over VPN | Cluster data leaves the cluster, which defeats the point |

**Not yet tested against a real EKS, GKE or AKS cluster** — the auth path was
verified by reading the client, and everything else was exercised against
`kind`. Treat this section as informed expectation, not a support matrix.

## Security

**Read the [security policy](SECURITY.md) before pointing this at anything you
care about.** The short version:

**Use the supplied RBAC, not your admin kubeconfig.**

```bash
kubectl apply -f deploy/rbac.yaml
kubectl create token triage-agent -n triage --duration=8h
```

Verified against a live cluster:

```
get pods            -> yes      delete pods        -> no
get pods/log        -> yes      create pods        -> no
list endpointslices -> yes      patch deployments  -> no
list deployments    -> yes      get secrets        -> no
```

**Authenticate the API.** Set `TRIAGE_API_TOKEN` and every endpoint requires
`Authorization: Bearer <token>`. Unset, the API is open — acceptable only on
loopback, which is why compose publishes to `127.0.0.1`.

**Logs are redacted, imperfectly.** `redaction.py` strips AWS keys, GitHub and
Slack tokens, JWTs, private keys, URL passwords, `KEY=value` secrets and bearer
headers from pod logs before they reach the model or your terminal. It is
pattern matching and it will miss novel formats. If your logs must never be
read by a model, drop the `pods/log` rule from the ClusterRole.

## Does it actually work?

Unit tests prove the code is right; they say nothing about whether the agent
reaches the right conclusion. `evals/` asks a real model real questions against
the demo cluster, where every fault is known in advance.

```bash
python evals/run_eval.py --repeat 10 --json results/qwen3.json
python evals/summarise.py results/*.json
```

Measured on the demo cluster, 7 cases, repeated:

| Model | Pass rate (95% CI) | n | Median | p95 | Fully grounded |
| --- | --- | --- | --- | --- | --- |
| `qwen3:8b` | **100%** — CI [85–100] | 21 | 46s | 76s | 10/21 |
| `llama3.2:3b` | **54%** — CI [38–70] | 35 | 3.2s | 6.1s | 20/35 |

The interval matters more than the headline: 21 runs cannot distinguish a
perfect agent from one that fails 15% of the time. `summarise.py` reports
Wilson intervals precisely so the number is not read as more precise than it
is.

The per-case split is the useful part:

| Case | `qwen3` | `llama3.2` |
| --- | --- | --- |
| oomkill_root_cause | 3/3 | **0/5** |
| crashloop_root_cause | 3/3 | **0/5** |
| image_pull_failure | 3/3 | 3/5 |
| service_unreachable_chain | 3/3 | 5/5 |
| service_selector_typo | 3/3 | 5/5 |
| healthy_not_reported_broken | 3/3 | 4/5 |
| host_not_cluster | 3/3 | 2/5 |

llama3.2 is 14× faster and solves the shallow cases — a service whose selector
matches nothing is visible in one call. It scores **zero** on the two that
require drilling from a status into termination reasons or container logs. It
reports *that* a pod is failing without finding *why*, which is the entire
point. Speed is not the tradeoff; depth is.

Note also that only 10 of 21 correct `qwen3` answers were **fully** grounded.
The rest contained at least one figure the checker could not trace — usually
arithmetic the model did itself. Correct and fully-traceable are different
bars, and the gap between them is worth watching.

Cases assert on substance — the root cause and the tools used — not wording.
One case is a control: an agent that calls everything broken is worse than
useless.

**What these numbers are not.** One synthetic cluster, seven faults, one
machine. Real clusters fail in uglier and more ambiguous ways, and a
7-case suite is a smoke test, not a benchmark.

## Verifying the model's claims

The model is told never to invent a figure. Asking is not enforcing — in
testing, qwen3 once reported an uptime of *18 days* for a host up four hours,
having never called the tool that reports uptime.

So every answer is checked against the tool output behind it:

| `confidence` | Meaning |
| --- | --- |
| `grounded` | every figure and status traces to a tool result |
| `partial` | some claims appear nowhere in the tool output |
| `ungrounded` | the model answered having called no tools at all |

**What it cannot do.** This is lexical matching over numbers and status names,
and it has two real limits:

- It **cannot verify reasoning.** *"The OOM is caused by a memory leak"*
  passes, because it contains no unsupported figure.
- **Incidental numbers launder fabrications.** Tool output is full of digits
  that mean nothing to the claim — timestamps, IPs, pod name hashes. A
  fabricated figure colliding with one reads as grounded. CI caught exactly
  this: the fabricated *"18 days"* test case passed on a runner whose boot
  timestamp was 18:12.

So `partial` is the stronger signal. It reliably means something was not
measured; `grounded` only means nothing contradicted the answer. Treat it as a
smoke alarm, not a proof.

It exempts markdown list numbering and values inside recommendations
(*"raise the limit to 128Mi"* proposes a number rather than claiming one).

## HTTP API

```bash
fastapi dev app.py     # docs at /docs
```

<details>
<summary><b>Endpoints</b></summary>

Kubernetes endpoints take `?namespace=` (default `default`).

| Endpoint | Returns |
| --- | --- |
| `GET /healthz` | Liveness. No dependencies. |
| `GET /readyz` | Readiness. Checks the model backend. |
| `GET /pods` | Status, ready, restarts, node. `?only_unhealthy=true` |
| `GET /pods/{name}` | Images, requests/limits, last termination reason and exit code |
| `GET /pods/{name}/events` | Recent Warning events |
| `GET /pods/{name}/logs` | Last N lines, falling back to a crashed container's previous run |
| `GET /nodes` | Ready state, pressure conditions, allocatable resources |
| `GET /deployments` | Desired vs ready vs available replicas |
| `GET /services/{name}/endpoints` | Selector, ports, ready/not-ready backing pods |
| `GET /platform` `/system` `/processes` `/cpu` `/memory` | Host stats |
| `POST /ask` | Natural-language question → answer, trace, confidence |

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H 'Authorization: Bearer $TRIAGE_API_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"question": "which pods are failing in demo?"}'
```

```json
{
  "answer": "memory-hog is OOMKilled; its 64Mi limit is too low.",
  "tool_calls": [{"name": "list_pods", "arguments": {"namespace": "demo"}}],
  "confidence": "grounded",
  "unverified": []
}
```

**`POST /ask` blocks for as long as the model takes** — tens of seconds is
normal, and a deep chain can exceed two minutes. Set generous client timeouts.
Making this async is the main outstanding API problem.
</details>

<details>
<summary><b>Docker</b></summary>

```bash
docker compose up --build
```

Publishes to `127.0.0.1:8000` with your kubeconfig mounted read-only. Ollama
stays on the host, where it keeps GPU acceleration; `--profile with-ollama`
runs it in a container instead, CPU-only on macOS and much slower.

**The host tools measure the container, not your machine.** `psutil` reads the
namespace it runs in, so `/platform` reports the container. On macOS and
Windows even a privileged container reports the Linux VM. Run `agent.py`
natively for real host metrics — the container is the right home for the
Kubernetes half.

**kind needs an internal kubeconfig**, since its server address is
`127.0.0.1:<port>`, which inside a container is the container itself:

```bash
kind get kubeconfig --name triage-demo --internal > kubeconfig-internal.yaml
```

Then uncomment the `networks:` lines in `docker-compose.yml` and set
`KUBECONFIG` to that file. Managed clusters need none of this.
</details>

## Tests

```bash
pip install -r requirements-dev.txt && pytest
```

No cluster and no model needed — the Kubernetes API and the Ollama client are
mocked. Fixtures use real `V1*` client models rather than bare mocks, so a
projection reaching for a field the API does not have fails the test. CI runs
the suite on Python 3.11–3.13 and separately builds and starts the container.

## Limitations

- **One namespace at a time** for interactive questions. The controller
  watches all namespaces, but there is still no cluster-wide *scan* command;
  that is the biggest functional gap against k8sgpt.
- **The controller holds dedup state in memory**, so a restart forgets what it
  already reported and it cannot be run with more than one replica.
- **Untested on managed clusters.** Everything here was exercised against
  `kind`; EKS/GKE/AKS auth was verified by reading the client, not by running
  against a real cluster.
- **Latency.** Tens of seconds per diagnosis. `kubectl describe` is faster
  when you already know where to look.
- **`/ask` is synchronous** and holds a request open for the whole run.
- **Answers vary between runs.** The same question can produce a different
  chain. The `confidence` field and `tool_calls` trace tell you which
  measurements an answer actually rests on.
- **Cumulative context is unbounded.** Per-call output is projected, but a
  long chain over a busy namespace can still grow past the window.
- **Requires a tool-capable model.** Models without a thinking mode fall back
  automatically, but score materially worse — see the benchmark.
- **No rate limiting**, no audit log of questions, no lockfile.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `TRIAGE_MODEL` | `qwen3` | Ollama model |
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama listens |
| `OLLAMA_TIMEOUT` | `300` | Seconds before a model call is abandoned |
| `K8S_TIMEOUT` | `15` | Seconds before a cluster call is abandoned |
| `TRIAGE_API_TOKEN` | *unset* | Bearer token; unset means no auth |
| `KUBECONFIG` | `~/.kube/config` | Cluster credentials |
| `LOG_FORMAT` | `json` | `text` for human-readable logs |
| `LOG_LEVEL` | `INFO` | Standard Python levels |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Two rules are not negotiable: tools
stay read-only, and tool output stays projected. If you change a prompt, tool
description or projection, run the evals and report before/after in the PR —
prompt changes are code changes with no compiler.

## License

MIT — see [LICENSE](LICENSE).
