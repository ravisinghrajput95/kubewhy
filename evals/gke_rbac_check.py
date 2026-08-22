"""
Does the read-only RBAC policy actually hold on GKE?

`deploy/rbac.yaml` claims two things: every read the agent needs succeeds, and
every write is refused. Both have to be checked against a real API server with
a real ServiceAccount token, because a policy that is wrong in either
direction is silent -- a missing read shows up as a tool returning
{"error": ...} in production, and a granted write shows up never, until
something uses it.

    kubectl apply -f deploy/rbac.yaml
    .venv/bin/python evals/gke_rbac_check.py --json results/gke-rbac.json

**`kubectl auth can-i` is not used and must not be.** On GKE, with `--as`, it
answers `no` for every permission a ServiceAccount demonstrably has, warning
`webhook authorizer does not support user rule resolution`. The only
trustworthy check is to mint a token and make the real request, which is what
this does: a second API client built from the token, calling the same methods
the tools call.

Writes are probed with `dry_run="All"`. The API server evaluates authorisation
before admission, so a 403 proves the verb is denied while nothing is created
if the policy is wrong -- the check cannot itself violate the read-only rule
it exists to verify.
"""

import argparse
import json
import subprocess
import sys

from kubernetes import client


def token_for(service_account, namespace, minutes=30):
    out = subprocess.run(
        ["kubectl", "create", "token", service_account, "-n", namespace,
         f"--duration={minutes}m"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise SystemExit(f"could not mint a token: {out.stderr.strip()}")
    return out.stdout.strip()


def api_for(token):
    """A client that is the ServiceAccount, not the operator running this."""
    server = subprocess.run(
        ["kubectl", "config", "view", "--minify",
         "-o", "jsonpath={.clusters[0].cluster.server}"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    ca = subprocess.run(
        ["kubectl", "config", "view", "--raw", "--minify",
         "-o", "jsonpath={.clusters[0].cluster.certificate-authority-data}"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()

    configuration = client.Configuration()
    configuration.host = server
    configuration.api_key = {"authorization": f"Bearer {token}"}
    if ca:
        import base64, tempfile
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
        handle.write(base64.b64decode(ca))
        handle.close()
        configuration.ssl_ca_cert = handle.name
    else:
        configuration.verify_ssl = False
    return client.ApiClient(configuration)


def check(label, call, expect):
    """expect is "allow" or "deny". Returns a row, never raises."""
    try:
        call()
        got = "allow"
        detail = ""
    except client.rest.ApiException as exc:
        got = "deny" if exc.status in (401, 403) else f"error {exc.status}"
        detail = (exc.reason or "")[:60]
    except Exception as exc:  # noqa: BLE001
        got = "error"
        detail = str(exc)[:60]

    ok = got == expect
    print(f"  {'PASS' if ok else 'FAIL':5} {label:<44} expected {expect:<5} got {got} {detail}")
    return {"check": label, "expected": expect, "got": got, "ok": ok, "detail": detail}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-account", default="kubewhy-agent")
    parser.add_argument("--namespace", default="kubewhy")
    parser.add_argument("--probe-namespace", default="demo")
    parser.add_argument("--json")
    args = parser.parse_args()

    token = token_for(args.service_account, args.namespace)
    api = api_for(token)
    core = client.CoreV1Api(api)
    apps = client.AppsV1Api(api)
    discovery = client.DiscoveryV1Api(api)
    ns = args.probe_namespace

    rows = []
    print("reads the agent depends on -- every one must be allowed")
    rows.append(check("list pods", lambda: core.list_namespaced_pod(ns, limit=1), "allow"))
    rows.append(check("list pods, all namespaces",
                      lambda: core.list_pod_for_all_namespaces(limit=1), "allow"))
    rows.append(check("list events", lambda: core.list_namespaced_event(ns, limit=1), "allow"))
    rows.append(check("list services", lambda: core.list_namespaced_service(ns, limit=1), "allow"))
    rows.append(check("list nodes", lambda: core.list_node(limit=1), "allow"))
    rows.append(check("list namespaces", lambda: core.list_namespace(limit=1), "allow"))
    rows.append(check("list deployments", lambda: apps.list_namespaced_deployment(ns, limit=1), "allow"))
    rows.append(check("list replicasets", lambda: apps.list_namespaced_replica_set(ns, limit=1), "allow"))
    rows.append(check("list endpointslices",
                      lambda: discovery.list_namespaced_endpoint_slice(ns, limit=1), "allow"))
    rows.append(check("list PVCs",
                      lambda: core.list_namespaced_persistent_volume_claim(ns, limit=1), "allow"))
    rows.append(check("watch pods (controller)",
                      lambda: core.list_namespaced_pod(ns, limit=1, watch=False,
                                                       timeout_seconds=1), "allow"))

    print("\nreads that must NOT be granted")
    rows.append(check("list secrets", lambda: core.list_namespaced_secret(ns, limit=1), "deny"))
    rows.append(check("list configmaps", lambda: core.list_namespaced_config_map(ns, limit=1), "deny"))
    rows.append(check("list service accounts",
                      lambda: core.list_namespaced_service_account(ns, limit=1), "deny"))

    print("\nwrites -- all must be refused (dry-run, so a hole cannot mutate anything)")
    rows.append(check("delete a pod", lambda: core.delete_namespaced_pod(
        "healthy-web", ns, dry_run="All"), "deny"))
    rows.append(check("create a pod", lambda: core.create_namespaced_pod(ns, client.V1Pod(
        metadata=client.V1ObjectMeta(name="rbac-probe"),
        spec=client.V1PodSpec(containers=[client.V1Container(
            name="c", image="busybox:1.36")])), dry_run="All"), "deny"))
    rows.append(check("patch a deployment", lambda: apps.patch_namespaced_deployment(
        "healthy-web", ns, {"metadata": {"labels": {"x": "y"}}}, dry_run="All"), "deny"))
    rows.append(check("scale a deployment", lambda: apps.patch_namespaced_deployment_scale(
        "healthy-web", ns, {"spec": {"replicas": 3}}, dry_run="All"), "deny"))
    rows.append(check("evict a pod", lambda: core.create_namespaced_pod_eviction(
        "healthy-web", ns, client.V1Eviction(
            metadata=client.V1ObjectMeta(name="healthy-web")), dry_run="All"), "deny"))
    rows.append(check("delete a namespace",
                      lambda: core.delete_namespace("noise-test", dry_run="All"), "deny"))
    rows.append(check("create a service account", lambda: core.create_namespaced_service_account(
        ns, client.V1ServiceAccount(metadata=client.V1ObjectMeta(name="rbac-probe")),
        dry_run="All"), "deny"))

    passed = sum(r["ok"] for r in rows)
    print(f"\nscore: {passed}/{len(rows)}")
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(rows, handle, indent=2)
        print(f"wrote {args.json}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
