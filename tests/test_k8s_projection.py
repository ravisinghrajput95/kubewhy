"""
Tests for the Kubernetes projections.

The point of these is not that the API is called, but that the projection
stays small and keeps the fields a diagnosis actually depends on. A projection
that silently starts returning raw objects would blow the model's context
window, so size is asserted explicitly.
"""

import datetime as dt
import json
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from routers import k8s_pods_info as k8s
from conftest import container_status, make_pod


@pytest.fixture
def api():
    """Patch the lazily-built CoreV1Api with a mock."""
    mock = MagicMock()
    with patch.object(k8s, "_api", return_value=mock):
        yield mock


@pytest.fixture
def apps_api():
    mock = MagicMock()
    with patch.object(k8s, "_apps_api", return_value=mock):
        yield mock


@pytest.fixture
def discovery_api():
    mock = MagicMock()
    with patch.object(k8s, "_discovery_api", return_value=mock):
        yield mock


def endpoint_slice(ready_ips=(), not_ready_ips=()):
    """Build an EndpointSlice with ready and not-ready addresses."""
    endpoints = [
        client.V1Endpoint(
            addresses=[ip], conditions=client.V1EndpointConditions(ready=True)
        )
        for ip in ready_ips
    ] + [
        client.V1Endpoint(
            addresses=[ip], conditions=client.V1EndpointConditions(ready=False)
        )
        for ip in not_ready_ips
    ]
    return client.V1EndpointSlice(
        address_type="IPv4",
        metadata=client.V1ObjectMeta(name="web-abc"),
        endpoints=endpoints,
    )


class TestPodStatus:
    """_pod_status has to match what kubectl shows, not the raw phase."""

    def test_waiting_reason_beats_phase(self):
        # A CrashLoopBackOff pod reports phase "Running"; reporting that would
        # tell the user everything is fine.
        pod = make_pod(
            phase="Running",
            statuses=[container_status(ready=False, waiting_reason="CrashLoopBackOff")],
        )
        assert k8s._pod_status(pod) == "CrashLoopBackOff"

    def test_terminated_reason_used_when_not_waiting(self):
        pod = make_pod(
            phase="Running",
            statuses=[container_status(ready=False, terminated_reason="OOMKilled")],
        )
        assert k8s._pod_status(pod) == "OOMKilled"

    def test_falls_back_to_phase(self):
        pod = make_pod(phase="Pending", statuses=[])
        assert k8s._pod_status(pod) == "Pending"

    def test_handles_pod_with_no_container_statuses(self):
        pod = make_pod(phase="Pending", statuses=None)
        pod.status.container_statuses = None
        assert k8s._pod_status(pod) == "Pending"


def crashing(name, workload, namespace, reason="CrashLoopBackOff"):
    return make_pod(
        name=name,
        namespace=namespace,
        owner=workload,
        statuses=[container_status(ready=False, waiting_reason=reason)],
    )


def owned_by(kind, owner, name, namespace="kube-system"):
    pod = make_pod(name=name, namespace=namespace)
    pod.metadata.owner_references = [
        client.V1OwnerReference(
            api_version="v1", kind=kind, name=owner, uid=f"uid-{owner}", controller=True
        )
    ]
    return pod


class TestClusterNativeWorkloads:
    """
    The demo cluster is five Deployments, so every assumption that only holds
    for Deployments passed unnoticed. A real cluster is mostly not that: the
    control plane runs as static pods, networking and logging as DaemonSets,
    backups as CronJobs.
    """

    def test_static_pods_group_by_component_not_by_node(self):
        # kube-apiserver and etcd are owned by the Node they run on. Returning
        # the owner name files every control plane component on a node under
        # one entry named after the node.
        api_server = owned_by(
            "Node", "ip-10-0-1-5", "kube-apiserver-ip-10-0-1-5"
        )
        etcd = owned_by("Node", "ip-10-0-1-5", "etcd-ip-10-0-1-5")

        assert k8s.workload_of(api_server) == "kube-apiserver"
        assert k8s.workload_of(etcd) == "etcd"

    def test_cronjob_runs_collapse_to_the_cronjob(self):
        """
        Jobs made by a CronJob are "<name>-<timestamp>". Keeping the timestamp
        makes every scheduled run a new workload, so the cooldown never applies
        and an hourly failure reports hourly forever.
        """
        first = owned_by("Job", "backup-28912345", "backup-28912345-abcde", "prod")
        second = owned_by("Job", "backup-28912405", "backup-28912405-fghij", "prod")

        assert k8s.workload_of(first) == "backup"
        assert k8s.workload_of(second) == "backup"

    def test_a_standalone_job_keeps_its_name(self):
        """Only a trailing timestamp is a CronJob; don't eat real names."""
        pod = owned_by("Job", "migrate-v2", "migrate-v2-abcde", "prod")

        assert k8s.workload_of(pod) == "migrate-v2"

    def test_daemonsets_and_statefulsets_are_named_directly(self):
        daemon = owned_by("DaemonSet", "kube-proxy", "kube-proxy-x7k2p")
        stateful = owned_by("StatefulSet", "postgres", "postgres-0", "prod")

        assert k8s.workload_of(daemon) == "kube-proxy"
        assert k8s.workload_of(stateful) == "postgres"

    def test_a_failing_init_container_is_the_reported_status(self):
        """
        "Wait for the database" crashlooping is one of the most common real
        failures, and it reports phase Pending with no app container status --
        so reading only container_statuses calls it "Pending".
        """
        pod = make_pod(name="api-abc123-xyz", namespace="prod", statuses=[])
        pod.status.container_statuses = None
        pod.status.init_container_statuses = [
            container_status(
                name="wait-for-db", ready=False, waiting_reason="CrashLoopBackOff"
            )
        ]

        assert k8s._pod_status(pod) == "Init:CrashLoopBackOff"
        # Same fault as any other crash: same grouping, same cooldown.
        assert k8s.fault_of("Init:CrashLoopBackOff") == "crash"
        assert k8s.base_status("Init:CrashLoopBackOff") == "CrashLoopBackOff"

    def test_a_completed_init_container_is_not_the_status(self):
        """An init container that finished is not the pod's problem."""
        pod = make_pod(name="api-abc123-xyz", namespace="prod")
        pod.status.init_container_statuses = [
            container_status(
                name="wait-for-db", terminated_reason="Completed", exit_code=0
            )
        ]

        assert k8s._pod_status(pod) == "Running"


class TestErrorsExplainThemselves:
    """
    Both found by looking at the UI against a real cluster.

    The API server explains itself well; the value is in not discarding what
    it said.
    """

    def test_api_error_keeps_the_servers_message(self, api):
        # "Bad Request" alone is useless. The body names the container and the
        # reason, which is the entire diagnosis.
        api.read_namespaced_pod.side_effect = ApiException(
            status=400, reason="Bad Request"
        )
        api.read_namespaced_pod.side_effect.body = json.dumps(
            {"message": 'container "app" in pod "x" is waiting to start: image can\'t be pulled'}
        )

        assert "waiting to start" in k8s.describe_pod("x", "demo")["error"]

    def test_a_container_that_never_started_is_not_an_error(self, api):
        """
        ImagePullBackOff means there are no logs and never will be. Reporting
        that as "kubernetes API error 400: Bad Request" told the reader nothing
        and pointed the model at a dead end.
        """
        failure = ApiException(status=400, reason="Bad Request")
        failure.body = json.dumps(
            {"message": 'container "app" in pod "x" is waiting to start: image can\'t be pulled'}
        )
        api.read_namespaced_pod_log.side_effect = failure

        result = k8s.get_pod_logs("x", "demo")

        assert "error" not in result
        assert "never started" in result["result"]
        # Names where to look instead.
        assert "describe_pod" in result["result"]


class TestScanAtScale:
    """
    A thousand workloads is the target, not five Deployments. The projection
    already scales; fetching and narrowing are what did not.
    """

    def test_pods_are_fetched_in_pages(self, api):
        """
        One unbounded request for a huge cluster is a multi-megabyte response
        that has to arrive inside K8S_TIMEOUT. Paging bounds each request.
        """
        first = client.V1PodList(
            items=[crashing("a-abc123-1", "a-abc123", "prod")],
            metadata=client.V1ListMeta(_continue="token-1"),
        )
        second = client.V1PodList(
            items=[crashing("b-abc123-1", "b-abc123", "prod")],
            metadata=client.V1ListMeta(_continue=None),
        )
        api.list_pod_for_all_namespaces.side_effect = [first, second]

        result = k8s.scan_cluster()

        assert set(result) == {"prod/a", "prod/b"}
        # Second call must carry the continue token, or it loops on page one.
        assert api.list_pod_for_all_namespaces.call_args_list[1].kwargs["_continue"] == "token-1"

    def test_a_single_namespace_uses_a_namespaced_query(self, api):
        """Cheaper than listing the cluster and discarding almost all of it."""
        api.list_namespaced_pod.return_value = client.V1PodList(
            items=[crashing("web-abc123-1", "web-abc123", "prod")],
            metadata=client.V1ListMeta(_continue=None),
        )

        assert list(k8s.scan_cluster(namespaces="prod")) == ["prod/web"]
        api.list_pod_for_all_namespaces.assert_not_called()
        assert api.list_namespaced_pod.call_args.args[0] == "prod"

    def test_several_namespaces_are_filtered(self, api):
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[
                crashing("web-abc123-1", "web-abc123", "prod"),
                crashing("web-abc123-1", "web-abc123", "staging"),
                crashing("web-abc123-1", "web-abc123", "sandbox"),
            ],
            metadata=client.V1ListMeta(_continue=None),
        )

        assert set(k8s.scan_cluster(namespaces="prod,staging")) == {
            "prod/web",
            "staging/web",
        }


class TestAskingAboutOneWorkload:
    """
    The bug this closes: asked about a healthy CronJob, the agent answered
    with a different workload's problem, confidently and grounded. It had no
    way to say "that one is fine" -- the scan returned only failures, so a
    healthy workload simply was not there.
    """

    def test_a_healthy_workload_is_reported_not_omitted(self, api, healthy_pod):
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[healthy_pod, crashing("web-abc123-1", "web-abc123", "prod")],
            metadata=client.V1ListMeta(_continue=None),
        )

        result = k8s.scan_cluster(workload="healthy")

        assert list(result) == ["demo/healthy"]
        assert result["demo/healthy"]["status"] == "Running"

    def test_only_the_named_workload_comes_back(self, api, healthy_pod):
        """Never hand back a different workload's problem."""
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[healthy_pod, crashing("web-abc123-1", "web-abc123", "prod")],
            metadata=client.V1ListMeta(_continue=None),
        )

        assert "prod/web" not in k8s.scan_cluster(workload="healthy")

    def test_a_name_that_does_not_exist_says_so(self, api, healthy_pod):
        """Distinct from "it is healthy", which returns a row."""
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[healthy_pod], metadata=client.V1ListMeta(_continue=None)
        )

        assert "no workload named" in k8s.scan_cluster(workload="ghost")["result"]

    def test_a_namespaced_name_also_matches(self, api):
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[crashing("web-abc123-1", "web-abc123", "prod")],
            metadata=client.V1ListMeta(_continue=None),
        )

        assert list(k8s.scan_cluster(workload="prod/web")) == ["prod/web"]


class TestMultiContainerLogs:
    """
    Sidecars are the norm at scale -- a mesh proxy, a log shipper, a metrics
    agent. The API refuses to guess and returns 400 listing the names, so
    before this the log tool failed on most pods of a real cluster.
    """

    def test_picks_the_failing_container_not_the_first(self, api):
        refusal = ApiException(status=400, reason="Bad Request")
        refusal.body = json.dumps(
            {
                "message": "a container name must be specified for pod x, "
                "choose one of: [istio-proxy app]"
            }
        )

        pod = make_pod(name="x")
        pod.status.container_statuses = [
            container_status(name="istio-proxy", ready=True),
            container_status(name="app", ready=False, terminated_reason="Error"),
        ]
        api.read_namespaced_pod.return_value = pod

        body = MagicMock()
        body.data = b"APP: connection refused to db:5432"
        api.read_namespaced_pod_log.side_effect = [refusal, body]

        result = k8s.get_pod_logs("x", "demo")

        # The proxy is healthy and first; the application is what crashed.
        assert result["container"] == "app"
        assert "connection refused" in result["logs"]

    def test_an_explicit_container_is_respected(self, api):
        body = MagicMock()
        body.data = b"PROXY: ready"
        api.read_namespaced_pod_log.return_value = body

        result = k8s.get_pod_logs("x", "demo", container="istio-proxy")

        assert result["container"] == "istio-proxy"
        assert api.read_namespaced_pod_log.call_args.kwargs["container"] == "istio-proxy"

    def test_single_container_pods_need_no_extra_read(self, api):
        """The common case must not pay for a pod read it does not need."""
        body = MagicMock()
        body.data = b"hello"
        api.read_namespaced_pod_log.return_value = body

        result = k8s.get_pod_logs("x", "demo")

        assert "container" not in result
        api.read_namespaced_pod.assert_not_called()


class TestEventAge:
    def test_events_carry_an_age(self, api):
        """
        Events are history. A FailedScheduling warning from before a pod was
        scheduled stays in its list forever, so without an age a resolved
        problem reads as a current one -- seen on a Running pod whose only
        warning was 27 minutes old.
        """
        event = client.CoreV1Event(
            metadata=client.V1ObjectMeta(name="e1"),
            involved_object=client.V1ObjectReference(name="web"),
            type="Warning",
            reason="FailedScheduling",
            count=1,
            message="0/1 nodes are available",
            last_timestamp=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=27),
        )
        api.list_namespaced_event.return_value = client.CoreV1EventList(items=[event])

        assert k8s.get_pod_events("web", "demo")["events"][0]["age"] == "27m"


class TestActiveContext:
    """
    Which cluster a surface says it is reading.

    Found live rather than reasoned about: the browser UI labelled itself
    kind-loglens-cri while showing pods that exist only in kind-triage-demo,
    because a second kind cluster was created in another shell -- which
    rewrites current-context -- after the process had already built its client.
    """

    def test_does_not_follow_a_later_kubeconfig_change(self):
        with patch.object(k8s, "_active_context", "kind-triage-demo"), patch.object(
            k8s.config,
            "list_kube_config_contexts",
            return_value=([], {"name": "kind-something-else"}),
        ):
            assert k8s.active_context() == "kind-triage-demo"

    def test_binds_lazily_when_nothing_is_loaded_yet(self):
        def load():
            k8s._active_context = "kind-triage-demo"

        with patch.object(k8s, "_active_context", None), patch.object(
            k8s, "_api", side_effect=load
        ):
            assert k8s.active_context() == "kind-triage-demo"

    def test_a_broken_kubeconfig_is_reported_not_raised(self):
        """
        `kind delete cluster` strips current-context from the kubeconfig, and
        the browser UI asks for the context before any tool runs -- so raising
        here replaced the entire page with a traceback.
        """
        with patch.object(k8s, "_active_context", None), patch.object(
            k8s, "_api", side_effect=Exception("Invalid kube-config file")
        ):
            assert k8s.active_context() == "unavailable"

    def test_switching_context_drops_the_cached_clients(self):
        """Otherwise the label moves and the connection does not."""
        with patch.object(k8s, "_core_v1", "stale"), patch.object(
            k8s, "_apps_v1", "stale"
        ), patch.object(k8s, "_discovery_v1", "stale"), patch.object(
            k8s, "_requested_context", None
        ), patch.object(k8s, "_active_context", "old"):
            k8s.use_context("kind-other")

            assert k8s._core_v1 is None
            assert k8s._apps_v1 is None
            assert k8s._discovery_v1 is None
            assert k8s._requested_context == "kind-other"

    def test_lists_contexts_and_survives_no_kubeconfig(self):
        with patch.object(
            k8s.config,
            "list_kube_config_contexts",
            return_value=([{"name": "a"}, {"name": "b"}], {"name": "a"}),
        ):
            assert k8s.list_contexts() == ["a", "b"]

        # In-cluster there is no kubeconfig; that is not an error.
        with patch.object(
            k8s.config, "list_kube_config_contexts", side_effect=Exception("no config")
        ):
            assert k8s.list_contexts() == []


class TestScanCluster:
    """
    Cluster-wide scan. The risk here is size: this is the one tool whose input
    grows with the whole cluster rather than one namespace, so grouping and
    truncation are the properties worth pinning down.
    """

    def test_groups_replicas_into_one_workload(self, api):
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[
                crashing(f"payments-api-6f79bc6fcb-{i}", "payments-api-6f79bc6fcb", "prod")
                for i in range(10)
            ]
        )
        result = k8s.scan_cluster()

        # Ten crashing replicas are one problem, not ten.
        assert list(result) == ["prod/payments-api"]
        assert result["prod/payments-api"]["pods"] == 10

    def test_stays_small_on_a_wide_failure(self, api):
        """Fifty broken pods must not cost fifty pods' worth of context."""
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[
                crashing(f"web-{w}-abc123-{i}", f"web-{w}-abc123", f"ns-{w}")
                for w in range(5)
                for i in range(10)
            ]
        )
        tokens = len(json.dumps(k8s.scan_cluster())) // 4
        assert tokens < 150, f"projection grew to {tokens} tokens"

    def test_names_an_example_pod_to_drill_into(self, api):
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[crashing("web-abc123-xyz", "web-abc123", "prod")]
        )
        entry = k8s.scan_cluster()["prod/web"]

        # Without a real pod name the model cannot follow up with describe_pod,
        # which makes the whole scan a dead end.
        assert entry["example"] == "web-abc123-xyz"
        assert entry["status"] == "CrashLoopBackOff"
        assert entry["fault"] == "crash"

    def test_excludes_healthy_workloads_by_default(self, api, healthy_pod):
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[healthy_pod, crashing("web-abc123-xyz", "web-abc123", "prod")]
        )
        assert list(k8s.scan_cluster()) == ["prod/web"]

    def test_includes_healthy_when_asked(self, api, healthy_pod):
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[healthy_pod]
        )
        result = k8s.scan_cluster(only_unhealthy=False)

        assert result["demo/healthy"]["status"] == "Running"
        # No fault class for a healthy pod: it would just repeat the status.
        assert "fault" not in result["demo/healthy"]

    def test_one_fault_not_two_across_a_transition(self, api):
        """
        A rollout pulling a bad image has replicas in ErrImagePull and
        ImagePullBackOff at the same instant. That is one problem.
        """
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[
                crashing("web-abc123-1", "web-abc123", "prod", reason="ErrImagePull"),
                crashing("web-abc123-2", "web-abc123", "prod", reason="ImagePullBackOff"),
            ]
        )
        result = k8s.scan_cluster()

        assert list(result) == ["prod/web"]
        assert result["prod/web"]["pods"] == 2
        assert result["prod/web"]["fault"] == "image-pull"

    def test_distinct_faults_on_one_workload_are_both_kept(self, api):
        """A crashing old ReplicaSet and an unpullable new one are two faults."""
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[
                crashing("web-abc123-1", "web-abc123", "prod"),
                crashing("web-def456-1", "web-def456", "prod", reason="ErrImagePull"),
            ]
        )
        result = k8s.scan_cluster()

        assert set(result) == {"prod/web:crash", "prod/web:image-pull"}

    def test_truncates_worst_first(self, api):
        items = [crashing("small-abc123-1", "small-abc123", "prod")]
        items += [
            crashing(f"big-abc123-{i}", "big-abc123", "prod") for i in range(5)
        ]
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(items=items)
        result = k8s.scan_cluster(limit=1)

        # Blast radius decides what survives the cut.
        assert "prod/big" in result
        assert "prod/small" not in result
        assert "1 more not shown" in result["_truncated"]

    def test_ignores_completed_job_pods(self, api):
        """
        A CronJob that ran successfully leaves Succeeded pods behind. They are
        not Running and have no ready containers, so a readiness-only check
        calls every one of them broken -- which on a real cluster means the
        scan is mostly finished Jobs. The demo cluster has none, so this only
        showed up against a cluster with real workloads on it.
        """
        done = make_pod(name="nightly-import-abc", namespace="prod", phase="Succeeded")
        done.status.container_statuses = [
            container_status(ready=False, terminated_reason="Completed", exit_code=0)
        ]
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[done, crashing("web-abc123-1", "web-abc123", "prod")]
        )

        assert list(k8s.scan_cluster()) == ["prod/web"]

    def test_a_job_that_failed_is_still_reported(self):
        """Succeeded is exempt; Failed is a real fault."""
        failed = make_pod(name="nightly-import-xyz", namespace="prod", phase="Failed")
        failed.status.container_statuses = [
            container_status(ready=False, terminated_reason="Error", exit_code=1)
        ]

        assert not k8s._is_healthy(failed)

    def test_ignores_terminating_pods(self, api):
        pod = crashing("web-abc123-xyz", "web-abc123", "prod")
        pod.metadata.deletion_timestamp = "2026-01-01T00:00:00Z"
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(items=[pod])

        assert "result" in k8s.scan_cluster()

    def test_reports_a_clean_cluster(self, api, healthy_pod):
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[healthy_pod]
        )
        assert k8s.scan_cluster() == {
            "result": "no unhealthy workloads in any namespace"
        }

    def test_returns_error_data_rather_than_raising(self, api):
        api.list_pod_for_all_namespaces.side_effect = ApiException(
            status=403, reason="Forbidden"
        )
        assert "error" in k8s.scan_cluster()

    def test_falls_back_to_pod_name_without_an_owner(self, api):
        """A bare pod has no workload; it must still be reported."""
        api.list_pod_for_all_namespaces.return_value = client.V1PodList(
            items=[
                make_pod(
                    name="standalone",
                    namespace="prod",
                    statuses=[
                        container_status(ready=False, waiting_reason="CrashLoopBackOff")
                    ],
                )
            ]
        )
        assert "prod/standalone" in k8s.scan_cluster()


class TestListPods:
    def test_projects_expected_fields_only(self, api, pod_list):
        api.list_namespaced_pod.return_value = pod_list
        result = k8s.list_pods("demo")

        assert set(result["healthy"]) == {"status", "ready", "restarts", "node"}

    def test_stays_small(self, api, pod_list):
        """The whole reason projection exists -- guard against regression."""
        api.list_namespaced_pod.return_value = pod_list
        tokens = len(json.dumps(k8s.list_pods("demo"))) // 4
        assert tokens < 100, f"projection grew to {tokens} tokens"

    def test_only_unhealthy_filters_running_pods(self, api, pod_list):
        api.list_namespaced_pod.return_value = pod_list
        result = k8s.list_pods("demo", only_unhealthy=True)

        assert "healthy" not in result
        assert "memory-hog" in result

    def test_reports_restart_count(self, api, pod_list):
        api.list_namespaced_pod.return_value = pod_list
        assert k8s.list_pods("demo")["memory-hog"]["restarts"] == 4

    def test_empty_namespace_is_not_an_error(self, api):
        api.list_namespaced_pod.return_value = client.V1PodList(items=[])
        result = k8s.list_pods("demo")

        assert "error" not in result
        assert "no matching pods" in result["result"]


class TestDescribePod:
    def test_surfaces_termination_reason_and_limits(self, api, oomkilled_pod):
        api.read_namespaced_pod.return_value = oomkilled_pod
        result = k8s.describe_pod("memory-hog", "demo")
        app = result["containers"]["app"]

        # Together these are the OOMKill diagnosis: killed for memory, against
        # a limit low enough to explain it.
        assert app["last_termination"] == {"reason": "OOMKilled", "exit_code": 137}
        assert app["limits"]["memory"] == "64Mi"

    def test_truncates_long_waiting_messages(self, api):
        pod = make_pod(
            statuses=[container_status(ready=False, waiting_reason="ImagePullBackOff")]
        )
        pod.status.container_statuses[0].state.waiting.message = "x" * 5000
        api.read_namespaced_pod.return_value = pod

        result = k8s.describe_pod("bad-image", "demo")
        assert len(result["containers"]["app"]["waiting_message"]) <= 300


class TestEvents:
    def test_returns_only_warnings_newest_first(self, api):
        import datetime as dt

        def event(name, kind, reason, when):
            return client.CoreV1Event(
                metadata=client.V1ObjectMeta(name=name),
                involved_object=client.V1ObjectReference(name="p"),
                type=kind,
                reason=reason,
                message="m",
                count=1,
                last_timestamp=when,
            )

        now = dt.datetime(2026, 1, 1, 12, 0)
        api.list_namespaced_event.return_value = client.CoreV1EventList(
            items=[
                event("a", "Normal", "Scheduled", now),
                event("b", "Warning", "Old", now - dt.timedelta(hours=1)),
                event("c", "Warning", "Recent", now),
            ]
        )

        result = k8s.get_pod_events("p", "demo")
        reasons = [e["reason"] for e in result["events"]]

        assert "Scheduled" not in reasons
        assert reasons == ["Recent", "Old"]


class TestLogs:
    def test_decodes_bytes_body(self, api):
        """The client returns a repr of bytes unless the raw body is read."""
        api.read_namespaced_pod_log.return_value.data = b"FATAL: db refused\n"
        result = k8s.get_pod_logs("crasher", "demo")

        assert result["logs"] == "FATAL: db refused"
        assert "b'" not in result["logs"]

    def test_falls_back_to_previous_container(self, api):
        # A container that just died has an empty current log; the useful
        # output belongs to the run that crashed.
        def read(*args, **kwargs):
            resp = MagicMock()
            resp.data = b"" if not kwargs.get("previous") else b"crash output"
            return resp

        api.read_namespaced_pod_log.side_effect = read
        result = k8s.get_pod_logs("crasher", "demo")

        assert result["logs"] == "crash output"
        assert "previous" in result["source"]

    def test_tail_is_capped(self, api):
        api.read_namespaced_pod_log.return_value.data = b"line"
        k8s.get_pod_logs("p", "demo", tail=10_000)

        assert api.read_namespaced_pod_log.call_args.kwargs["tail_lines"] == 100


class TestNodes:
    def test_reports_pressure_and_readiness(self, api):
        node = client.V1Node(
            metadata=client.V1ObjectMeta(name="node-1"),
            spec=client.V1NodeSpec(unschedulable=True),
            status=client.V1NodeStatus(
                conditions=[
                    client.V1NodeCondition(type="Ready", status="True"),
                    client.V1NodeCondition(type="MemoryPressure", status="True"),
                    client.V1NodeCondition(type="DiskPressure", status="False"),
                ],
                allocatable={"cpu": "4", "memory": "8Gi"},
            ),
        )
        api.list_node.return_value = client.V1NodeList(items=[node])

        result = k8s.list_nodes()["node-1"]
        assert result["ready"] is True
        assert result["pressure"] == ["MemoryPressure"]
        assert result["unschedulable"] is True

    def test_no_pressure_reported_as_none(self, api):
        node = client.V1Node(
            metadata=client.V1ObjectMeta(name="node-1"),
            spec=client.V1NodeSpec(),
            status=client.V1NodeStatus(
                conditions=[client.V1NodeCondition(type="Ready", status="True")],
                allocatable={},
            ),
        )
        api.list_node.return_value = client.V1NodeList(items=[node])
        assert k8s.list_nodes()["node-1"]["pressure"] is None


class TestDeployments:
    def _deployment(self, desired, ready):
        return client.V1Deployment(
            metadata=client.V1ObjectMeta(name="web"),
            spec=client.V1DeploymentSpec(
                replicas=desired,
                selector=client.V1LabelSelector(match_labels={"app": "web"}),
                template=client.V1PodTemplateSpec(
                    spec=client.V1PodSpec(
                        containers=[client.V1Container(name="web", image="nginx:1.27")]
                    )
                ),
            ),
            status=client.V1DeploymentStatus(
                ready_replicas=ready, available_replicas=ready
            ),
        )

    def test_flags_degraded_deployment(self, apps_api):
        apps_api.list_namespaced_deployment.return_value = client.V1DeploymentList(
            items=[self._deployment(desired=3, ready=1)]
        )
        result = k8s.list_deployments("demo")["web"]

        assert result["healthy"] is False
        assert (result["desired"], result["ready"]) == (3, 1)

    def test_healthy_when_counts_match(self, apps_api):
        apps_api.list_namespaced_deployment.return_value = client.V1DeploymentList(
            items=[self._deployment(desired=2, ready=2)]
        )
        assert k8s.list_deployments("demo")["web"]["healthy"] is True

    def test_missing_ready_replicas_is_zero_not_crash(self, apps_api):
        # A deployment that has never scheduled a pod omits ready_replicas.
        apps_api.list_namespaced_deployment.return_value = client.V1DeploymentList(
            items=[self._deployment(desired=1, ready=None)]
        )
        assert k8s.list_deployments("demo")["web"]["ready"] == 0


class TestServiceEndpoints:
    def _service(self):
        return client.V1Service(
            metadata=client.V1ObjectMeta(name="web"),
            spec=client.V1ServiceSpec(
                type="ClusterIP",
                selector={"app": "web"},
                ports=[client.V1ServicePort(port=80, target_port=8080)],
            ),
        )

    def test_distinguishes_unready_pods_from_no_match(self, api, discovery_api):
        """The two causes need different fixes, so they must read differently."""
        api.read_namespaced_service.return_value = self._service()
        discovery_api.list_namespaced_endpoint_slice.return_value = (
            client.V1EndpointSliceList(items=[endpoint_slice(not_ready_ips=["10.0.0.1"])])
        )

        result = k8s.get_service_endpoints("web", "demo")
        assert result["ready_endpoints"] == []
        assert "readiness probe" in result["diagnosis"]
        assert "1 pod(s) match" in result["diagnosis"]

    def test_selector_matching_nothing_says_so(self, api, discovery_api):
        api.read_namespaced_service.return_value = self._service()
        discovery_api.list_namespaced_endpoint_slice.return_value = (
            client.V1EndpointSliceList(items=[endpoint_slice()])
        )

        result = k8s.get_service_endpoints("web", "demo")
        assert "matches no pods at all" in result["diagnosis"]

    def test_healthy_service_has_no_diagnosis(self, api, discovery_api):
        api.read_namespaced_service.return_value = self._service()
        discovery_api.list_namespaced_endpoint_slice.return_value = (
            client.V1EndpointSliceList(items=[endpoint_slice(ready_ips=["10.0.0.1"])])
        )

        result = k8s.get_service_endpoints("web", "demo")
        assert result["ready_endpoints"] == ["10.0.0.1"]
        assert "diagnosis" not in result

    def test_no_endpointslice_at_all_is_handled(self, api, discovery_api):
        api.read_namespaced_service.return_value = self._service()
        discovery_api.list_namespaced_endpoint_slice.return_value = (
            client.V1EndpointSliceList(items=[])
        )

        result = k8s.get_service_endpoints("web", "demo")
        assert result["ready_endpoints"] == []
        assert "no EndpointSlice" in result["diagnosis"]

    def test_addresses_split_across_multiple_slices(self, api, discovery_api):
        """Large services are sharded across slices; all must be counted."""
        api.read_namespaced_service.return_value = self._service()
        discovery_api.list_namespaced_endpoint_slice.return_value = (
            client.V1EndpointSliceList(
                items=[
                    endpoint_slice(ready_ips=["10.0.0.1", "10.0.0.2"]),
                    endpoint_slice(ready_ips=["10.0.0.3"], not_ready_ips=["10.0.0.4"]),
                ]
            )
        )

        result = k8s.get_service_endpoints("web", "demo")
        assert len(result["ready_endpoints"]) == 3
        assert result["not_ready_endpoints"] == ["10.0.0.4"]

    def test_missing_ready_condition_counts_as_ready(self, api, discovery_api):
        # Per the EndpointSlice spec, an absent ready condition means ready.
        api.read_namespaced_service.return_value = self._service()
        slice_ = client.V1EndpointSlice(
            address_type="IPv4",
            metadata=client.V1ObjectMeta(name="web-abc"),
            endpoints=[client.V1Endpoint(addresses=["10.0.0.9"], conditions=None)],
        )
        discovery_api.list_namespaced_endpoint_slice.return_value = (
            client.V1EndpointSliceList(items=[slice_])
        )

        assert k8s.get_service_endpoints("web", "demo")["ready_endpoints"] == ["10.0.0.9"]


class TestErrorHandling:
    """Errors must come back as data, so the agent can report and continue."""

    def test_api_exception_becomes_error_dict(self, api):
        api.list_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")
        result = k8s.list_pods("demo")

        assert "403" in result["error"] and "Forbidden" in result["error"]

    def test_unreachable_cluster_becomes_error_dict(self, api):
        api.list_namespaced_pod.side_effect = ConnectionRefusedError("no route")
        result = k8s.list_pods("demo")

        assert "cluster unreachable" in result["error"]

    def test_errors_never_raise(self, api):
        api.read_namespaced_pod.side_effect = RuntimeError("boom")
        assert "error" in k8s.describe_pod("p", "demo")


class TestWorkloadPods:
    """
    Every pod of one workload. The property under test is that narrowing by the
    controller's label selector changed only how much is transferred, never
    which pods come back -- the owner reference stays the definition of
    membership.
    """

    def _deployment(self, match_labels=None, expressions=None):
        selector = client.V1LabelSelector(
            match_labels=match_labels, match_expressions=expressions
        )
        return client.V1Deployment(
            metadata=client.V1ObjectMeta(name="web", namespace="demo"),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=selector,
                template=client.V1PodTemplateSpec(
                    spec=client.V1PodSpec(
                        containers=[client.V1Container(name="web", image="nginx:1.27")]
                    )
                ),
            ),
        )

    def _pods(self, api, *pods):
        api.list_namespaced_pod.return_value = client.V1PodList(
            items=list(pods), metadata=client.V1ListMeta(_continue=None)
        )

    def _not_found(self, apps_api):
        for reader in (
            apps_api.read_namespaced_deployment,
            apps_api.read_namespaced_daemon_set,
            apps_api.read_namespaced_stateful_set,
        ):
            reader.side_effect = ApiException(status=404, reason="Not Found")

    def test_asks_the_api_server_to_filter(self, api, apps_api):
        apps_api.read_namespaced_deployment.return_value = self._deployment(
            match_labels={"app": "web", "tier": "front"}
        )
        self._pods(api, make_pod(name="web-abc-1", owner="web-abc"))

        k8s.workload_pods("demo", "web")

        # Sorted, so the selector string does not depend on dict ordering.
        assert (
            api.list_namespaced_pod.call_args.kwargs["label_selector"]
            == "app=web,tier=front"
        )

    def test_reads_the_namespace_when_nothing_owns_the_name(self, api, apps_api):
        # A CronJob's pods, a static pod and a bare pod all land here: there is
        # no single selector, so the old full read is still correct.
        self._not_found(apps_api)
        self._pods(api, make_pod(name="backup-29768545-x", owner=None))

        k8s.workload_pods("demo", "backup-29768545-x")

        assert api.list_namespaced_pod.call_args.kwargs.get("label_selector") is None

    def test_match_expressions_fall_back_rather_than_guess(self, api, apps_api):
        # Serialising set-based requirements wrongly would drop pods silently.
        apps_api.read_namespaced_deployment.return_value = self._deployment(
            match_labels={"app": "web"},
            expressions=[
                client.V1LabelSelectorRequirement(
                    key="tier", operator="In", values=["front"]
                )
            ],
        )
        self._pods(api, make_pod(name="web-abc-1", owner="web-abc"))

        k8s.workload_pods("demo", "web")

        assert api.list_namespaced_pod.call_args.kwargs.get("label_selector") is None

    def test_selector_does_not_widen_membership(self, api, apps_api):
        """
        A selector can match a pod this workload does not own -- a hand-made
        pod carrying the same labels, or two controllers sharing them. The
        owner reference decides, or narrowing would quietly redefine what a
        workload is.
        """
        apps_api.read_namespaced_deployment.return_value = self._deployment(
            match_labels={"app": "web"}
        )
        self._pods(
            api,
            make_pod(name="web-abc-1", owner="web-abc"),
            make_pod(name="impostor", owner="other-def"),
        )

        assert [p["pod"] for p in k8s.workload_pods("demo", "web")] == ["web-abc-1"]

    def test_permission_error_is_not_a_silent_full_read(self, api, apps_api):
        """
        403 on the controller read means RBAC is wrong, and degrading to a
        namespace-wide read would hide that as a slow page instead of an error.
        """
        apps_api.read_namespaced_deployment.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        result = k8s.workload_pods("demo", "web")

        assert "403" in result["error"]
        api.list_namespaced_pod.assert_not_called()

    def test_reports_each_pod_of_a_multi_container_workload(self, api, apps_api):
        apps_api.read_namespaced_deployment.return_value = self._deployment(
            match_labels={"app": "web"}
        )
        pod = make_pod(name="web-abc-1", owner="web-abc")
        pod.spec.containers.append(client.V1Container(name="sidecar", image="envoy:1"))
        pod.status.container_statuses = [
            container_status(name="app", ready=True),
            container_status(name="sidecar", ready=False),
        ]
        self._pods(api, pod)

        result = k8s.workload_pods("demo", "web")

        # One unready container makes the pod unready, and both are named so a
        # caller can ask for the right one's logs.
        assert result[0]["ready"] is False
        assert result[0]["containers"] == ["app", "sidecar"]
