"""Chart skeleton render: one api Deployment, listener Services, external Secret refs only."""

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


def api_container(docs: list[dict]) -> dict:
    (dep,) = of_kind(docs, "Deployment")
    return dep["spec"]["template"]["spec"]["containers"][0]


def env_of(container: dict) -> dict:
    return {e["name"]: e for e in container.get("env", [])}


def test_chart_skeleton_renders():
    docs = render()
    kinds = sorted(d["kind"] for d in docs)
    assert kinds == ["ConfigMap", "Deployment", "Pod", "Service", "Service", "ServiceAccount"]
    (hook,) = of_kind(docs, "Pod")
    assert hook["metadata"]["annotations"]["helm.sh/hook"] == "test"
    (dep,) = of_kind(docs, "Deployment")
    assert dep["metadata"]["name"] == "agents-mindmap-api"
    spec = dep["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    container = spec["containers"][0]
    assert container["command"] == ["mindmap"] and container["args"] == ["serve"]
    assert container["image"] == "harbor.szymonrichert.pl/containers/agents-mindmap:1.2.3"
    assert spec["initContainers"][0]["args"] == ["migrate"]
    ports = {p["name"]: p["containerPort"] for p in container["ports"]}
    assert ports == {"http": 8080, "service": 8082}
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
    container = api_container(docs)
    assert {"name": "delegate", "containerPort": 8081, "protocol": "TCP"} in container["ports"]
    assert env_of(container)["MINDMAP_DELEGATE_TOKENS"]["valueFrom"]["secretKeyRef"] == {
        "name": "mm-delegate",
        "key": "tokens",
    }


def test_secret_values_come_from_external_secrets_only():
    docs = render({"taskboard": {"url": "http://tb:8003", "tokenSecret": {"name": "mm-tb"}}})
    assert not of_kind(docs, "Secret")
    env = env_of(api_container(docs))
    refs = {k: v["valueFrom"]["secretKeyRef"] for k, v in env.items()}
    assert refs == {
        "MINDMAP_DATABASE_URL": {"name": "agents-mindmap-db-app", "key": "uri"},
        "MINDMAP_ORCHESTRATOR_TOKEN": {"name": "mm-orch", "key": "token"},
        "MINDMAP_RELAY_TOKEN": {"name": "mm-relay", "key": "token"},
        "MINDMAP_TASKBOARD_TOKEN": {"name": "mm-tb", "key": "token"},
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
