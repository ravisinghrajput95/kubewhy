"""
Fixtures building real kubernetes client model objects.

Using the actual V1* models rather than bare Mocks means the tests fail if the
projection code reaches for an attribute the API does not have -- which is the
mistake worth catching.
"""

import pytest
from kubernetes import client


def container_status(
    name="app",
    ready=True,
    restart_count=0,
    waiting_reason=None,
    terminated_reason=None,
    exit_code=None,
):
    state = client.V1ContainerState()
    if waiting_reason:
        state.waiting = client.V1ContainerStateWaiting(
            reason=waiting_reason, message=f"{waiting_reason} detail"
        )
    elif terminated_reason:
        state.terminated = client.V1ContainerStateTerminated(
            reason=terminated_reason,             # Not `exit_code or 1`: 0 is a real exit code and the falsy check
            # turned every successful termination into a failure.
            exit_code=1 if exit_code is None else exit_code
        )
    else:
        state.running = client.V1ContainerStateRunning()

    last_state = client.V1ContainerState()
    if terminated_reason:
        last_state.terminated = client.V1ContainerStateTerminated(
            reason=terminated_reason,             # Not `exit_code or 1`: 0 is a real exit code and the falsy check
            # turned every successful termination into a failure.
            exit_code=1 if exit_code is None else exit_code
        )

    return client.V1ContainerStatus(
        name=name,
        ready=ready,
        restart_count=restart_count,
        image="busybox:1.36",
        image_id="",
        state=state,
        last_state=last_state,
    )


def make_pod(
    name="test-pod",
    phase="Running",
    node="node-1",
    statuses=None,
    limits=None,
    requests=None,
    namespace="demo",
    owner=None,
):
    resources = client.V1ResourceRequirements(
        limits=limits or {}, requests=requests or {}
    )
    metadata = client.V1ObjectMeta(name=name, namespace=namespace)
    if owner:
        # A real ReplicaSet reference: workload grouping trims the hash suffix
        # off this name, so tests that skip it would not exercise the grouping.
        metadata.owner_references = [
            client.V1OwnerReference(
                api_version="apps/v1",
                kind="ReplicaSet",
                name=owner,
                uid=f"uid-{owner}",
                controller=True,
            )
        ]
    return client.V1Pod(
        metadata=metadata,
        spec=client.V1PodSpec(
            node_name=node,
            containers=[
                client.V1Container(
                    name="app", image="busybox:1.36", resources=resources
                )
            ],
        ),
        status=client.V1PodStatus(
            phase=phase,
            container_statuses=statuses if statuses is not None else [container_status()],
        ),
    )


@pytest.fixture
def healthy_pod():
    return make_pod(name="healthy", phase="Running")


@pytest.fixture
def oomkilled_pod():
    return make_pod(
        name="memory-hog",
        phase="Running",
        statuses=[
            container_status(
                ready=False,
                restart_count=4,
                waiting_reason="CrashLoopBackOff",
                terminated_reason="OOMKilled",
                exit_code=137,
            )
        ],
        limits={"memory": "64Mi"},
        requests={"memory": "32Mi"},
    )


@pytest.fixture
def pod_list(healthy_pod, oomkilled_pod):
    return client.V1PodList(items=[healthy_pod, oomkilled_pod])
