"""The committed OpenAPI documents equal what the apps export."""

from pathlib import Path

import pytest

from agents_mindmap.api.openapi import export

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("name", "service"), [("openapi.json", False), ("openapi-service.json", True)]
)
def test_openapi_current(name: str, service: bool):
    committed = (ROOT / name).read_text()
    assert committed == export(service), (
        f"{name} is stale: uv run python -m agents_mindmap.api.openapi"
        f"{' --service' if service else ''} > {name}"
    )


def test_feed_routes_are_web_only():
    import json

    web = json.loads((ROOT / "openapi.json").read_text())["paths"]
    service = json.loads((ROOT / "openapi-service.json").read_text())["paths"]
    prefix = "/mindmap/api/v1"
    assert {f"{prefix}/events", f"{prefix}/stream"} <= set(web)
    assert not {f"{prefix}/events", f"{prefix}/stream"} & set(service)
