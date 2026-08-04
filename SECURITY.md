# Security

## Reporting a vulnerability

Open a [private security advisory](https://github.com/ravisinghrajput95/local-triage-agent/security/advisories/new).
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
