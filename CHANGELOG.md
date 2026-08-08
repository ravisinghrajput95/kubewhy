# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [semver](https://semver.org/); pre-1.0 means the tool
signatures and response shapes may still change.

## [Unreleased]

### Added

- **Probe reporting in `describe_pod`.** Each container's readiness, liveness
  and startup probes: what is checked, and the timings. A container can be
  `Running` with nothing terminated and still be broken — not ready means its
  readiness probe is failing and it is receiving no traffic. The timings are
  there because a probe is as often the cause as the symptom: a container
  needing 60s to start, under a liveness probe that gives it ~20s, reports
  `CrashLoopBackOff` with exit code 137, which reads as an application crash
  or an OOM kill. Only the probe's numbers tell those apart.
- **Two demo workloads that are `Running` and broken** — `never-ready` and
  `slow-starter` — because no existing demo fault was invisible in the pod
  status, so nothing exercised the case.
- **`POST /ask/stream`** — the agent loop as server-sent events, one per tool
  call and result, ending with the same body `/ask` returns. Fixes the silence
  during a long diagnosis, not the blocking: the connection is still held for
  the whole run, and detaching the work needs a job store that survives more
  than one replica — the same unsolved problem as the controller's in-memory
  dedup state.

### Changed

- **`workload_pods()` asks the API server to filter.** It read every pod in the
  namespace and discarded the ones whose owner did not match; it now looks up
  the owning controller's label selector and passes it to the API. Rendering
  all 8 demo workloads went from 96 pod objects transferred to 31, with
  identical output. The owner reference still decides membership — the
  selector only narrows what is fetched. CronJobs, static pods and bare pods
  have no single selector and still fall back to the full read.
- **`mcp` 1.29 → 2.0.** `FastMCP` no longer exists; the server is built on
  `MCPServer` from `mcp.server`. The port moved from server settings into
  `run()`, which mattered — setting `settings.port` under 2.0 binds 8000 and
  says nothing. Verified with a real MCP client over stdio and streamable
  HTTP, not just the mocked tests: 13 tools advertised with descriptions
  intact, live cluster reads, and a 404 still returning `{"error": ...}` as
  data rather than a protocol error frame.
- Clients now see an **empty server version** where 1.29 reported `1.29.0`.
  That was the SDK's version rather than kubewhy's, so it was never right;
  setting it properly needs a version constant this project does not have.

## [0.1.2] — 2026-08-07

The rename release. Republishes the image and chart under the new name — v0.1.1
and earlier exist only at the `local-triage-agent` paths, which are not moved
by a repository rename.

### Changed — breaking

- **Renamed from `local-triage-agent` to `kubewhy`.** "Agent" had become one of
  five surfaces, and the old name never said Kubernetes. The GitHub repository
  redirects; **published artifacts do not**. Until the next `v*` tag the image
  and chart exist only at the old paths:
  `ghcr.io/ravisinghrajput95/local-triage-agent` and
  `oci://ghcr.io/…/charts/local-triage-agent`. `TRIAGE_*` variables, the
  `triage` namespace, the `triage-agent` ServiceAccount and the `triage.*`
  logger names are deliberately unchanged — renaming them would break every
  existing deployment for no benefit.

### Added

- **Cluster-wide scan** (`scan_cluster`) — finds failing workloads across every
  namespace in one API call, closing the biggest functional gap against k8sgpt.
  Exposed on all three surfaces: as a model tool, as `GET /scan`, and over MCP.
  Results are grouped by owning workload and by *fault* rather than status
  name, so three crashing replicas are one entry and a rollout with pods in
  both `ErrImagePull` and `ImagePullBackOff` is one finding rather than two.
  Measured on a 19-pod cluster: ~146 tokens against ~33,042 raw.
- **`python agent.py --scan`** — prints the scan without involving the model,
  returning in under a second; `--explain N` then spends a full diagnosis on
  the N workloads with the largest blast radius.
- **Browser UI** (`ui.py`, `requirements-ui.txt`) — the scan as a table, pod
  drill-down, and an ask panel that renders the tool chain as it runs. Streamlit
  telemetry and its bind-all-interfaces default are overridden in
  `.streamlit/config.toml`; both would otherwise contradict the project's
  claims. Tested headlessly with Streamlit's `AppTest` in a separate CI job, so
  the default install and the main test matrix stay lean.
- **`agent.stream()`** — the agent loop as an event generator
  (`tool_call` / `tool_result` / `answer`). `ask()` is now this drained to
  completion, so the two cannot drift. Needed for any surface that shows
  progress, and the same shape a streaming `/ask` endpoint will want.
- **Context selection** (`active_context`, `list_contexts`, `use_context`) —
  report and switch the cluster a surface is bound to.

### Fixed

- **A surface could name the wrong cluster.** The client is built once and
  cached, but the context was re-read from the kubeconfig on each render, so
  creating a cluster in another shell — which rewrites `current-context` —
  relabelled the UI while it kept querying the original cluster. Found live:
  the page said `kind-loglens-cri` while showing pods that exist only in
  `kind-triage-demo`.
- **Recommendations were flagged as unmeasured claims.** The claim checker
  splits on `:`, so `limits.memory: 256Mi` tore the proposed numbers away from
  the verb proposing them and reported a correct answer as `partial`. That
  fired on `key: value`, which is how every resource recommendation is written.
  A line now stays prescriptive from its verb to the end of the line.

### Changed

- `workload_of` and `FAULT_CLASS` moved from `controller.py` into
  `routers/k8s_pods_info.py`. The scan and the controller have to agree on what
  counts as the same problem, and two copies would drift.

### Added — working at cluster scale

- **`scan_cluster(namespaces=…)`** and paged fetching. Pods are read a page at
  a time, so no single request has to carry a multi-megabyte response inside
  `K8S_TIMEOUT`; a single namespace becomes a namespaced query rather than a
  cluster-wide one. The browser UI exposes the same filter plus a name search,
  because a flat list of a thousand workloads is not navigable in either
  surface.
- **`scan_cluster(workload=…)`** reports one workload's state whether or not it
  is broken. Without it there was no way to answer "is X healthy?" with "yes":
  the scan returned only failures, so a question about a healthy workload found
  nothing and the model answered with a *different* workload's problem —
  confidently, and marked `grounded`, because every claim was true of the
  workload it had substituted. The system prompt now also says to report a
  healthy workload as healthy and never to describe one that was not asked
  about.
- **`get_pod_logs` reports which container it read**, and takes an explicit
  `container` argument.
- **The demo cluster grew the shapes that were hiding bugs**: a succeeding and
  a failing CronJob, a failing init container, a DaemonSet, and a
  two-container pod. Every fix below was invisible against five Deployments.

### Fixed — detection on real clusters

The demo cluster is five Deployments, so every assumption that holds only for
Deployments went unnoticed. None of these are visible there; all are ordinary
anywhere else.

- **Completed Jobs were reported as failures.** A Succeeded pod is not Running
  and has no ready containers, so a readiness-only check called every finished
  CronJob run broken. Seen directly: a scan of a real cluster listed two
  `Completed` pods as unhealthy.
- **Failing init containers were invisible.** A crashlooping "wait for the
  database" init container reports phase `Pending` with no app container
  status, so it read as `Pending` and the controller ignored it entirely.
  `_pod_status` now reports `Init:<reason>` as kubectl does, and it classifies
  and dedups as the same fault as any other crash.
- **Static pods were grouped by node.** `kube-apiserver`, `etcd` and
  `kube-scheduler` are owned by the Node they run on, so every control-plane
  component on a node collapsed into one finding named after the node.
- **Every CronJob run was a new workload.** Jobs created by a CronJob are
  `<name>-<timestamp>`; keeping the timestamp meant the per-workload cooldown
  never applied and an hourly failure would report hourly, forever.
- **The claim checker scopes claims to the entity they name.** A status
  measured for one workload no longer supports the same status asserted about
  another — the weakness `scan_cluster` made worse by returning every failing
  workload in one result. A clause naming no entity still falls back to
  checking against everything, and entity matching is substring, so both
  remaining loosenesses fail toward silence rather than false alarms.
- **Test fixture `exit_code or 1`** turned every successful termination into a
  failure, which is why the init-container case looked correct at first.
- **API errors discarded the server's explanation.** `_handle` reported only
  `reason`, so asking for the logs of an `ImagePullBackOff` pod gave
  "kubernetes API error 400: Bad Request" while the response body said
  `container "app" ... is waiting to start: image can't be pulled`. The body is
  the diagnosis; it is now kept.
- **"No logs yet" is no longer reported as a failure.** A container that never
  started has no logs and never will, which is an expected state for
  `ImagePullBackOff` or a failing init container. It now returns a result
  naming the reason and pointing at `describe_pod` / `get_pod_events`.
- **Events carry an age.** Events are history, not state: a `FailedScheduling`
  warning from before a pod was scheduled stays in its list forever, so an
  ageless projection showed a 27-minute-resolved problem on a Running pod as
  though it were current.

## [0.1.0] — 2026-08-04

First tagged release. Read-only Kubernetes and host diagnosis driven by a
local model, with claim verification and a scored eval suite.

### Added

- **Kubernetes tools** — pods, pod detail, events, logs, nodes, deployments
  and service endpoints. All output is projected rather than raw: a 5-pod
  namespace is ~7,560 tokens from the API and ~91 after projection, which is
  what keeps a multi-step diagnosis inside the context window.
- **Host tools** — platform, system utilisation, processes, top CPU and
  memory consumers.
- **Tool-calling agent** (`agent.py`) over any tool-capable Ollama model,
  bounded by `MAX_ROUNDS`, falling back automatically for models without a
  thinking mode.
- **Claim verification** (`grounding.py`) — figures and status names in an
  answer are checked against the tool output behind it and reported as
  `grounded`, `partial` or `ungrounded`, with unsupported claims listed.
- **MCP server** (`mcp_server.py`) exposing all 12 tools over stdio or
  streamable HTTP, for Claude, Cursor, Zed and other MCP clients.
- **HTTP API** (`app.py`) with bearer auth, split `/healthz` and `/readyz`,
  and structured JSON request logging.
- **Secret redaction** (`redaction.py`) for pod logs and event messages.
- **Read-only RBAC** (`deploy/rbac.yaml`), verified to deny every mutation.
- **Eval suite** (`evals/`) scoring the agent against a deliberately broken
  demo cluster, with Wilson confidence intervals via `summarise.py`.
- **Demo workloads** (`demo/broken-pods.yaml`) covering CrashLoopBackOff,
  OOMKilled, ImagePullBackOff and two distinct service-endpoint faults, plus
  healthy controls.
- Docker image and compose stack, published to loopback by default.
- CI across Python 3.11–3.13 plus a container build-and-serve check.

### Security

- Dependencies pinned to minor versions; Dependabot enabled.
- Compose publishes to `127.0.0.1` rather than all interfaces.
- Timeouts on every cluster call (`K8S_TIMEOUT`) and model call
  (`OLLAMA_TIMEOUT`).

### Known limitations

- One namespace at a time; no cluster-wide scan.
- `POST /ask` is synchronous and can hold a request open for minutes.
- Claim verification is lexical and cannot check reasoning.
- Cumulative context across a long chain is unbounded.

[Unreleased]: https://github.com/ravisinghrajput95/kubewhy/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/ravisinghrajput95/kubewhy/compare/v0.1.1...v0.1.2
[0.1.0]: https://github.com/ravisinghrajput95/kubewhy/releases/tag/v0.1.0
