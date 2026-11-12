import os
from pathlib import Path

import pytest

from agents_mindmap import runtime
from agents_mindmap.cli import EXIT_CONFIG, build_parser, main

IMPLEMENTED = ("serve-web", "serve-service", "serve-delegate", "migrate")
LATER = ("notifier", "embedder", "reembed")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("MINDMAP_"):
            monkeypatch.delenv(key)


def set_env(monkeypatch, database_url: str = "postgresql://u:p@db.invalid/mindmap") -> None:
    for key, value in {
        "DATABASE_URL": database_url,
        "ORCHESTRATOR_URL": "http://o.invalid",
        "ORCHESTRATOR_TOKEN": "orch-value",
        "PUBLIC_BASE_URL": "https://lab.example.test",
        "RELAY_TOKEN": "relay-value",
    }.items():
        monkeypatch.setenv(f"MINDMAP_{key}", value)


def test_cli_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: mindmap")
    for name in IMPLEMENTED:
        assert name in out
    # not shipped until implemented (Tasks 13 and 21): no stub subcommand in a release
    for name in LATER:
        assert name not in out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("mindmap ")


def test_no_command_exits_2():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


@pytest.mark.parametrize("command", IMPLEMENTED)
def test_bad_config_exits_78_naming_variables(command, capsys):
    assert main([command]) == EXIT_CONFIG
    err = capsys.readouterr().err
    for name in ("DATABASE_URL", "ORCHESTRATOR_URL", "PUBLIC_BASE_URL"):
        assert f"MINDMAP_{name}" in err


def test_serve_web_runs_without_relay_or_delegate_tokens(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.delenv("MINDMAP_RELAY_TOKEN")
    calls = []
    monkeypatch.setattr(runtime, "main", lambda s, listener: calls.append(listener) or 0)
    assert main(["serve-web"]) == 0
    assert calls == ["web"]


def test_serve_service_needs_the_relay_token(monkeypatch, capsys):
    set_env(monkeypatch)
    calls = []
    monkeypatch.setattr(runtime, "main", lambda s, listener: calls.append(listener) or 0)
    assert main(["serve-service"]) == 0
    assert calls == ["service"]
    monkeypatch.delenv("MINDMAP_RELAY_TOKEN")
    assert main(["serve-service"]) == EXIT_CONFIG
    assert "MINDMAP_RELAY_TOKEN" in capsys.readouterr().err
    assert calls == ["service"]


def test_serve_delegate_needs_delegate_tokens(monkeypatch, capsys):
    set_env(monkeypatch)
    monkeypatch.delenv("MINDMAP_RELAY_TOKEN")
    calls = []
    monkeypatch.setattr(runtime, "main", lambda s, listener: calls.append(listener) or 0)
    assert main(["serve-delegate"]) == EXIT_CONFIG
    assert "MINDMAP_DELEGATE_TOKENS" in capsys.readouterr().err
    monkeypatch.setenv("MINDMAP_DELEGATE_TOKENS", "delegate-value")
    assert main(["serve-delegate"]) == 0
    assert calls == ["delegate"]


def test_migrate_applies_and_reports(monkeypatch, empty_db_url, capsys):
    set_env(monkeypatch, empty_db_url)
    assert main(["migrate"]) == 0
    assert main(["migrate"]) == 0
    assert capsys.readouterr().err == ""


def registered() -> list[str]:
    parser = build_parser()
    (sub,) = [a for a in parser._actions if a.dest == "command"]
    return list(sub.choices)


def test_cli_no_unimplemented_commands(capsys):
    names = registered()
    assert set(names) == set(IMPLEMENTED)
    for name in names:
        with pytest.raises(SystemExit) as exc:
            main([name, "--help"])
        assert exc.value.code == 0
        assert capsys.readouterr().out.startswith(f"usage: mindmap {name}")
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    for name in LATER:
        assert f"`mindmap {name}`" not in readme
