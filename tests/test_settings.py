import logging
import os

import pytest
from agents_common.settings import SettingsError

from agents_mindmap.settings import EMBED_DIM, EMBED_MODEL, Settings, load_settings

CANARY = "CANARY-7f3c9a"

REQUIRED = {
    "MINDMAP_DATABASE_URL": "postgresql://u:p@db/mindmap",
    "MINDMAP_ORCHESTRATOR_URL": "http://agents-orchestrator-services:8002",
    "MINDMAP_ORCHESTRATOR_TOKEN": "orch-value",
    "MINDMAP_PUBLIC_BASE_URL": "https://lab.example.test/",
    "MINDMAP_RELAY_TOKEN": "relay-value",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("MINDMAP_"):
            monkeypatch.delenv(key)


def set_env(monkeypatch, **extra):
    for key, value in {**REQUIRED, **extra}.items():
        monkeypatch.setenv(key, value)


def problems_of(monkeypatch, **extra) -> SettingsError:
    set_env(monkeypatch, **extra)
    with pytest.raises(SettingsError) as exc:
        load_settings()
    return exc.value


def test_defaults(monkeypatch):
    set_env(monkeypatch)
    s = load_settings()
    assert isinstance(s, Settings)
    assert s.allowed_cidrs == ["127.0.0.1/32"]
    assert (s.http_port, s.delegate_port, s.service_port) == (8080, 8081, 8082)
    assert s.public_base_url == "https://lab.example.test"
    assert s.delegate_tokens == []
    assert s.taskboard_url == "" and s.embedder_url == ""
    assert s.embedder_model == EMBED_MODEL == "intfloat/multilingual-e5-large-instruct"
    assert s.embed_dim == EMBED_DIM == 1024
    assert s.notice_debounce_seconds == 60.0
    assert s.notifier_poll_seconds == 5.0
    assert s.notifier_backoff_max_seconds == 60.0
    assert s.idle_in_transaction_timeout_ms == 30000


def test_errors_name_each_variable(monkeypatch):
    set_env(monkeypatch, MINDMAP_ALLOWED_CIDRS="nope", MINDMAP_PUBLIC_BASE_URL="not-a-url")
    monkeypatch.delenv("MINDMAP_RELAY_TOKEN")
    with pytest.raises(SettingsError) as exc:
        load_settings()
    assert set(exc.value.problems) == {
        "MINDMAP_ALLOWED_CIDRS",
        "MINDMAP_PUBLIC_BASE_URL",
        "MINDMAP_RELAY_TOKEN",
    }


def test_delegate_tokens_parsed_from_csv(monkeypatch):
    set_env(monkeypatch, MINDMAP_DELEGATE_TOKENS="a, ,b,")
    s = load_settings()
    assert [t.get_secret_value() for t in s.delegate_tokens] == ["a", "b"]


def test_settings_tokens_distinct(monkeypatch):
    err = problems_of(
        monkeypatch,
        MINDMAP_RELAY_TOKEN=CANARY,
        MINDMAP_TASKBOARD_URL="http://agents-taskboard-delegate:8003",
        MINDMAP_TASKBOARD_TOKEN=CANARY,
    )
    assert set(err.problems) == {"MINDMAP_TASKBOARD_TOKEN"}
    assert "MINDMAP_RELAY_TOKEN" in str(err)
    assert CANARY not in str(err)


@pytest.mark.parametrize(
    ("extra", "names"),
    [
        ({"MINDMAP_DELEGATE_TOKENS": "x,relay-value"}, {"MINDMAP_DELEGATE_TOKENS"}),
        ({"MINDMAP_DELEGATE_TOKENS": "x,y,x"}, {"MINDMAP_DELEGATE_TOKENS"}),
        ({"MINDMAP_DELEGATE_TOKENS": "orch-value"}, {"MINDMAP_DELEGATE_TOKENS"}),
        ({"MINDMAP_ORCHESTRATOR_TOKEN": "relay-value"}, {"MINDMAP_ORCHESTRATOR_TOKEN"}),
        (
            {"MINDMAP_TASKBOARD_URL": "http://t", "MINDMAP_TASKBOARD_TOKEN": "orch-value"},
            {"MINDMAP_TASKBOARD_TOKEN"},
        ),
        (
            {
                "MINDMAP_DELEGATE_TOKENS": "d",
                "MINDMAP_TASKBOARD_URL": "http://t",
                "MINDMAP_TASKBOARD_TOKEN": "d",
            },
            {"MINDMAP_TASKBOARD_TOKEN"},
        ),
    ],
)
def test_every_token_pair_must_differ(monkeypatch, extra, names):
    err = problems_of(monkeypatch, **extra)
    assert set(err.problems) == names
    for value in ("relay-value", "orch-value"):
        assert value not in str(err)


def test_taskboard_token_required_with_url(monkeypatch):
    err = problems_of(monkeypatch, MINDMAP_TASKBOARD_URL="http://agents-taskboard-delegate:8003")
    assert set(err.problems) == {"MINDMAP_TASKBOARD_TOKEN"}


@pytest.mark.parametrize("env", ["MINDMAP_TASKBOARD_URL", "MINDMAP_EMBEDDER_URL"])
def test_optional_urls_must_be_absolute(monkeypatch, env):
    err = problems_of(monkeypatch, **{env: "nope", "MINDMAP_TASKBOARD_TOKEN": "tb"})
    assert set(err.problems) == {env}


@pytest.mark.parametrize(
    ("env", "port"),
    [("MINDMAP_DELEGATE_PORT", "8080"), ("MINDMAP_SERVICE_PORT", "8081")],
)
def test_ports_distinct(monkeypatch, env, port):
    err = problems_of(monkeypatch, **{env: port})
    assert set(err.problems) == {env}
    assert "must differ" in err.problems[env]


@pytest.mark.parametrize("value", ["0", "65536"])
def test_port_range(monkeypatch, value):
    err = problems_of(monkeypatch, MINDMAP_SERVICE_PORT=value)
    assert set(err.problems) == {"MINDMAP_SERVICE_PORT"}


def test_settings_repr_hides_tokens(monkeypatch, caplog):
    set_env(
        monkeypatch,
        MINDMAP_RELAY_TOKEN=f"{CANARY}-relay",
        MINDMAP_ORCHESTRATOR_TOKEN=f"{CANARY}-orch",
        MINDMAP_DELEGATE_TOKENS=f"{CANARY}-d1,{CANARY}-d2",
        MINDMAP_TASKBOARD_URL="http://agents-taskboard-delegate:8003",
        MINDMAP_TASKBOARD_TOKEN=f"{CANARY}-tb",
        MINDMAP_DATABASE_URL=f"postgresql://u:{CANARY}@db/mindmap",
    )
    caplog.set_level(logging.DEBUG)
    s = load_settings()
    logging.getLogger("agents_mindmap.test").info("settings %r %s", s, s)
    assert CANARY not in repr(s)
    assert CANARY not in str(s)
    assert CANARY not in caplog.text
    assert s.relay_token.get_secret_value() == f"{CANARY}-relay"
