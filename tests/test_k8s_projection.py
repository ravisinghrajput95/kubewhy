"""
Tests for the Kubernetes projections.

The point of these is not that the API is called, but that the projection
stays small and keeps the fields a diagnosis actually depends on. A projection
that silently starts returning raw objects would blow the model's context
window, so size is asserted explicitly.
"""

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
