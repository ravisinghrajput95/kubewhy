# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [semver](https://semver.org/); pre-1.0 means the tool
signatures and response shapes may still change.

## [Unreleased]

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

[Unreleased]: https://github.com/ravisinghrajput95/local-triage-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ravisinghrajput95/local-triage-agent/releases/tag/v0.1.0
