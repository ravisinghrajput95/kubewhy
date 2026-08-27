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

**A larger evaluation corpus**, and n high enough to rank configurations rather
than report UNDETERMINED. The current design cannot reach significance after
multiplicity correction; that is a property of 29 scenarios at n=5, not of the
models.

**A fault list chosen from outside this project.** The corpus's real weakness
is not its ground truth — `AI_EVALUATION.md` takes that from what the API
server actually emitted, not from the manifests' intent. It is **selection**:
the same person chose the system's capabilities and chose which 29 faults to
measure it on, and nothing in the corpus can escape that from inside.

The obvious fix is a corpus built from real incident history with independently
recorded causes. That is the right answer and it needs data this project does
not have.

A cheaper approximation, written down here so it is not mistaken for that:

1. Take the fault list from a source outside this project — Kubernetes' own
   documented pod/container failure reasons, the `kubectl` waiting and
   terminated reason sets, published postmortems. The point is that something
   other than this project decides which faults exist.
2. Build a fixture per fault **before** running kubewhy against any of them,
   so the list cannot be trimmed to what it handles.
3. Take ground truth from the cluster's own output, as the current corpus
   already does.
4. Run it once and publish the failures, including faults kubewhy has no tool
   to see.

**What that buys, and what it does not.** It removes the author's choice of
*which faults exist*, which is the largest part of the bias. It does not remove
the author from the fixtures, the expectations or the harness, and a fault
reproduced on kind is not an incident. It would be evidence that the system was
measured against a list it did not pick — not evidence that it works in
production. Anything built this way must say so in VALIDATION.md, or it becomes
the thing it was meant to replace.

**An importer for real incidents** is the other half and is worth building
first if the data exists: a documented case schema plus a validator, so an
incident with a recorded cause becomes a runnable scenario. It also makes the
ask precise — an incident is only usable if its cause was established
independently of kubewhy.

## Platform coverage

**Real vLLM validation.** The `vllm` provider is the OpenAI wire protocol under
another name. It has now been run against a real OpenAI-protocol server —
tool calls, tool results, grounding and token usage all round-tripped — but
never against vLLM itself. What that leaves untested is vLLM's own
`--tool-call-parser`, which is per-model and is the one part no other server
can stand in for. See [VALIDATION.md](VALIDATION.md).

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
