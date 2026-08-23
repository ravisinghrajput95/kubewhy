"""
The chart and the code, checked against each other.

`helm template` proves the YAML renders. It does not prove the environment it
renders is one the process can start with -- and that gap is where this kind of
bug lives: a values file that installs cleanly, a pod that comes up, and an
inference mode that means something different from what the operator asked for.

So these tests render the chart for each supported shape, pull the container's
env out of the manifest, and hand it to the function that actually reads it.
Nothing is asserted about the YAML's spelling; everything is asserted about the
configuration it produces.

Skipped when helm is not installed, because the suite has to run in CI images
that do not have it.
"""

import json
import shutil
import subprocess

import pytest

import inference

CHART = "deploy/chart"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is not installed"
)


def render(*settings):
    """The rendered manifests, as a list of documents."""
    command = ["helm", "template", "t", CHART]
    for setting in settings:
        command += ["--set", setting]
    out = subprocess.run(command, capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(out.stderr.strip())
    return out.stdout


def refuses(*settings):
    """The stderr of a render that was supposed to fail."""
    command = ["helm", "template", "t", CHART]
    for setting in settings:
        command += ["--set", setting]
    out = subprocess.run(command, capture_output=True, text=True)
    assert out.returncode != 0, "the chart rendered when it should have refused"
    return out.stderr


def container_env(manifests, component=None):
    """
    The env of the first container, as the dict the process would see.

    Parsed with the YAML the kubernetes client already depends on rather than
    by hand: an env block read with a regex is a test that passes on a
    manifest Kubernetes would reject.
    """
    import yaml

    for document in yaml.safe_load_all(manifests):
        if not document or document.get("kind") != "Deployment":
            continue
        labels = document["spec"]["template"]["metadata"]["labels"]
        if component and labels.get("app.kubernetes.io/component") != component:
            continue
        if not component and "app.kubernetes.io/component" in labels:
            continue
        container = document["spec"]["template"]["spec"]["containers"][0]
        return {
            item["name"]: item.get("value")
            for item in container.get("env", [])
            if "value" in item
        }
    raise AssertionError(f"no Deployment for component {component!r}")


class TestTheChartConfiguresSomethingTheCodeAccepts:
    """
    The cross-check. Each of these renders the chart and then builds the real
    configuration object out of what it rendered.
    """

    def test_the_default_install_is_in_cluster_inference(self):
        config = inference.from_env(container_env(render()))

        assert config.primary.mode == "cluster"
        assert config.primary.provider == "ollama"
        assert config.primary.destination == "internal"
        assert config.policy.allow_external is False
        assert config.fallback is None

    def test_the_chart_ollama_is_the_endpoint_it_creates(self):
        """
        The chart can stand up its own Ollama. If model.ollamaHost and the
        Service that template creates ever disagree, the default is a broken
        address in one of the two places it appears -- and the pod comes up
        healthy and fails only when someone asks it something.
        """
        import yaml

        manifests = render("ollama.enabled=true", "ollama.namespace=ollama")
        services = [
            d for d in yaml.safe_load_all(manifests)
            if d and d.get("kind") == "Service"
            and d["metadata"]["name"] == "ollama"
        ]
        assert services, "ollama.enabled rendered no Service"

        service = services[0]
        host = f'{service["metadata"]["name"]}.{service["metadata"]["namespace"]}'
        config = inference.from_env(container_env(manifests))

        assert host in config.primary.endpoint

    def test_in_cluster_vllm(self):
        config = inference.from_env(container_env(render(
            "vllm.enabled=true",
            "inference.provider=vllm",
            "inference.endpoint=http://vllm.default.svc.cluster.local:8000/v1",
            "inference.model=Qwen/Qwen2.5-7B-Instruct",
        )))

        assert config.primary.provider == "vllm"
        assert config.primary.destination == "internal"
        assert config.primary.model == "Qwen/Qwen2.5-7B-Instruct"

    def test_api_inference_once_it_is_allowed(self):
        config = inference.from_env(container_env(render(
            "inference.mode=api",
            "inference.allowExternal=true",
            "inference.model=gpt-4o-mini",
        )))

        assert config.primary.mode == "api"
        assert config.primary.destination == "external"
        assert config.policy.allow_external is True

    def test_local_inference_at_an_address_the_pod_can_reach(self):
        """
        Local mode from inside a cluster means a workstation or a node, which
        has to be an address the pod can actually route to -- a container's
        localhost is its own.
        """
        config = inference.from_env(container_env(render(
            "inference.mode=local",
            "inference.endpoint=http://192.168.1.10:11434",
        )))

        assert config.primary.mode == "local"
        assert config.primary.destination == "internal"

    def test_a_fallback_survives_the_round_trip(self):
        config = inference.from_env(container_env(render(
            "inference.allowExternal=true",
            "inference.fallback.enabled=true",
            "inference.fallback.model=gpt-4o-mini",
        )))

        assert config.fallback is not None
        assert config.fallback.mode == "api"
        assert config.fallback.model == "gpt-4o-mini"

    def test_the_ui_gets_the_same_inference_configuration(self):
        """
        Both workloads run the agent loop. Rendering the env twice by hand is
        how the UI ends up talking to a different model than the controller
        for a release nobody notices.
        """
        manifests = render("ui.enabled=true", "ui.exposureAcknowledged=true",
                           "inference.model=llama3.2")

        # The inference configuration, not the whole env: the controller
        # legitimately carries watch and sink settings the UI has no use for.
        controller = inference.from_env(container_env(manifests))
        ui = inference.from_env(container_env(manifests, "ui"))

        assert controller.describe() == ui.describe()
        assert controller.primary.endpoint == ui.primary.endpoint
        assert controller.primary.model == "llama3.2"


class TestTheChartRefusesWhatTheCodeWouldRefuse:
    """
    The guards exist so the failure arrives from `helm install` with an
    explanation, rather than from a CrashLoopBackOff twenty minutes later.
    Each of these is a configuration inference.Config would also reject.
    """

    def test_api_mode_without_permission_to_leave_the_network(self):
        assert "allowExternal" in refuses("inference.mode=api")

    def test_an_unknown_mode(self):
        assert "expected local, cluster or api" in refuses(
            "inference.mode=onprem")

    def test_a_fallback_with_no_model_of_its_own(self):
        assert "own model name" in refuses(
            "inference.fallback.enabled=true", "inference.allowExternal=true")

    def test_a_fallback_that_leaves_the_network_without_permission(self):
        assert "not a way around" in refuses(
            "inference.fallback.enabled=true", "inference.fallback.model=x")

    def test_an_egress_policy_that_contradicts_the_mode(self):
        assert "opposite things" in refuses(
            "networkPolicy.enabled=true", "inference.mode=api",
            "inference.allowExternal=true")

    def test_the_same_configurations_are_refused_by_the_code(self):
        """
        The guards are a convenience, not the control. If the chart's checks
        were ever the only ones, a `kubectl apply` of a rendered manifest would
        walk straight past them.
        """
        with pytest.raises(ValueError):
            inference.from_env({"TRIAGE_INFERENCE_MODE": "api"})
        with pytest.raises(ValueError):
            inference.from_env({"TRIAGE_INFERENCE_MODE": "onprem"})


class TestTheInClusterModelIsActuallyUsable:
    """
    Both of these were found by installing the chart on a fresh kind cluster,
    not by templating it. `helm template` renders the broken version and the
    working one identically, which is precisely why they survived.
    """

    def _ollama_container(self, manifests):
        import yaml

        for document in yaml.safe_load_all(manifests):
            if not document or document.get("kind") != "Deployment":
                continue
            labels = document["spec"]["template"]["metadata"]["labels"]
            if labels.get("app.kubernetes.io/name") == "ollama":
                return document["spec"]["template"]["spec"]["containers"][0]
        raise AssertionError("ollama.enabled rendered no Deployment")

    def test_the_pull_waits_for_the_server_before_trying(self):
        """
        postStart runs concurrently with the container's main process, so
        `ollama pull` reached a server that was not listening yet, failed in
        under a second, and `|| true` swallowed it. Measured on kind
        2026-08-23: the pod ran with an empty model directory and answered
        every diagnosis with `model not found (status code: 404)`.

        A race rather than a certain failure, which is worse -- it resolved in
        this project's favour on GKE and against it on kind.
        """
        container = self._ollama_container(render("ollama.enabled=true"))
        hook = container["lifecycle"]["postStart"]["exec"]["command"][-1]

        assert "ollama list" in hook, "the hook does not wait for the server"
        assert "sleep" in hook, "the hook does not retry"
        assert hook.count("ollama pull") >= 1

    def test_readiness_means_the_model_is_there_not_that_a_port_is_open(self):
        """
        With an HTTP readiness probe this pod reported 1/1 Ready holding no
        model at all. Nothing in `kubectl get pods`, `describe` or the events
        said so -- the only evidence in the cluster was a 404 in Ollama's own
        access log.
        """
        container = self._ollama_container(
            render("ollama.enabled=true", "model.name=llama3.2"))
        probe = container["readinessProbe"]

        assert "httpGet" not in probe
        assert "llama3.2" in probe["exec"]["command"][-1]
        # The first window has to cover a model download; a pod still pulling
        # is correctly NotReady for all of it.
        assert probe["periodSeconds"] * probe["failureThreshold"] >= 300

    def test_the_port_check_returns_when_nothing_is_being_pulled(self):
        """
        With pullModelOnStart off, the chart is not responsible for the model
        and cannot require one -- an operator baking weights into an image or
        pulling them out of band would get a pod that is never Ready.
        """
        container = self._ollama_container(
            render("ollama.enabled=true", "ollama.pullModelOnStart=false"))

        assert "httpGet" in container["readinessProbe"]
        assert "lifecycle" not in container

    def test_the_endpoint_points_at_the_service_the_chart_creates(self):
        import yaml

        manifests = render("ollama.enabled=true", "ollama.namespace=kubewhy",
                           "model.ollamaHost=http://ollama.kubewhy.svc.cluster.local:11434")
        names = {d["metadata"]["name"] for d in yaml.safe_load_all(manifests)
                 if d and d.get("kind") == "Service"}
        config = inference.from_env(container_env(manifests))

        assert "ollama" in names
        assert "ollama.kubewhy" in config.primary.endpoint
        assert config.primary.destination == "internal"


class TestSecretsStayInSecrets:
    def test_an_api_key_is_never_a_plain_env_value(self):
        manifests = render("inference.mode=api", "inference.allowExternal=true",
                           "inference.apiKey.value=sk-should-not-be-inline")

        env = container_env(manifests)
        assert "OPENAI_API_KEY" not in env, (
            "the key was rendered as a literal env value; it must come from a "
            "secretKeyRef so it is not in the Deployment for anyone with get "
            "on deployments to read"
        )
        assert "sk-should-not-be-inline" not in json.dumps(
            [d for d in manifests.split("---") if "kind: Deployment" in d])

    def test_an_existing_secret_is_referenced_rather_than_copied(self):
        import yaml

        manifests = render("inference.mode=api", "inference.allowExternal=true",
                           "inference.apiKey.existingSecret=my-openai-key")

        secrets = [d for d in yaml.safe_load_all(manifests)
                   if d and d.get("kind") == "Secret"]
        assert secrets == [], "the chart created a Secret it was told existed"
        assert "my-openai-key" in manifests
