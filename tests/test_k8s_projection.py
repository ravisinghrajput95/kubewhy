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

    def test_distinguishes_unready_pods_from_no_match(self, api):
        """The two causes need different fixes, so they must read differently."""
        api.read_namespaced_service.return_value = self._service()
        api.read_namespaced_endpoints.return_value = client.V1Endpoints(
            subsets=[
                client.V1EndpointSubset(
                    addresses=[],
                    not_ready_addresses=[client.V1EndpointAddress(ip="10.0.0.1")],
                )
            ]
        )

        result = k8s.get_service_endpoints("web", "demo")
        assert result["ready_endpoints"] == []
        assert "readiness probe" in result["diagnosis"]
        assert "1 pod(s) match" in result["diagnosis"]

    def test_selector_matching_nothing_says_so(self, api):
        api.read_namespaced_service.return_value = self._service()
        api.read_namespaced_endpoints.return_value = client.V1Endpoints(subsets=[])

        result = k8s.get_service_endpoints("web", "demo")
        assert "matches no pods at all" in result["diagnosis"]

    def test_healthy_service_has_no_diagnosis(self, api):
        api.read_namespaced_service.return_value = self._service()
        api.read_namespaced_endpoints.return_value = client.V1Endpoints(
            subsets=[
                client.V1EndpointSubset(
                    addresses=[client.V1EndpointAddress(ip="10.0.0.1")]
                )
            ]
        )

        result = k8s.get_service_endpoints("web", "demo")
        assert result["ready_endpoints"] == ["10.0.0.1"]
        assert "diagnosis" not in result

    def test_missing_endpoints_object_is_handled(self, api):
        api.read_namespaced_service.return_value = self._service()
        api.read_namespaced_endpoints.side_effect = ApiException(status=404)

        result = k8s.get_service_endpoints("web", "demo")
        assert result["ready_endpoints"] == []
        assert "no endpoints object" in result["diagnosis"]


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
