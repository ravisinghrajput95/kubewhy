# kubewhy — Building an Evidence-First Kubernetes AIOps Agent

## Problem

Everything in Kubernetes tells you *what* is broken. `kubectl get pods` says
`CrashLoopBackOff`; the dashboard says the same thing in colour. Neither says
why, and finding out means a sequence a person performs from memory: describe
the pod, read the termination reason, pull the events, read the dead container's
logs, notice the service has no ready endpoints.

That sequence is mechanical, which makes it a candidate for automation. But
pointing a language model at a cluster produces a specific and predictable set of
failures — hallucinated causes, drift onto a louder broken neighbour, confident
answers with nothing behind them, and cluster contents leaving the network.

The interesting question was never "can an LLM diagnose Kubernetes". It was:
**what has to be true of the system around the model for its answer to be worth
acting on?**

## Design

One principle, applied consistently: **the model chooses how to investigate; it
does not decide what is true, what it is investigating, or when to stop.**

| Deterministic | Model |
|---|---|
| Which tools exist and what they return | Which tool to call next |
| Tool arguments, after scope enforcement | The arguments it proposes |
| Whether a claim is supported, contradicted or unsupported | Which claims to make |
| When the investigation stops | When it would like to stop |
| Where evidence may go | — |

The consequence is the property worth having: **kubewhy's safety guarantees do
not depend on the model behaving well.** Swapping models changes the answers. It
does not change what the tools may do, where evidence may go, how long a run may
take, or how a claim is scored.

## Why evidence first?

Two decisions did most of the work.

**Projection, not raw objects.** Tools return a projection — the fields that
matter for diagnosis — rather than the API object. A `V1Pod` is thousands of
tokens of managed fields and last-applied annotations; the diagnostic content is
perhaps twenty fields. Projecting keeps the context window usable and makes the
tool output something a *test* can assert against.

**Claims are checked against what was collected, not against the world.** The
checker never asks "is this true of Kubernetes?" It asks "does this figure
appear in a tool result from this run, and where?" That question is answerable
deterministically, which means it can be unit-tested, and it produces a citation
as a by-product.

## Architecture

Six surfaces — CLI, REST, MCP, watch controller, Streamlit console, Slack —
share one set of read-only tools and one inference gateway. Tools are plain
Python functions returning JSON-able dicts, which is what lets one definition
serve all six with no adapter.

Grounding sits **between** the investigation and the diagnosis. See
[ARCHITECTURE.md](ARCHITECTURE.md).

## Security model

Read-only RBAC with no write verbs; redaction at collection and again at the
egress boundary; an external-data policy that refuses to start rather than
silently sending cluster contents to a vendor; NetworkPolicy so the dataplane
enforces the claim rather than the process promising it. See
[SECURITY.md](SECURITY.md).

## Inference abstraction

Where the model runs — a workstation, this cluster, or a hosted API — is
configuration. `backends.py` is the protocol; `inference.py` is where inference
happens plus egress policy, failover and telemetry. The gateway presents the same
four methods a backend does, so the agent loop is unchanged across all three.

Failover is **wire-aware**: mid-run failover happens only between providers
sharing a wire format, because a conversation half in one dialect is not
resumable in another.

## Failure cases discovered

The most valuable output of this project is the list of things that were wrong.
Every one was found by testing, not by review.

### Endpoint classification bypass

**Problem** — an external endpoint could be spelled so it classified as internal,
defeating the egress policy entirely.
**Detection** — adversarial validation, deliberately attacking the boundary.
**Root cause** — the classifier and the HTTP client parsed the endpoint
separately. IDN full stops and integer-form IPv4 normalised differently in each.
**Fix** — both normalise through the same parser, by construction.
**Regression** — classifier tests including IDN, integer IPv4 and IPv6 literals.
A first repair broke IPv6; that is now tested too.

**Lesson:** two parsers on one string agree only by coincidence.

### The target re-derived from the prompt

**Problem** — every scoped investigation died on `no workload named example
exists in this cluster`.
**Detection** — driving the console by hand. It reproduced identically on two
unrelated models, which is what proved it was not the model.
**Root cause** — the loop discarded the target it was given and recovered it by
parsing the prompt the system had just written. `(for example pod nightly-sync)`
parses as a workload called `example`; remove that phrase and "any **other
workload**" yields `other`. Enforcement then rewrote correct calls to the phantom.
**Fix** — the caller passes the target as data. Parsing survives only where there
is genuinely only a sentence.
**Regression** — 20 tests, two workloads in different namespaces, 14 confirmed
red against the previous code.

**Lesson:** I spent a day writing this up as a prompt-engineering problem. It was
deterministic code overwriting correct work.

### Grounding could say CONTRADICTED but not SUPPORTED

**Problem** — both models answered a scenario correctly and both scored
`insufficient_evidence`, while the tool they had called had returned exactly the
measurement that settles it.
**Detection** — the evaluation baseline, then confirmed systematic across 45
recorded runs.
**Root cause** — the contract recognised figures and statuses. The answer asserts
a *relation* with neither.
**Fix** — the same predicate and the same fact already driving the contradiction
rule, in the other direction.
**Regression** — replayed over 907 runs three times, because the first two drafts
called correct answers contradicted. Final: 45 improvements, 0 regressions.

### Readiness that trusted a reachable endpoint

**Problem** — readiness reported healthy when the endpoint was up but did not
serve the configured model.
**Fix** — three-valued model verification: ready, not ready, unknown.

### A fallback that reset the deadline

**Problem** — a 2s budget produced a 4.01s run, because the deadline was computed
per provider call rather than per investigation.
**Fix** — one deadline per investigation, shared. Exhausted budget skips the
fallback and says so.
**Regression** — 38 tests, including a boundary case where the first repair raced
and an `int()` truncation that fired a second early.

### A nondeterministic evaluation fixture

**Problem** — a scenario failed because the pod it was told about was deleted
mid-investigation.
**Root cause** — a CronJob firing every minute keeping two failures: ~2 minutes
of pod life against a 72-second median investigation.
**Fix** — in the fixture, not the agent and not the expectation.

**Lesson:** a corpus whose ground truth evaporates is not measuring the model.

## Adversarial validation

A dedicated phase attacked the system rather than testing it: injection through
pod logs, image references and annotations; egress bypass; RBAC escalation;
grounding under deliberately misleading questions. Nine findings, all resolved.

Two findings were about the *tests* rather than the product, and are the ones I
would highlight:

- **An injection case passed 3/3 for weeks while its payload never reached the
  model.** Eval cases now declare `payload` and the run fails if it did not
  arrive.
- **RBAC probes lied three ways** — `kubectl --token` merges the admin cert so
  everything reads as allowed; an unquoted shell variable mangled the command so
  everything read as denied; `auth can-i` rejects `--request-timeout`. The fix was
  to stop asking and attempt the operations.

## AI evaluation

29 scenarios with declared ground truth, run 5 times against two configurations:
145 runs each. qwen3 127/145, gpt-4o-mini 132/145. **Paired at scenario level the
comparison is UNDETERMINED** (p = 0.3438, 19 of 29 identical).

The behavioural finding is more useful than the pass rate: gpt-4o-mini collects
more evidence yet refuses more often and makes four times fewer unsupported
claims; qwen3 is more willing to conclude and sometimes wrong. Latency differs
~12× in this environment.

See [AI_EVALUATION.md](AI_EVALUATION.md).

## GKE validation

The released chart on a real GKE cluster: RBAC validated at runtime by attempting
operations rather than asking `can-i`; NetworkPolicy enforced by Calico in the
dataplane; a live exec-credential token expiry exercised.

## UI

An operator console rather than a chatbot. The investigation is the primary
object: verdict, root cause, contradictions first, then Observed / Inferred /
Unknown with per-claim citations, the timeline of calls with the arguments
actually executed, and the raw evidence.

**The view computes no verdict.** A second implementation of the checker in the
view is how a console comes to disagree with its own backend.

Three defects came out of driving it by hand that no test had caught, including
a page that blanked on every contradiction because nothing had ever rendered one.

## Results

- 977 automated tests
- 907-run grounding replay, no regressions
- 290 live evaluation runs across two inference configurations
- Read-only RBAC, redaction, egress policy and NetworkPolicy validated at runtime
- Investigation context integrity proven end to end

## Limitations

One cluster, one machine, one prompt configuration. n=5 per scenario. Real vLLM
and EKS not tested. No browser paint automation. **Generalized diagnostic
accuracy is not established and is not claimed.**

## What I would build next

In order of value, and none of it implemented:

1. **A browser E2E suite** — designed in [E2E.md](E2E.md), including the harness
   decision that makes it possible (a scripted inference server, so verdicts that
   never occur in a demo can be rendered on demand).
2. **A larger evaluation corpus**, and n high enough to rank configurations.
3. **Real vLLM validation** — the protocol path exists and has never met a real
   server.
4. **Application authentication** for the console.
5. **Asynchronous investigation API** that survives a restart.

Explicitly *not* next: autonomous remediation. Every tool is read-only, and the
grounding work is what would have to be trusted before a write path could be.
