# local-triage-agent

[![tests](https://github.com/ravisinghrajput95/local-triage-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/ravisinghrajput95/local-triage-agent/actions/workflows/tests.yml)

Diagnoses failing Kubernetes pods and host resource problems using a local LLM
via [Ollama](https://ollama.com). Reads logs, events and resource limits to
find root causes. Nothing leaves your machine.

It triages in the medical sense — it works out what is wrong and how bad it is.
It never scales, restarts or deletes anything; every tool is read-only by
design.

Unlike [k8sgpt](https://github.com/k8sgpt-ai/k8sgpt), which is more mature and
built for cluster-wide scanning, this runs entirely on your own hardware with
no cloud API key, covers the host alongside the cluster, and returns the tool
calls behind every answer so you can check what it actually measured.

Both surfaces are exposed as a FastAPI service and as tools the model can call:
host stats (CPU, memory, disk, processes) and cluster state (pods, events,
logs, nodes, deployments, service endpoints).

Given a namespace it has never seen, it works down the chain from symptom to
root cause on its own:

```
$ python agent.py "Something is wrong in the demo namespace. What is broken and why?"
  -> list_pods({'only_unhealthy': True, 'namespace': 'demo'})
  -> describe_pod({'name': 'bad-image-647c5576d5-lkk52', 'namespace': 'demo'})
  -> describe_pod({'name': 'crasher-5964d99948-466pd', 'namespace': 'demo'})
  -> get_pod_logs({'name': 'crasher-5964d99948-466pd', 'tail': 50, ...})
  -> describe_pod({'name': 'memory-hog-bc76968c6-z92zr', 'namespace': 'demo'})

1. bad-image      Image pull failed (nginx:this-tag-does-not-exist doesn't
                  exist). Fix: update the image spec to a valid tag.
2. crasher        Crashes with "FATAL: could not connect to db:5432:
                  connection refused". Fix: verify the database service is
                  running and reachable.
3. memory-hog     OOMKilled: the 64Mi memory limit is too low. Fix: raise the
                  limit and check for a leak.
```

Note that it reports the *reason* behind each failure, not just the status
name — the database error came from reading the crashed container's logs.

It follows the same chain across resource types. Asked why a *service* is
unreachable, it goes from the service to its backing pods to their logs:

```
$ python agent.py "The crasher-svc service in the demo namespace is unreachable. Why?"
  -> get_service_endpoints({'name': 'crasher-svc', 'namespace': 'demo'})
  -> list_pods({'namespace': 'demo', 'only_unhealthy': True})
  -> describe_pod({'name': 'crasher-5964d99948-28p5k', ...})
  -> get_pod_logs({'name': 'crasher-5964d99948-28p5k', 'tail': 100, ...})

crasher-svc is unreachable because its pod is crashing with exit code 1,
repeatedly restarting due to a database connection failure. The logs show it
cannot connect to db:5432, which is refused.
```

## Requirements

- Python 3.11+ (developed on 3.14)
- [Ollama](https://ollama.com) installed and running
- The `qwen3` model pulled locally
- Optional: a Kubernetes cluster. The host tools work without one; the pod
  tools return a clear "cluster unreachable" error rather than crashing.

## Getting started

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Pull the model and make sure Ollama is up:

```bash
ollama pull qwen3
ollama serve          # skip if the Ollama desktop app is already running
```

Verify the model supports tool calling — the `Capabilities` block must list
`tools`:

```bash
ollama show qwen3
```

### Optional: a cluster to diagnose

To reproduce the example above from scratch, spin up a throwaway cluster and
deploy the deliberately broken workloads in `demo/`:

```bash
kind create cluster --name triage-demo
kubectl apply -f demo/broken-pods.yaml
kubectl get pods -n demo        # wait for the failure states to appear
```

That manifest creates one pod per common failure mode — `CrashLoopBackOff`,
`OOMKilled`, `ImagePullBackOff` — plus two broken services (one whose selector
matches nothing, one whose pods never become ready) and healthy deployments and
services as controls, so the agent has to distinguish broken from working
rather than calling everything broken.

Tear it down with `kind delete cluster --name triage-demo`.

## Usage

### Ask the agent directly

```bash
python agent.py "is anything eating memory?"
python agent.py "how long has this host been up, and who is logged in?"
```

The agent prints each tool call it makes to stderr, so you can see what it
actually measured versus what it inferred. Pipe stderr to `/dev/null` for just
the answer.

### Run the HTTP service

```bash
fastapi dev app.py
```

Then open <http://127.0.0.1:8000/docs> for the interactive API docs.

#### Raw stat endpoints

| Method | Endpoint     | Returns                                              |
| ------ | ------------ | ---------------------------------------------------- |
| GET    | `/platform`  | OS, hostname, boot time, uptime                       |
| GET    | `/system`    | CPU %, memory %, root disk %, logged-in user          |
| GET    | `/processes` | All running processes grouped as `{name: [pids]}`     |
| GET    | `/cpu`       | Top 5 processes by CPU                                |
| GET    | `/memory`    | Top 5 processes by memory                             |

#### Kubernetes endpoints

All take `?namespace=` (default `default`).

| Method | Endpoint             | Returns                                          |
| ------ | -------------------- | ------------------------------------------------ |
| GET    | `/pods`              | Pods with status, ready, restarts, node. `?only_unhealthy=true` filters to problems |
| GET    | `/pods/{name}`       | Images, requests/limits, and last termination reason and exit code |
| GET    | `/pods/{name}/events`| Recent Warning events only                        |
| GET    | `/pods/{name}/logs`  | Last N lines, falling back to the crashed container's previous run |
| GET    | `/nodes`             | Ready state, active pressure conditions, allocatable CPU/memory |
| GET    | `/deployments`       | Desired vs ready vs available replicas, and images |
| GET    | `/services/{name}/endpoints` | Selector, ports, and ready/not-ready backing pods |

```bash
curl 'http://127.0.0.1:8000/pods?namespace=demo&only_unhealthy=true'
```

#### Agent endpoint

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "which process is using the most CPU?"}'
```

```json
{
  "answer": "The process using the most CPU is com.apple.Virtualization.VirtualMachine (PID 2485) with 8.0% CPU utilization.",
  "tool_calls": [{ "name": "get_top_cpu_processes", "arguments": { "limit": 1 } }],
  "confidence": "grounded",
  "unverified": []
}
```

The `tool_calls` array is the audit trail — it tells you which measurements the
answer was actually built from.

## Checking the model's claims

The model is told never to invent a figure, but asking is not enforcing. In
testing, qwen3 once reported an uptime of *18 days* for a host that had been up
four hours, having never called the tool that reports uptime.

So every answer is checked against the tool output behind it. Numbers and
status names are pulled out of the answer and looked up in what the tools
actually returned:

| `confidence` | Meaning                                            |
| ------------ | -------------------------------------------------- |
| `grounded`   | every claim traces back to a tool result            |
| `partial`    | some claims appear nowhere in the tool output       |
| `ungrounded` | the model answered having called no tools at all    |

Anything unsupported is listed in `unverified`, and the CLI prints it to
stderr. On the real hallucination above, the check returns
`partial` with `unverified: ["18"]`.

It is a lint, not a gate — it flags claims the model did not read anywhere,
which is usually fabrication and occasionally arithmetic it did itself. Two
deliberate exemptions keep it quiet enough to be worth reading: markdown list
numbering, and values in recommendations (*"raise the limit to 128Mi"* proposes
a number rather than claiming one).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

No cluster and no model are needed — the Kubernetes API and `ollama.chat` are
both mocked. The suite covers three things:

- **Projections stay small and keep the diagnostic fields.** One test asserts
  a token ceiling directly, so a projection that regresses to returning raw
  objects fails the build rather than silently exhausting the context window.
- **Status precedence.** A `CrashLoopBackOff` pod reports phase `Running`;
  tests pin the rule that the container's waiting or terminated reason wins,
  because reporting the phase would tell you everything is fine.
- **Failures stay contained.** Unreachable clusters, 403s, unknown tools and
  exceptions inside a tool must all come back as data, never raise, so the
  agent can report and carry on.

Fixtures use real `V1*` client models rather than bare mocks, so a projection
that reaches for a field the API does not have fails the test.

CI runs the suite on Python 3.11–3.13 and separately builds the container and
checks it serves, on every push and pull request.

### Evals: does it actually diagnose?

The unit tests prove the code is correct. They say nothing about whether the
agent reaches the right conclusion — so there is a second suite that asks the
real model real questions against the deliberately broken demo cluster, where
every fault is known in advance.

```bash
kind create cluster --name triage-demo
kubectl apply -f demo/broken-pods.yaml
python evals/run_eval.py                          # defaults to qwen3
python evals/run_eval.py --model llama3.2 --repeat 3
```

Cases assert on substance rather than wording — the root cause, the tools it
should have used, and for the healthy control, what it must *not* claim. Since
the model is non-deterministic, `--repeat` reports a pass rate; one failure is
noise, a low rate is a finding.

Measured on the demo cluster, one run per case:

| Model      | Score      | Avg per case | Notes                                   |
| ---------- | ---------- | ------------ | --------------------------------------- |
| `qwen3`    | 7/7 (100%) | 56s          | Reaches every root cause                 |
| `llama3.2` | 4/7 (57%)  | 4.4s         | 13× faster, stops at the symptom         |

The split is informative: llama3.2 handles the shallow cases — a service with
no endpoints, telling host from cluster — but fails the ones needing a drill
down into logs or resource limits. It reports *that* a pod is failing without
finding *why*. If you want fast triage of obvious faults it is usable; for root
causes, qwen3 earns its latency.

This suite lives outside `tests/` and never runs in CI, since it needs a
cluster and a model.

## Running in Docker

```bash
docker compose up --build
```

The service comes up on <http://127.0.0.1:8000>, with your kubeconfig mounted
read-only and `OLLAMA_HOST` pointed at the host.

Ollama deliberately stays on the host, where it keeps GPU/Metal acceleration.
If you want a fully self-contained stack anyway:

```bash
docker compose --profile with-ollama up
```

On macOS that container is CPU-only and noticeably slower, so the host install
is the better default.

### The host tools measure the container, not your machine

This is the one thing to understand before containerising. `psutil` reads the
namespace it runs in, so inside the container `/platform` reports the container:

```json
{ "OS": "Linux-6.12.76-linuxkit-aarch64", "Hostname": "70a98c1dbc1d",
  "user": null }
```

On macOS and Windows, Docker runs inside a Linux VM, so even a privileged
container would report that VM rather than your actual laptop. If you care
about real host metrics, run `agent.py` natively — the container is the right
home for the Kubernetes half, not the host half.

### Talking to a kind cluster

A kind kubeconfig points at `https://127.0.0.1:<port>`, which inside a
container resolves to the container itself. Use the internal kubeconfig and
join the cluster's network:

```bash
kind get kubeconfig --name triage-demo --internal > kubeconfig-internal.yaml
```

That rewrites the server to `https://triage-demo-control-plane:6443`, reachable
over kind's docker network. Then uncomment the `networks:` lines in
`docker-compose.yml` and set `KUBECONFIG=./kubeconfig-internal.yaml`. A managed
cluster (EKS/GKE/AKS) needs none of this — its endpoint is already routable.

## How it works

Ollama cannot reach your HTTP endpoints on its own. What it supports is *tool
calling*: the model is given a set of tool schemas, and replies with a
structured request to invoke one. `agent.py` executes the matching Python
function, feeds the result back, and repeats until the model has enough to
answer.

```
agent.py          tool-calling loop, system prompt, tool registry
grounding.py      checks answers against the tool output behind them
app.py            FastAPI routes (raw stats, cluster state, /ask)
routers/          the collectors, one per concern
tests/            pytest suite, no cluster or model required
evals/            scored runs against the demo cluster and a real model
demo/             deliberately broken manifests to diagnose against
```

Each collector in `routers/` is a plain function returning a JSON-able dict, so
it serves as both a FastAPI route handler and a model tool with no adapter in
between. Tool schemas are derived automatically from each function's signature
and docstring — **the docstrings are the tool descriptions the model reads**, so
edit them with that in mind.

To add a capability: write the function in `routers/`, give it a docstring
explaining when to use it, and register it in the `TOOLS` dict in `agent.py`.

### Tool output is projected, deliberately

The pod tools never return raw Kubernetes objects. Measured against the demo
namespace:

| Response                        | Tokens |
| ------------------------------- | ------ |
| Raw `list_namespaced_pod` (5 pods) | ~7,560 |
| `list_pods(only_unhealthy=True)`   | ~91    |

That is roughly 1,500 tokens per pod before projection, so a 50-pod namespace
would exceed qwen3's entire 40k context in a single call. Every field the model
does not need to diagnose a fault is dropped. When adding a tool, decide what
the model actually needs and return only that — it is the difference between an
agent that works and one that runs out of context on its first call.

## Notes and limitations

- **Thinking mode is on by default.** With it disabled, qwen3 tends to answer
  multi-part questions from only the first tool it calls and invent the rest —
  in testing it reported an uptime of "18 days" for a host that had been up 4
  hours. `ask(..., think=False)` is faster but not trustworthy for compound
  questions. Verify against the `tool_calls` trace either way.
- **The model can still be wrong.** It reports what the tools measured, but the
  interpretation is the model's. Treat answers as a starting point.
- **Latency.** `/system` and `/cpu` each sample for ~1 second by design. An
  agent chaining several calls compounds that, on top of model inference time.
- **`/processes` is large** — ~500 entries on a typical desktop, roughly 3k
  tokens. Pass `name_filter` to narrow it; the agent is instructed to do so.
- **No authentication.** These endpoints expose hostname, usernames, the full
  process table and cluster state. Bind to localhost; do not expose this
  publicly as-is.
- **Cluster credentials matter.** The pod tools use whatever context your
  kubeconfig points at, with your permissions. They only ever read, but point
  this at a production cluster and pod logs — which can contain secrets — flow
  into the model's context. Use a read-only service account, and check your
  current context with `kubectl config current-context` before asking.
- **Pod tools are read-only by design.** Nothing here scales, deletes or
  restarts anything. Diagnosis is a much safer thing to automate than
  remediation.
- **Answers vary between runs.** The same question can produce a different
  chain of tool calls. In one run the agent read the crasher's logs and quoted
  the exact database error; in another it stopped at the status and said the
  pod was "likely OOMKilled" without checking. The `confidence` field and the
  `tool_calls` trace tell you which happened, and `evals/` measures how often
  it gets there at all.

## Configuration

| Variable      | Default             | Purpose                            |
| ------------- | ------------------- | ---------------------------------- |
| `TRIAGE_MODEL` | `qwen3`             | Ollama model to use                |
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama is listening     |
| `KUBECONFIG`  | `~/.kube/config`    | Cluster credentials                |

## License

MIT — see [LICENSE](LICENSE).
