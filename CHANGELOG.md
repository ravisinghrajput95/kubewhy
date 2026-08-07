# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [semver](https://semver.org/); pre-1.0 means the tool
signatures and response shapes may still change.

## [Unreleased]

### Added

- **`POST /ask/stream`** — the agent loop as server-sent events, one per tool
  call and result, ending with the same body `/ask` returns. Fixes the silence
  during a long diagnosis, not the blocking: the connection is still held for
  the whole run, and detaching the work needs a job store that survives more
  than one replica — the same unsolved problem as the controller's in-memory
  dedup state.

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

### Known issues

- The claim checker flattens all tool output into one blob, so a status
  measured for one workload supports the same status claimed about another.
  The cluster-wide scan widens each result and therefore weakens this check;
  confirmed against a live cluster and pinned in `tests/test_grounding.py`.

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
