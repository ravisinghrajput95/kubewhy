# Security

## Reporting a vulnerability

Open a [private security advisory](https://github.com/ravisinghrajput95/kubewhy/security/advisories/new).
Please don't file a public issue for anything exploitable.

## What this tool touches

It reads. It never writes — no tool scales, restarts, patches or deletes
anything, and `deploy/rbac.yaml` enforces that at the API server rather than
relying on the code being well behaved.

What it *reads* is sensitive:

| Source | Contains |
| --- | --- |
| Pod logs | Credentials, tokens, connection strings, user data |
| Pod specs | Env var names, image registries, resource limits |
| Events | Container arguments, scheduling detail |
| Host process table | Usernames, command lines |

## Running it safely

**Use the supplied RBAC, not your admin kubeconfig.**

```bash
kubectl apply -f deploy/rbac.yaml
kubectl create token triage-agent -n triage --duration=8h
```

That ClusterRole grants `get`/`list` on pods, nodes, services, events,
deployments and endpointslices, plus `get` on `pods/log`. Verified:

```
get pods            -> yes      delete pods        -> no
get pods/log        -> yes      create pods        -> no
list endpointslices -> yes      patch deployments  -> no
list deployments    -> yes      get secrets        -> no
```

**Authenticate the API.** Every endpoint exposes cluster or host state. Set
`TRIAGE_API_TOKEN` and the API requires `Authorization: Bearer <token>`;
leave it unset and the API is open, which is only acceptable on loopback.
Compose publishes to `127.0.0.1` for this reason — if you change that
binding, set a token first.

**Check your context before asking.** The agent uses whatever
`kubectl config current-context` points at.

**`--scan` reads every namespace.** The ClusterRole always permitted this, but
until now nothing exercised it in one command: a scan lists pods across the
whole cluster, including namespaces you did not have in mind.

It never reads logs — those need a second, pod-specific call. It does fetch
full pod objects from the API server, specs included, because no field
selector can identify a failing pod server-side; what it *returns* is only
workload names, statuses and counts, and nothing else is retained or printed.
So a scan does not surface the material in the table above, but it does
transfer pod specs over your network.

On a shared or multi-tenant cluster, scope the credential with a namespaced
Role instead of the ClusterRole. Verified: the scan then fails closed with
`kubernetes API error 403` while the namespace-scoped tools keep working.

## Why Secrets are never listed

The ClusterRole grants no verb on `secrets`, and that is deliberate rather
than an oversight to be tidied up later.

The tempting exception is existence checking: `scan_references` would like to
say that a pod names a Secret that was never created, and the API server can
return `PartialObjectMetadata` — names without contents. It is a real feature
and it does what it says.

**It is not a security boundary.** Content negotiation happens *after*
authorization, so RBAC cannot distinguish a metadata-only request from a full
one, and `list` cannot be restricted by `resourceNames` the way `get` can.
Granting `list secrets` to read names grants every Secret's contents to
anything holding that token.

Measured on a live cluster rather than argued from the documentation. A
ServiceAccount was granted exactly `list` on `secrets`, and one token was used
twice:

```
Accept: …as=PartialObjectMetadataList…   -> PartialObjectMetadataList, names only
(no Accept header)                       -> SecretList, password = <plaintext>
```

The feature is doing its job in the first line. The grant is doing its job in
the second, and the second is the one that matters.

It buys nothing anyway. A pod referencing an absent Secret is already
diagnosable without any Secrets permission: the kubelet cannot build the
container and puts the name in the pod's own status
(`CreateContainerConfigError`, `secret "x" not found`) or in a `FailedMount`
event. The only reference that stays invisible is one marked `optional: true`,
where absence is what the author asked for.

## Secret redaction, and its limits

`redaction.py` strips recognisable secrets from pod logs and event messages
before they reach the model or your terminal: AWS keys, GitHub and Slack
tokens, JWTs, private key blocks, passwords in URLs, `KEY=value` pairs named
like credentials, and bearer headers.

**This is best-effort pattern matching and will miss things.** A secret in an
unrecognised format passes through. It reduces exposure; it does not
eliminate it. If your logs contain material that must never be read by a
model or written to a terminal, drop the `pods/log` rule from the ClusterRole
and diagnose without logs.

## Known limitations

- **No rate limiting.** A caller with a valid token can drive unlimited
  inference and API reads.
- **No audit log of questions asked**, only structured logs of tool calls.
- **Model context is not encrypted at rest** if Ollama swaps to disk.
- **Dependencies are pinned to minor versions**, not hashes. There is no
  lockfile and no SBOM.
