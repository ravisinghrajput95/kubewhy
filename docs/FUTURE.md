# kubewhy — future ideas

**Nothing in this document is implemented.** It exists so that ideas do not leak
into the README as capability. Everything here is a "could", not a "does".

If you are evaluating what kubewhy actually does today, read
[VALIDATION.md](VALIDATION.md) instead — that is the evidence document, and it is
deliberately conservative.

## Testing

**Browser E2E suite.** Designed in [E2E.md](E2E.md), including the harness
decision that makes it feasible: a scripted OpenAI-protocol server on loopback,
so verdicts that never occur in a demo — `contradicted`, `deadline_exceeded`,
zero tool calls — can be rendered on demand and asserted. The design also says
which cases *should not* be browser cases, which is the more useful half.

**Mutation testing in the repository.** A harness existed during development and
killed 28 guards. It was never committed, so it is not reproducible and is listed
as NOT TESTED.

**A larger evaluation corpus**, and n high enough to rank configurations rather
than report UNDETERMINED. The current design cannot reach significance after
multiplicity correction; that is a property of 29 scenarios at n=5, not of the
models.

## Platform coverage

**Real vLLM validation.** The `vllm` provider is the OpenAI wire protocol under
another name and has never met a real vLLM server. Everything about that path is
protocol-level.

**EKS.** Auth is verified by reading the client rather than by running against a
cluster.

**Managed NetworkPolicy dataplanes beyond Calico on GKE.**

## Product

**Per-user authorization.** The console now authenticates
(`ui.auth.enabled=true`, see [SECURITY.md](SECURITY.md)); it does not
authorize. Everyone who signs in sees everything the ServiceAccount can read.
That is a decision rather than an unfinished feature — kubewhy targets one SRE
team against one cluster, and per-user authorization would mean Kubernetes
impersonation, which needs the `impersonate` verb and turns a least-privilege
reader into a credential broker. If it is ever wanted, one release per team
with its own ServiceAccount is the shape to reach for.

**An asynchronous investigation API that survives a restart.** `/ask/jobs`
detaches the work but the result lives in this process's store — one replica or
nothing.

**Audit logging** of questions asked and evidence collected.

**Rate limiting.**

## Explicitly not planned

**Autonomous remediation.** Every tool is read-only, and that is a design
property rather than an unfinished feature. The grounding work is what would have
to be trusted before a write path could be, and the current evidence does not
support that trust: at n=5, one configuration still produced 42 unsupported
claims and 9 contradicted ones. A system that acts on those is worse than no
system.

**FinOps, cost analysis, capacity planning.** Different problem, different
evidence, no overlap with what the tools collect.

**More LLM providers.** The abstraction exists; adding providers without a
reason to is surface area with no benefit.
