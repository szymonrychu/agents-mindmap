import os

import pytest

from agents_mindmap import runtime
from agents_mindmap.cli import EXIT_CONFIG, build_parser, main

IMPLEMENTED = ("serve", "migrate")
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
    for name in ("DATABASE_URL", "ORCHESTRATOR_URL", "PUBLIC_BASE_URL", "RELAY_TOKEN"):
        assert f"MINDMAP_{name}" in err


def test_serve_runs_runtime_main(monkeypatch):
    set_env(monkeypatch)
    calls = []
    monkeypatch.setattr(runtime, "main", lambda s: calls.append(s.http_port) or 0)
    assert main(["serve"]) == 0
    assert calls == [8080]


def test_migrate_applies_and_reports(monkeypatch, empty_db_url, capsys):
    set_env(monkeypatch, empty_db_url)
    assert main(["migrate"]) == 0
    assert main(["migrate"]) == 0
    assert capsys.readouterr().err == ""
