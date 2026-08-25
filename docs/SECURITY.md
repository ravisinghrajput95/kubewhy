# kubewhy — security model

A tool that reads pod logs and can be pointed at a hosted model is a tool that
can leak a cluster. This document states what is protected, what it is protected
from, and — as precisely as the evidence allows — what is not.

**Every control listed here is implemented and tested.** Where something is a
design intention rather than an enforced property, it says so.

## Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| Kubernetes metadata | pod specs, events, node status | reveals topology, images, versions, internal hostnames |
| Pod logs | `get_pod_logs` | the richest asset and the most likely to contain a credential someone logged by accident |
| Configuration | ConfigMap and Secret *references* | key names are diagnostic; values are not needed and are not read |
| Credentials encountered in evidence | anywhere in the above | not kubewhy's own, and the reason redaction exists |
| Provider API credentials | `OPENAI_API_KEY` and equivalents | grants spend and, on some providers, access to other data |
| Cluster credentials | kubeconfig, in-cluster ServiceAccount token | the keys to everything above |

## Threats and controls

### Evidence exfiltration

Sending cluster contents to a third party, deliberately or by misconfiguration.

- **Local inference is the default**, and the default keeps every prompt and
  every projected tool result on your network.
- **`TRIAGE_ALLOW_EXTERNAL_INFERENCE` is required** for an external endpoint. An
  external endpoint without it **refuses to start**. A request to one is a **403,
  not a 500** — a 500 reads as a bug and gets retried; a 403 reads as a decision.
- **A fallback is not a way around it.** The policy applies to the fallback
  provider on the same terms.
- **Endpoint classification** decides internal vs external on the name *as
  written*, never resolved. The classifier and the HTTP client normalise through
  the same parser **by construction**. This is not a stylistic choice: two
  parsers disagreeing on one string was an exploitable bypass, found and fixed
  during adversarial validation — IDN full stops and integer-form IPv4 both
  spelled an external host as internal. See [VALIDATION.md](VALIDATION.md).
- **Redaction runs twice** — at collection, and again at the egress boundary.
- **NetworkPolicy** (`networkPolicy.enabled=true`) makes the claim a property the
  dataplane enforces rather than one this process promises. Validated on GKE with
  Calico.

**Limitation, stated plainly:** redaction is pattern matching. It covers AWS
keys, GitHub and Slack tokens, JWTs, private keys, URL passwords, `KEY=value`
secrets, bearer headers, Google API keys and storage account keys. **It will
miss novel formats.** If your logs must never be read by a model, drop the
`pods/log` rule from the ClusterRole — that is the only complete control.

### Excessive Kubernetes permissions

- **The ClusterRole has no write verbs.** No create, update, patch, delete,
  or exec. There is no write path in the code to exercise even if the RBAC
  allowed it.
- **Tools are read-only by construction**, and that is the first of three
  non-negotiable properties this project holds: tools stay read-only, tool output
  stays projected, and errors come back as `{"error": ...}` data rather than
  being raised.
- **Runtime validated on GKE**, by attempting the operations rather than asking
  `kubectl auth can-i` — which lies in at least three ways documented in
  [VALIDATION.md](VALIDATION.md).

### Wrong-target investigation

An answer about a workload nobody asked about is a correctness failure and a
disclosure one: it puts a third party's logs in front of someone investigating
their own service.

- **The target is fixed before the first round** and every tool call is enforced
  against it. Off-target calls are rewritten where arguments allow and refused
  where they do not.
- **Surfaces that know their target pass it as data**, never as prose to be
  re-parsed. Measured: 135/145 runs extracted a target, wrong-target rate 0.7%
  (qwen3) and 0.0% (gpt-4o-mini).

### Hallucinated or overstated root cause

- **Every claim is checked against collected evidence.** Five verdicts, including
  `insufficient_evidence` (nothing here could be checked) and `contradicted` (the
  evidence says otherwise).
- **Unsupported figures are rewritten in place** as `[unverified: …]`, so a
  reader skimming for a number does not find a fabricated one.
- **Contradiction detection is a separate deterministic stage.** Measured on the
  n=5 baseline: 9 contradicted claims (qwen3), 3 (gpt-4o-mini) — caught, not
  shipped as fact.

### Unbounded runs

- **One deadline per investigation** (`TRIAGE_INVESTIGATION_BUDGET`, default
  600s), derived from the p99 of 1273 recorded investigations.
- **The deadline is shared across primary and fallback.** A fallback cannot reset
  it; when the budget is exhausted the fallback is skipped and logged as
  `fallback_skipped_deadline_exhausted`. That defect existed and was fixed.
- **`MAX_ROUNDS`** bounds the loop independently.

### Provider failure

- **Wire-aware failover.** Mid-run failover only between providers sharing a wire
  format, because a conversation half in one dialect is not resumable in another.
- **Readiness verifies the configured model three-valued** — ready, not ready,
  unknown — rather than assuming a reachable endpoint serves the model asked for.

### Browser as an attack surface

- **Streamlit renders server-side.** The browser holds no Kubernetes client and
  no provider credential.
- **Pinned by tests**: a configured API key appears nowhere on the rendered page,
  `ui.py` names no credential and contains no `fetch(`, no provider host and no
  Kubernetes host.
- **The console has no authentication.** It is pinned to loopback for that
  reason, and the Helm chart requires a second explicit acknowledgement
  (`ui.exposureAcknowledged=true`) before it will expose one in-cluster —
  ClusterIP only, no Ingress.
- **The API can be authenticated** with `TRIAGE_API_TOKEN`. Unset, it is open,
  which is acceptable only on loopback.

### Prompt injection through cluster content

Cluster content reaches the model — log lines, image references, annotations.
Fixtures in `demo/adversarial.yaml` carry injection payloads in exactly those
places, and the eval corpus scores them.

**Text in evidence is data, never instruction.** Two scenarios cover it and both
pass at n=5 on both configurations. An eval case may declare a `payload`, and the
run **fails** if that text never reached the model — because an injection test
that passes while its payload never arrived is proving nothing.

## What is not protected

- **Redaction is incomplete by nature.** See above.
- **No audit log of questions asked.**
- **No rate limiting.**
- **A user who can reach the console sees everything the ServiceAccount can
  read.** There is no per-user authorization model.
- **`vllm` is protocol-level support only** — never run against a real vLLM
  server, so nothing about that path is validated at runtime.
