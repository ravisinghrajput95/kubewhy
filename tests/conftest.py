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


@pytest.fixture(autouse=True)
def no_test_reaches_a_cluster():
    """
    No test builds a Kubernetes client that could reach a real cluster.

    Nothing in this suite is supposed to reach one, and until this existed
    nothing stopped it. Most of `test_agent_loop.py` stubs the tools with
    `patch.dict(agent.TOOLS, ...)`, but the runaway-loop and nudge cases do
    not, so every round dispatched a real cluster read -- and what that cost
    depended on the developer's kubeconfig, which is not part of the
    repository.

    Measured 2026-09-01, one machine, the single test
    `test_no_nudge_without_rounds_left_to_use_it`, which drives seven rounds:

        no current-context                        0.56s
        context at a port that refuses            0.58s
        context at an unroutable address       7m 00.6s
        the same, with this fixture               0.57s

    A refused connection is instant, which is why this stayed invisible on a
    laptop with a stopped kind cluster. An address that black-holes packets
    is the same configuration with a firewall in front of it, and it costs
    seven minutes for one test. The handoff records 78s for this test against
    a dead kind node; that is the same failure, milder.

    Slow is the mild version. The bad version is a developer whose
    `current-context` names a cluster that *works*: those tests then read it
    for real. `~/.kube/config` was rewritten by another process during the
    session that wrote this fixture -- `current-context` was unset at 09:06
    and named `kind-aiops-test` by 09:14 -- so the machine's state changing
    underneath a test run is not hypothetical either.

    **This is not what made `controller.py` and `ui.py` unsurveyable.** That
    claim is in the handoff and it does not reproduce: with no kubeconfig and
    with one, `test_controller.py` runs in 7.0s and `test_ui.py` in 5.5s, and
    both pass alone. Their 7s is deliberate sleeps in the run-path tests. Both
    modules were surveyed on 2026-09-01 and the results are in VALIDATION.md.

    A test that genuinely wants a client patches `_build_bundle` itself, and
    `patch.object` restores this one afterwards.
    """
    import routers.k8s_pods_info as k8s

    class Unreachable:
        """
        A client bundle that builds and cannot connect.

        That is the shape the real code has on a machine with a kubeconfig:
        `new_client_from_config` opens no socket, so the failure lands on the
        call, where every tool already handles it. Refusing at *build* time
        instead moves the exception to a line nothing expects -- a Streamlit
        AppTest rendering a page of those never completes, and the suite hangs
        rather than failing.
        """

        def __getattr__(self, name):
            def unreachable(*args, **kwargs):
                raise ConnectionError(
                    "the test suite does not reach a cluster: patch "
                    "routers.k8s_pods_info._build_bundle if this test needs "
                    "a client"
                )

            return unreachable

    def refuse(requested):
        bundle = {key: Unreachable() for key in
                  ("core", "apps", "discovery", "networking",
                   "autoscaling", "policy", "storage")}
        bundle["active"] = "no-cluster-in-tests"
        return bundle

    # Cleared once for the session, not per test. Per-test clearing makes the
    # three AppTest cases in test_investigation_identity.py time out at 60s
    # each -- measured, not guessed: 20 passed in 3.7s without the clear and
    # 3 failed in 237s with it. Clearing once still closes the hole it is
    # there for, which is a bundle cached before any fixture ran; nothing can
    # cache a real one *during* the session, because `refuse` is installed
    # for every test.
    if not getattr(k8s, "_cleared_for_tests", False):
        k8s._bundles.clear()
        k8s._cleared_for_tests = True

    original, k8s._build_bundle = k8s._build_bundle, refuse
    try:
        yield
    finally:
        k8s._build_bundle = original
