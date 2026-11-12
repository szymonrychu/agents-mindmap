"""Chart skeleton render: one api Deployment, listener Services, external Secret refs only."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "chart" / "agents-mindmap"

BASE = {
    "image": {"tag": "1.2.3"},
    "publicBaseUrl": "https://lab.example.test",
    "postgres": {"existingSecret": "agents-mindmap-db-app"},
    "orchestrator": {"url": "http://orch.agents.svc", "tokenSecret": {"name": "mm-orch"}},
    "relayTokenSecret": {"name": "mm-relay"},
}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _run(values: dict) -> subprocess.CompletedProcess:
    helm = shutil.which("helm")
    assert helm, "helm binary not found on PATH; the chart tests require it"
    with tempfile.TemporaryDirectory() as tmp:
        vf = Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values))
        return subprocess.run(
            [helm, "template", "agents-mindmap", str(CHART), "-n", "agents", "-f", str(vf)],
            capture_output=True,
            text=True,
        )


def render(overrides: dict | None = None) -> list[dict]:
    proc = _run(_merge(BASE, overrides or {}))
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


LISTENERS = ("web", "service", "delegate")


def deployments(docs: list[dict]) -> dict[str, dict]:
    return {
        d["metadata"]["labels"]["app.kubernetes.io/component"]: d
        for d in of_kind(docs, "Deployment")
    }


def api_container(docs: list[dict], component: str = "web") -> dict:
    return deployments(docs)[component]["spec"]["template"]["spec"]["containers"][0]


def env_of(container: dict) -> dict:
    return {e["name"]: e for e in container.get("env", [])}


def test_chart_skeleton_renders():
    docs = render()
    kinds = sorted(d["kind"] for d in docs)
    assert kinds == [
        "ConfigMap",
        "Deployment",
        "Deployment",
        "Pod",
        "Service",
        "Service",
        "ServiceAccount",
    ]
    (hook,) = of_kind(docs, "Pod")
    assert hook["metadata"]["annotations"]["helm.sh/hook"] == "test"
    deps = deployments(docs)
    assert set(deps) == {"web", "service"}
    for component, port in (("web", 8080), ("service", 8082)):
        dep = deps[component]
        assert dep["metadata"]["name"] == f"agents-mindmap-{component}"
        spec = dep["spec"]["template"]["spec"]
        assert spec["automountServiceAccountToken"] is False
        (container,) = spec["containers"]
        assert container["command"] == ["mindmap"]
        assert container["args"] == [f"serve-{component}"]
        assert container["image"] == "harbor.szymonrichert.pl/containers/agents-mindmap:1.2.3"
        assert spec["initContainers"][0]["args"] == ["migrate"]
        assert container["ports"] == [{"name": "http", "containerPort": port, "protocol": "TCP"}]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert spec["securityContext"]["runAsNonRoot"] is True


def test_services_without_delegate():
    svc = {s["metadata"]["name"]: s for s in of_kind(render(), "Service")}
    assert set(svc) == {"agents-mindmap-web", "agents-mindmap-service"}
    assert svc["agents-mindmap-web"]["spec"]["ports"][0]["port"] == 80
    assert svc["agents-mindmap-web"]["spec"]["ports"][0]["targetPort"] == "http"
    assert svc["agents-mindmap-service"]["spec"]["ports"][0]["port"] == 8082


def test_delegate_rendered_only_with_tokens_secret():
    assert "MINDMAP_DELEGATE_TOKENS" not in env_of(api_container(render()))
    docs = render({"delegate": {"tokensSecret": {"name": "mm-delegate"}}})
    svc = {s["metadata"]["name"]: s for s in of_kind(docs, "Service")}
    assert svc["agents-mindmap-delegate"]["spec"]["ports"][0]["port"] == 8081
    container = api_container(docs, "delegate")
    assert container["args"] == ["serve-delegate"]
    assert container["ports"] == [{"name": "http", "containerPort": 8081, "protocol": "TCP"}]
    assert env_of(container)["MINDMAP_DELEGATE_TOKENS"]["valueFrom"]["secretKeyRef"] == {
        "name": "mm-delegate",
        "key": "tokens",
    }


def test_secret_values_come_from_external_secrets_only():
    docs = render({"taskboard": {"url": "http://tb:8003", "tokenSecret": {"name": "mm-tb"}}})
    assert not of_kind(docs, "Secret")
    refs = {
        k: v["valueFrom"]["secretKeyRef"] for k, v in env_of(api_container(docs, "service")).items()
    }
    assert refs == {
        "MINDMAP_DATABASE_URL": {"name": "agents-mindmap-db-app", "key": "uri"},
        "MINDMAP_ORCHESTRATOR_TOKEN": {"name": "mm-orch", "key": "token"},
        "MINDMAP_RELAY_TOKEN": {"name": "mm-relay", "key": "token"},
    }


def test_settings_configmap():
    docs = render({"embedder": {"url": "http://emb"}, "env": {"MINDMAP_LOG_LEVEL": "debug"}})
    (cm,) = of_kind(docs, "ConfigMap")
    assert cm["data"] == {
        "MINDMAP_PUBLIC_BASE_URL": "https://lab.example.test",
        "MINDMAP_ORCHESTRATOR_URL": "http://orch.agents.svc",
        "MINDMAP_HTTP_PORT": "8080",
        "MINDMAP_SERVICE_PORT": "8082",
        "MINDMAP_DELEGATE_PORT": "8081",
        "MINDMAP_ALLOWED_CIDRS": "127.0.0.1/32",
        "MINDMAP_EMBEDDER_URL": "http://emb",
        "MINDMAP_LOG_LEVEL": "debug",
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"publicBaseUrl": ""}, "publicBaseUrl is required"),
        ({"orchestrator": {"url": ""}}, "orchestrator.url is required"),
        ({"postgres": {"existingSecret": ""}}, "postgres.existingSecret is required"),
        ({"orchestrator": {"tokenSecret": {"name": ""}}}, "orchestrator.tokenSecret.name"),
        ({"relayTokenSecret": {"name": ""}}, "relayTokenSecret.name is required"),
        ({"taskboard": {"url": "http://tb"}}, "taskboard.tokenSecret.name is required"),
        ({"env": {"OTHER_KEY": "x"}}, "must start with MINDMAP_"),
    ],
)
def test_bad_values_fail_the_render(overrides: dict, message: str):
    proc = _run(_merge(BASE, overrides))
    assert proc.returncode != 0
    assert message in proc.stderr


def test_ingress_off_by_default_and_routes_only_web_under_mindmap():
    assert not of_kind(render(), "Ingress")
    (ing,) = of_kind(render({"ingress": {"enabled": True}}), "Ingress")
    backends = [
        (p["path"], p["backend"]["service"]["name"], p["pathType"])
        for rule in ing["spec"]["rules"]
        for p in rule["http"]["paths"]
    ]
    assert backends == [("/mindmap", "agents-mindmap-web", "Prefix")]


SHA_TAGS = ["", "abc1234", "latest", "1.2", "v1.2.3", "1.2.3-rc1"]


@pytest.mark.parametrize("tag", SHA_TAGS)
def test_chart_image_semver(tag: str):
    proc = _run(_merge(BASE, {"image": {"tag": tag}}))
    assert proc.returncode != 0
    assert "image.tag must be a semver" in proc.stderr
    assert "1.2.3" not in proc.stdout


def test_chart_image_semver_accepts_x_y_z():
    docs = render({"image": {"tag": "10.20.30"}})
    assert api_container(docs)["image"].endswith("/agents-mindmap:10.20.30")


OAUTH = {
    "oauth2Proxy": {
        "enabled": True,
        "redeemUrl": "http://keycloak.example/token",
        "secretName": "mm-proxy",
        "secretNamespace": "agents",
    }
}


def pod_annotations(docs: list[dict], component: str = "web") -> dict:
    return deployments(docs)[component]["spec"]["template"]["metadata"]["annotations"]


def test_chart_renders_listeners_and_sidecar():
    docs = render({**OAUTH, "delegate": {"tokensSecret": {"name": "mm-delegate"}}})
    dep = deployments(docs)["web"]
    anno = pod_annotations(docs)
    assert anno["oauth2-proxy-admission/enable"] == "true"
    assert anno["oauth2-proxy-admission/redeem-url"] == "http://keycloak.example/token"
    assert anno["oauth2-proxy-admission/secret-name"] == "mm-proxy"
    assert anno["oauth2-proxy-admission/secret-namespace"] == "agents"
    container = dep["spec"]["template"]["spec"]["containers"][0]
    # the injector reads the first container and its first port: that is the web listener
    assert container["ports"][0] == {"name": "http", "containerPort": 8080, "protocol": "TCP"}
    assert [p["name"] for p in container["ports"]] == ["http"]
    # the sidecar takes the name `http`: probes address the port by number
    assert container["livenessProbe"]["httpGet"]["port"] == 8080
    assert container["readinessProbe"]["httpGet"]["port"] == 8080
    svc = {s["metadata"]["name"]: s["spec"]["ports"][0] for s in of_kind(docs, "Service")}
    assert svc["agents-mindmap-web"]["targetPort"] == "http"
    assert svc["agents-mindmap-service"]["targetPort"] == "http"
    assert svc["agents-mindmap-delegate"]["targetPort"] == "http"


def test_sidecar_annotations_off_by_default_and_complete_when_on():
    assert not any(k.startswith("oauth2-proxy-admission/") for k in pod_annotations(render()))
    for field in ("redeemUrl", "secretName", "secretNamespace"):
        bad = {"oauth2Proxy": {**OAUTH["oauth2Proxy"], field: ""}}
        proc = _run(_merge(BASE, bad))
        assert proc.returncode != 0
        assert f"oauth2Proxy.{field} is required" in proc.stderr


LITERAL = re.compile(r"(?i)(token|secret|password)[\w-]*\s*:\s*\S{12,}")


def test_chart_no_literal_secrets():
    """Rendered with every secret source set: values appear only as secretKeyRef, and the chart
    never renders a Secret nor a token-like literal."""
    docs = render(
        {
            **OAUTH,
            "delegate": {"tokensSecret": {"name": "mm-delegate"}},
            "taskboard": {"url": "http://tb:8003", "tokenSecret": {"name": "mm-tb"}},
            "embedder": {"enabled": True, "url": "http://emb"},
            "notifier": {"enabled": True},
        }
    )
    assert not of_kind(docs, "Secret")
    for dep in of_kind(docs, "Deployment"):
        spec = dep["spec"]["template"]["spec"]
        for container in (*spec["containers"], *spec.get("initContainers", [])):
            for env in container.get("env", []):
                if env["name"] == "MINDMAP_TASKBOARD_URL":  # a plain URL, no secret
                    continue
                assert set(env) == {"name", "valueFrom"}, env["name"]
                assert set(env["valueFrom"]) == {"secretKeyRef"}
    for cm in of_kind(docs, "ConfigMap"):
        for key, value in cm["data"].items():
            assert not re.search(r"TOKEN|PASSWORD|SECRET|DATABASE_URL", key), key
            assert "://" not in value or "@" not in value, key
    rendered = "\n".join(
        line for line in yaml.safe_dump_all(docs).splitlines() if "secretKeyRef" not in line
    )
    assert not LITERAL.search(
        "\n".join(
            line
            for line in rendered.splitlines()
            if not line.strip().startswith(("name:", "key:", "oauth2-proxy-admission/secret"))
        )
    )


def test_chart_delegate_optional():
    docs = render()
    assert "delegate" not in deployments(docs)
    assert "agents-mindmap-delegate" not in [
        s["metadata"]["name"] for s in of_kind(docs, "Service")
    ]
    assert all(
        "MINDMAP_DELEGATE_TOKENS" not in env_of(api_container(docs, c))
        for c in LISTENERS
        if c in deployments(docs)
    )


def workers(docs: list[dict]) -> dict[str, dict]:
    return {
        d["metadata"]["labels"]["app.kubernetes.io/component"]: d
        for d in of_kind(docs, "Deployment")
        if d["metadata"]["labels"]["app.kubernetes.io/component"] not in LISTENERS
    }


def test_workers_off_by_default():
    assert workers(render()) == {}


def test_notifier_and_embedder_deployments_behind_flags():
    docs = render(
        {
            "taskboard": {"url": "http://tb:8003", "tokenSecret": {"name": "mm-tb"}},
            "embedder": {"enabled": True, "url": "http://emb"},
            "notifier": {"enabled": True},
        }
    )
    found = workers(docs)
    assert set(found) == {"notifier", "embedder"}
    for name, dep in found.items():
        spec = dep["spec"]
        assert spec["replicas"] == 1 and spec["strategy"] == {"type": "Recreate"}
        pod = spec["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        (container,) = pod["containers"]
        assert container["command"] == ["mindmap"] and container["args"] == [name]
        assert "ports" not in container
        assert "MINDMAP_DATABASE_URL" in env_of(container)
        assert not any(
            k.startswith("oauth2-proxy-admission/")
            for k in spec["template"]["metadata"]["annotations"]
        )
    only = workers(
        render(
            {
                "notifier": {"enabled": True},
                "taskboard": {"url": "http://tb", "tokenSecret": {"name": "t"}},
            }
        )
    )
    assert set(only) == {"notifier"}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"notifier": {"enabled": True}}, "notifier.enabled needs taskboard.url"),
        ({"embedder": {"enabled": True}}, "embedder.enabled needs embedder.url"),
    ],
)
def test_worker_flags_need_their_settings(overrides: dict, message: str):
    proc = _run(_merge(BASE, overrides))
    assert proc.returncode != 0
    assert message in proc.stderr


FULL = {
    **OAUTH,
    "delegate": {"tokensSecret": {"name": "mm-delegate"}},
    "taskboard": {"url": "http://tb:8003", "tokenSecret": {"name": "mm-tb"}},
    "embedder": {"enabled": True, "url": "http://emb"},
    "notifier": {"enabled": True},
}

TOKENS = {
    "MINDMAP_RELAY_TOKEN",
    "MINDMAP_DELEGATE_TOKENS",
    "MINDMAP_ORCHESTRATOR_TOKEN",
    "MINDMAP_TASKBOARD_TOKEN",
}

# container -> the token env vars it verifies or sends (secrets invariant 9: only where used)
EXPECTED_TOKENS = {
    "web": {"MINDMAP_ORCHESTRATOR_TOKEN"},
    "service": {"MINDMAP_ORCHESTRATOR_TOKEN", "MINDMAP_RELAY_TOKEN"},
    "delegate": {"MINDMAP_ORCHESTRATOR_TOKEN", "MINDMAP_DELEGATE_TOKENS"},
    "notifier": {"MINDMAP_TASKBOARD_TOKEN"},
    "embedder": set(),
}


def test_each_container_carries_only_its_tokens():
    deps = deployments(render(FULL))
    assert set(deps) == set(EXPECTED_TOKENS)
    for component, dep in deps.items():
        spec = dep["spec"]["template"]["spec"]
        (container,) = spec["containers"]
        assert set(env_of(container)) & TOKENS == EXPECTED_TOKENS[component], component
        assert "MINDMAP_DATABASE_URL" in env_of(container)


def test_migrate_init_container_only_gets_the_database():
    for component in LISTENERS:
        dep = deployments(render(FULL))[component]
        (init,) = dep["spec"]["template"]["spec"]["initContainers"]
        assert set(env_of(init)) == {"MINDMAP_DATABASE_URL"}, component


def test_only_the_web_pod_is_behind_oauth2():
    docs = render(FULL)
    for component in ("service", "delegate", "notifier", "embedder"):
        assert not any(
            k.startswith("oauth2-proxy-admission/") for k in pod_annotations(docs, component)
        ), component
    assert pod_annotations(docs, "web")["oauth2-proxy-admission/enable"] == "true"


def test_each_listener_is_its_own_deployment_with_its_own_service():
    docs = render(FULL)
    deps = deployments(docs)
    for component, port in (("web", 8080), ("service", 8082), ("delegate", 8081)):
        (container,) = deps[component]["spec"]["template"]["spec"]["containers"]
        assert [p["containerPort"] for p in container["ports"]] == [port]
    for svc in of_kind(docs, "Service"):
        name = svc["metadata"]["name"].removeprefix("agents-mindmap-")
        assert svc["spec"]["selector"]["app.kubernetes.io/component"] == name
