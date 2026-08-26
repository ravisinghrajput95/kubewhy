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

### No record of who read what

- **One audit record per investigation**, on the `triage.audit` logger as
  `msg: investigation`, and optionally appended to `TRIAGE_AUDIT_LOG` as well.
  It names the principal and how they authenticated, the surface, the
  (redacted) question, the target, every tool called and with what arguments,
  the namespaces touched, which pods' **logs** were read specifically, the
  verdict, and where inference was configured to happen.
- **Emitted for every run, not only successful ones.** A run that raised, one
  that hit its deadline, and one whose caller closed the browser tab all
  produce a record — that last is `abandoned` rather than `error`, because an
  audit trail that files closed tabs as failures has people chasing incidents
  that did not happen.
- **The record does not carry the evidence.** Not the tool output, not the
  answer (it quotes the evidence), not the inference endpoint (it can carry a
  token in its userinfo), not an exception message (a provider's error text
  can quote the request). The question is carried, capped and passed through
  `redaction.redact()`, because a person can paste a credential into it.
  Verified live: the string a demo pod actually logs
  (`FATAL: could not connect to db:5432`) appears nowhere in the record of the
  run that read it, while `sensitive_reads` names the pod whose logs were read.
- **`evidence_left_network` is derived from policy, not observed.**
  `inference.Gateway` holds one `active` target for the whole process, so
  reading it at the end of a run is right with one investigation in flight and
  wrong with two. Policy is provable: `allow_external=False` makes egress
  impossible because the gateway raises before a request is built. It is
  three-valued — `false`, `true`, `"possible"` — because an auditor can act on
  "possible" and cannot act on a `false` that meant "probably not".

**Limitations.** The record says a tool was called and what it was called with;
it cannot say what a person then did with the answer. It is written to a log
stream this process can also write other things to, so it is as tamper-evident
as your log pipeline and no more. And `TRIAGE_AUDIT=0` turns it off, which is a
decision someone has to make rather than a default.

### An unbounded caller

`TRIAGE_INVESTIGATION_BUDGET` bounds one run; these bound how many.

- **Investigations per caller per hour** (`TRIAGE_MAX_INVESTIGATIONS_PER_HOUR`,
  default 60). Per principal, which is what the authentication above
  establishes — a shared bucket would let one runaway client lock out the
  person trying to diagnose the incident it caused. Refused as **429** with a
  `Retry-After` naming the seconds until the window frees, not the window
  length. Generous by design: at the recorded 41s median one process cannot
  run much past 88 an hour serially, so it cannot bite a person working an
  incident.
- **External inference tokens per hour** (`TRIAGE_MAX_EXTERNAL_TOKENS_PER_HOUR`,
  default off). Counted **only where evidence actually left the network** —
  local and in-cluster inference spend nothing per token, and a ceiling there
  would be friction with nothing behind it. Counted in tokens rather than
  currency: a price table per model goes stale, and the provider reports
  tokens exactly.
- **A sliding window, not a fixed bucket.** A fixed hourly bucket lets a caller
  spend the whole allowance in the last minute of one hour and the whole
  allowance again in the first minute of the next.
- **Only the endpoints that drive the model carry it.** `/scan` and `/pods`
  cost no model time, and the same ceiling on them would make the console's own
  browsing count against the person using it.

**Limitations, and they matter for the cost half.** These are guardrails
against a loop that got away, **not billing controls**. If money is at stake,
set a spend cap with your provider — this process can decline to start work and
that is all it can do; it cannot recall a request already in flight.

The windows are **in memory and per process**. A restart clears them, bounded
by the window length. And a deployment running the API, the controller and the
console as separate pods has three independent windows, not one shared budget.
**The console does not enforce the ceiling at all**: a person clicking a button
is not the runaway loop this exists to stop, and blocking an SRE mid-incident
would cost more than it saved. Only the API refuses.

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
- **The console can be authenticated** with `ui.auth.enabled=true`, and the
  enforcement is structural rather than a check the app performs. See below.
- **The API can be authenticated** with `TRIAGE_API_TOKEN`, and accepts a
  proxy-asserted identity in `TRIAGE_AUTH_MODE=proxy`. With neither it is open,
  which is acceptable only on loopback.

### Unauthenticated access to the console

Streamlit has no route layer to hang an authenticator on: anything the process
checks runs after the connection is accepted and the websocket is up, which
makes app-level authentication something a bug can undo. So the control is
structural.

- **An authenticating proxy is the only listener the Service targets.**
  `ui.auth.enabled=true` adds an oauth2-proxy sidecar, flips the console's bind
  from `0.0.0.0` to `127.0.0.1`, and points the Service at the proxy. The
  console's own port is then in no Service at all. Same argument as
  NetworkPolicy: a property the platform enforces beats one this process
  promises.
- **A sidecar rather than an ingress annotation**, deliberately. Sharing the
  pod's network namespace is what lets the console bind loopback; an
  ingress-level authenticator leaves the console's port reachable from anywhere
  in the cluster, which is the arrangement that looks authenticated and is not.
- **The app fails closed on the misconfiguration.** `TRIAGE_AUTH_MODE=proxy` is
  the operator declaring a proxy is in front; a request that then carries no
  identity header is refused rather than served anonymously. `st.stop()` fires
  before the scan, so a refused caller causes no cluster read — a page that
  collects pod logs and then declines to render them has already made the
  disclosure.
- **The API additionally checks the peer address.** A well-formed identity
  header arriving from off the pod's loopback is refused, because that request
  has proved the premise the header trust rests on is false. Streamlit exposes
  no peer address, so the console relies on the bind alone.

**Validated on kind v1.32.2 against a real OIDC issuer** (Dex v2.41.1,
oauth2-proxy v7.7.1), from a separate pod: the console's port is
`ConnectionRefused`, the proxy's is open, an unauthenticated request through
the Service is a 302 to the provider, `/_stcore/stream` is gated the same way
as `/`, and **a forged `X-Forwarded-Email` presented with a valid session
reaches the app as the real address** — the proxy overwrites the client's
header rather than appending to it. That last one is the property the whole
design rests on. See [VALIDATION.md](VALIDATION.md).

**Limitations, stated plainly.** Header trust is only as good as the guarantee
that nothing else can reach the backend; the loopback bind is that guarantee,
and an operator who restores `--server.address=0.0.0.0` has removed it, leaving
only the app-level check. `networkPolicy.enabled=true` selects the console pod
and permits egress only to private address space, so it cannot be combined with
a SaaS identity provider. And the console is only as authenticated as the
provider in front of it — kubewhy verifies a header, not a token signature.

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
- **Authentication is not authorization, and there is no per-user authorization
  model.** Everyone who signs in sees everything the ServiceAccount can read.
  This is a design decision, not an omission: kubewhy is built for one SRE team
  against one cluster, the tools take `namespace` as a filter the caller picks
  rather than a boundary, and making it a boundary would mean Kubernetes
  impersonation — which needs the `impersonate` verb and turns kubewhy from a
  least-privilege reader into a credential broker. Narrow the ClusterRole, or
  run one release per team with its own ServiceAccount.
- **`vllm` is protocol-level support only** — never run against a real vLLM
  server, so nothing about that path is validated at runtime.
