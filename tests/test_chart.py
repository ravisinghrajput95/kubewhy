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


AUTH = (
    "ui.enabled=true",
    "ui.auth.enabled=true",
    "ui.auth.issuerUrl=https://dex.example.com/dex",
    "ui.auth.clientID=kubewhy",
    "ui.auth.externalUrl=http://localhost:8080",
    "ui.auth.existingSecret=kubewhy-auth",
)


def containers(manifests, component):
    """Every container of one component's Deployment, by name."""
    import yaml

    for document in yaml.safe_load_all(manifests):
        if not document or document.get("kind") != "Deployment":
            continue
        labels = document["spec"]["template"]["metadata"]["labels"]
        if labels.get("app.kubernetes.io/component") != component:
            continue
        return {c["name"]: c for c in document["spec"]["template"]["spec"]["containers"]}
    raise AssertionError(f"no Deployment for component {component!r}")


def service(manifests, component):
    import yaml

    for document in yaml.safe_load_all(manifests):
        if not document or document.get("kind") != "Service":
            continue
        if document["metadata"]["labels"].get("app.kubernetes.io/component") != component:
            continue
        return document
    raise AssertionError(f"no Service for component {component!r}")


class TestTheConsoleIsUnreachableExceptThroughTheProxy:
    """
    The arrangement, not its spelling.

    The console's authentication is structural: with ui.auth.enabled the
    Streamlit process binds loopback and the Service targets the sidecar, so
    an unauthenticated browser cannot reach the app at all. Every assertion
    here is one half of that arrangement, because each half is useless alone
    -- a loopback bind whose Service still points at the console is an outage,
    and a Service pointing at the proxy while the console binds every
    interface is a bypass that works perfectly and looks fine.
    """

    def test_the_console_binds_loopback(self):
        ui = containers(render(*AUTH), "ui")["ui"]
        assert "--server.address=127.0.0.1" in ui["command"]
        assert "--server.address=0.0.0.0" not in ui["command"]

    def test_the_service_reaches_the_proxy_and_not_the_console(self):
        manifests = render(*AUTH)
        ports = service(manifests, "ui")["spec"]["ports"]

        assert [p["targetPort"] for p in ports] == ["auth"]

    def test_the_consoles_port_is_in_no_service(self):
        """
        The property that makes the bind meaningful. Asserted over every
        Service in the release rather than the console's own, because a second
        Service selecting the same pods would undo this from another file.
        """
        import yaml

        manifests = render(*AUTH)
        console_port = containers(manifests, "ui")["ui"]["ports"][0]["containerPort"]

        for document in yaml.safe_load_all(manifests):
            if not document or document.get("kind") != "Service":
                continue
            for port in document["spec"]["ports"]:
                assert port.get("targetPort") != console_port
                assert port.get("targetPort") != "http" or \
                    document["metadata"]["labels"].get(
                        "app.kubernetes.io/component") != "ui"

    def test_the_proxy_forwards_to_the_console_over_loopback(self):
        """
        Naming the pod IP here would work, and would quietly undo the bind.
        """
        manifests = render(*AUTH)
        console_port = containers(manifests, "ui")["ui"]["ports"][0]["containerPort"]
        proxy = containers(manifests, "ui")["auth"]

        assert f"--upstream=http://127.0.0.1:{console_port}" in proxy["args"]

    def test_moving_the_console_port_moves_the_upstream_with_it(self):
        """
        Two places hold that number and a test that renders only the default
        cannot tell they are coupled.
        """
        manifests = render(*AUTH, "ui.port=9000")
        ui = containers(manifests, "ui")

        assert "--server.port=9000" in ui["ui"]["command"]
        assert "--upstream=http://127.0.0.1:9000" in ui["auth"]["args"]


class TestTheConsoleKnowsWhatIsInFrontOfIt:
    def test_proxy_mode_is_declared_to_the_app(self):
        """
        The second control. Without this the console would serve a request
        carrying no identity header as anonymous, which is exactly what
        happens if the sidecar is removed and the bind is loosened together.
        """
        assert container_env(render(*AUTH), "ui")["TRIAGE_AUTH_MODE"] == "proxy"

    def test_the_proxy_passes_the_identity_the_app_reads(self):
        """
        Without --pass-user-headers the proxy authenticates the browser and
        tells the console nothing, and the console then refuses every request.
        A loud failure, but only because the app declares proxy mode -- this
        pair has to stay together.
        """
        proxy = containers(render(*AUTH), "ui")["auth"]
        assert "--pass-user-headers=true" in proxy["args"]

    def test_the_proxy_does_not_trust_forwarded_headers_from_the_browser(self):
        """
        --reverse-proxy=true would let a client spell its own source address.
        It is the default-off flag most likely to be turned on by somebody
        copying an ingress example.
        """
        proxy = containers(render(*AUTH), "ui")["auth"]
        assert "--reverse-proxy=false" in proxy["args"]
        assert "--reverse-proxy=true" not in proxy["args"]


class TestAuthSecretsStayOutOfTheManifest:
    def test_the_client_secret_is_a_secret_reference(self):
        proxy = containers(render(*AUTH), "ui")["auth"]
        names = {e["name"]: e for e in proxy["env"]}

        assert "valueFrom" in names["OAUTH2_PROXY_CLIENT_SECRET"]
        assert "value" not in names["OAUTH2_PROXY_CLIENT_SECRET"]

    def test_no_credential_is_passed_as_an_argument(self):
        """
        An argument is visible in `kubectl describe pod` and in every process
        listing inside the container.
        """
        proxy = containers(render(*AUTH), "ui")["auth"]
        joined = " ".join(proxy["args"])

        assert "client-secret" not in joined
        assert "cookie-secret" not in joined


class TestTheAuthGuards:
    def test_auth_satisfies_the_exposure_acknowledgement(self):
        """
        The acknowledgement exists because the console had no authentication.
        Once it does, demanding the acknowledgement as well would be asking
        the operator to confirm a risk they just removed.
        """
        render(*AUTH)  # raises if it refuses

    def test_neither_auth_nor_acknowledgement_still_refuses(self):
        assert "ui.auth.enabled=true or ui.exposureAcknowledged=true" in refuses(
            "ui.enabled=true")

    @pytest.mark.parametrize(
        "omitted",
        ["ui.auth.issuerUrl", "ui.auth.clientID",
         "ui.auth.externalUrl", "ui.auth.existingSecret"],
    )
    def test_every_required_setting_is_named_when_it_is_missing(self, omitted):
        """
        Named individually rather than "auth is misconfigured". externalUrl in
        particular produces a login loop rather than an error when it is
        wrong, and a loop is much harder to diagnose than a failed install.
        """
        kept = [s for s in AUTH if not s.startswith(omitted + "=")]
        assert omitted in refuses(*kept)


class TestWithoutAuthNothingChanged:
    """
    The acknowledged-exposure path is what existing installs use, and adding
    the proxy must not have moved it underneath them.
    """

    def test_the_console_still_binds_every_interface(self):
        ui = containers(render("ui.enabled=true", "ui.exposureAcknowledged=true"), "ui")
        assert "--server.address=0.0.0.0" in ui["ui"]["command"]

    def test_there_is_no_sidecar(self):
        ui = containers(render("ui.enabled=true", "ui.exposureAcknowledged=true"), "ui")
        assert list(ui) == ["ui"]

    def test_the_service_still_reaches_the_console(self):
        manifests = render("ui.enabled=true", "ui.exposureAcknowledged=true")
        assert service(manifests, "ui")["spec"]["ports"][0]["targetPort"] == "http"

    def test_the_app_is_not_told_to_expect_an_identity(self):
        """
        Declaring proxy mode with no proxy would refuse every request, which
        is a failure mode this path must not acquire by accident.
        """
        env = container_env(render("ui.enabled=true", "ui.exposureAcknowledged=true"), "ui")
        assert "TRIAGE_AUTH_MODE" not in env


class TestTheConsoleCanActuallyBeProbed:
    """
    The defect installing found and rendering could not.

    The kubelet probes the POD IP. With auth on the console binds loopback, so
    an httpGet probe dials 10.x.x.x:8501 and gets connection refused forever:
    readiness keeps the pod out of the Service and liveness kills the
    container every 40 seconds, while the proxy beside it stays healthy. On
    kind the pod sat 1/2 Running with 4 restarts and `helm template` rendered
    the broken probe and the working one identically.
    """

    def test_the_probes_do_not_dial_the_pod_ip(self):
        ui = containers(render(*AUTH), "ui")["ui"]

        for probe in ("readinessProbe", "livenessProbe"):
            assert "httpGet" not in ui[probe], (
                f"{probe} uses httpGet, which the kubelet sends to the pod IP; "
                "the console binds loopback when auth is on")

    def test_the_probes_reach_the_console_over_loopback(self):
        ui = containers(render(*AUTH), "ui")["ui"]
        port = ui["ports"][0]["containerPort"]

        for probe in ("readinessProbe", "livenessProbe"):
            command = " ".join(ui[probe]["exec"]["command"])
            assert f"127.0.0.1:{port}" in command
            assert "/_stcore/health" in command

    def test_the_probe_follows_a_changed_console_port(self):
        ui = containers(render(*AUTH, "ui.port=9000"), "ui")["ui"]
        assert "127.0.0.1:9000" in " ".join(ui["readinessProbe"]["exec"]["command"])

    def test_the_proxy_is_still_probed_over_http(self):
        """
        It binds every interface, so the pod IP reaches it. Converting this to
        an exec probe too would be cargo-culting the fix.
        """
        proxy = containers(render(*AUTH), "ui")["auth"]
        assert proxy["readinessProbe"]["httpGet"]["path"] == "/ping"

    def test_without_auth_the_http_probe_is_unchanged(self):
        ui = containers(render("ui.enabled=true", "ui.exposureAcknowledged=true"), "ui")["ui"]
        assert ui["readinessProbe"]["httpGet"]["path"] == "/_stcore/health"


class TestTheNotesMatchTheDeployment:
    """
    NOTES.txt is the last thing an operator reads and the first thing they
    believe. It told them the console had no authentication while it was
    running behind a proxy, and printed a port-forward to a port that is in no
    Service -- both found by installing, because nothing renders NOTES.txt in
    a test but `helm install` prints it.
    """

    def test_the_notes_do_not_claim_the_console_is_unauthenticated(self):
        # NOTES.txt is not a manifest, so it is not among the rendered
        # documents and `helm template` never produces it. A dry-run install
        # is the only way a test can see what an operator is told.
        assert "no authentication" not in _notes(*AUTH)

    def test_the_notes_name_the_proxy_port_not_the_console_port(self):
        notes = _notes(*AUTH)
        assert "4180" in notes
        assert "port-forward" not in notes

    def test_the_notes_say_authentication_is_not_authorization(self):
        """
        The single most likely misreading of this feature, and the one that
        would put kubewhy in front of two teams that must not see each other.
        """
        assert "not authorization" in _notes(*AUTH)

    def test_without_auth_the_notes_still_warn(self):
        notes = _notes("ui.enabled=true", "ui.exposureAcknowledged=true")
        assert "no authentication" in notes
        assert "ui.auth.enabled=true" in notes


def _notes(*settings):
    """
    NOTES.txt, which only an install renders -- `helm template` does not
    produce it at all.

    `--dry-run=client`, not a bare `--dry-run`. The plain form contacts the
    API server for capability discovery, so these four tests passed on a
    laptop with a kubeconfig and failed every CI run with "Kubernetes cluster
    unreachable: dial tcp [::1]:8080". The rest of this module renders with
    `helm template` and never noticed, which is why the skipif at the top
    guards on helm being installed and not on a cluster being reachable.
    """
    command = ["helm", "install", "t", CHART, "--dry-run=client",
               "--namespace", "t"]
    for setting in settings:
        command += ["--set", setting]
    out = subprocess.run(command, capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(out.stderr.strip())
    return out.stdout.split("NOTES:", 1)[-1]


class TestOneReplicaOfTheConsole:
    """
    The chart's comment used to say the UI held no state between requests and
    that more than one replica was safe here. It does hold state: ui.py keeps
    investigation history in store.build(), so two pods are two histories and
    which one a person sees depends on which pod their websocket landed on.
    """

    def test_two_replicas_are_refused(self):
        assert "ui.replicas must be 1" in refuses(
            "ui.enabled=true", "ui.exposureAcknowledged=true", "ui.replicas=2")

    def test_the_refusal_explains_what_breaks(self):
        """
        "Not supported" would send someone looking for the supported way. What
        they need to know is that a reconnect lands in a different history.
        """
        message = refuses("ui.enabled=true", "ui.exposureAcknowledged=true",
                          "ui.replicas=2")
        assert "two histories" in message

    def test_one_replica_renders(self):
        render("ui.enabled=true", "ui.exposureAcknowledged=true", "ui.replicas=1")

    def test_the_controller_is_pinned_to_one_and_recreated(self):
        """
        Two controllers deliver every finding twice, and a RollingUpdate is
        how you get two: old pod and new pod both watching during a rollout.
        """
        import yaml

        for document in yaml.safe_load_all(render()):
            if not document or document.get("kind") != "Deployment":
                continue
            if "app.kubernetes.io/component" in document["spec"]["template"]["metadata"]["labels"]:
                continue
            assert document["spec"]["replicas"] == 1
            assert document["spec"]["strategy"]["type"] == "Recreate"
            return
        raise AssertionError("no controller Deployment")


class TestTheConsoleKeepsItsHistoryAcrossARestart:
    """
    persistence.enabled fixed the restart case for the controller and did not
    fix it here: the console's sidebar came back empty after every restart,
    because nothing gave it a TRIAGE_STATE_DB.
    """

    PERSISTED = ("ui.enabled=true", "ui.exposureAcknowledged=true",
                 "persistence.enabled=true", "podSecurityContext.fsGroup=1000")

    def test_the_console_gets_a_state_db(self):
        env = container_env(render(*self.PERSISTED), "ui")
        assert env["TRIAGE_STATE_DB"].endswith("/state.db")

    def test_it_is_not_the_controllers_claim(self):
        """
        Two processes writing one SQLite file over a volume is the corruption
        case store.py describes, and mounting the controller's claim here
        would look tidier than creating a second PVC -- which is how it would
        get done.
        """
        import yaml

        claims = {}
        for document in yaml.safe_load_all(render(*self.PERSISTED)):
            if not document or document.get("kind") != "Deployment":
                continue
            component = document["spec"]["template"]["metadata"]["labels"].get(
                "app.kubernetes.io/component", "controller")
            for volume in document["spec"]["template"]["spec"].get("volumes", []):
                if "persistentVolumeClaim" in volume:
                    claims[component] = volume["persistentVolumeClaim"]["claimName"]

        assert claims["controller"] != claims["ui"], (
            f"both components mount {claims['controller']}")

    def test_both_claims_are_created(self):
        import yaml

        names = {d["metadata"]["name"] for d in yaml.safe_load_all(render(*self.PERSISTED))
                 if d and d.get("kind") == "PersistentVolumeClaim"}
        assert names == {"t-state", "t-ui-state"}

    def test_the_claims_are_read_write_once(self):
        """RWX here would advertise a multi-replica story the store cannot honour."""
        import yaml

        for document in yaml.safe_load_all(render(*self.PERSISTED)):
            if document and document.get("kind") == "PersistentVolumeClaim":
                assert document["spec"]["accessModes"] == ["ReadWriteOnce"]

    def test_without_persistence_the_console_gets_no_state_db(self):
        env = container_env(render("ui.enabled=true", "ui.exposureAcknowledged=true"), "ui")
        assert "TRIAGE_STATE_DB" not in env
