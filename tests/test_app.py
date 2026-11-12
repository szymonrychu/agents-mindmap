"""Probes, admission gates and the error body shape on the three listeners."""

import json

import httpx
import pytest
from conftest import DELEGATE_TOKEN, RELAY_TOKEN

from agents_mindmap.api.app import (
    PREFIX,
    create_delegate_app,
    create_service_app,
    create_web_app,
)
from agents_mindmap.api.errors import ApiError, error_response

FACTORIES = {
    "web": create_web_app,
    "delegate": create_delegate_app,
    "service": create_service_app,
}
TOKENS = {"web": None, "delegate": DELEGATE_TOKEN, "service": RELAY_TOKEN}


def client(app, peer: str = "127.0.0.1", token: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 1)),
        base_url="http://test",
        headers=headers,
    )


@pytest.mark.parametrize("listener", list(FACTORIES))
async def test_healthz_all_listeners(settings, listener):
    app = FACTORIES[listener](settings, None)
    async with client(app, peer="10.0.0.1") as c:
        response = await c.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("listener", list(FACTORIES))
async def test_readyz_without_database_is_503(settings, listener):
    app = FACTORIES[listener](settings, None)
    async with client(app, peer="10.0.0.1") as c:
        response = await c.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "not_ready", "message": "database is not reachable"}
    }


async def test_web_forbids_peer_outside_allowlist(settings):
    app = create_web_app(settings, None)
    async with client(app, "10.0.0.1") as c:
        denied = await c.get(f"{PREFIX}/maps")
    assert denied.status_code == 403
    assert denied.json() == {
        "error": {"code": "forbidden", "message": "source address is not allowed"}
    }
    async with client(app, "127.0.0.1") as c:
        assert (await c.get(f"{PREFIX}/nothing-here")).status_code == 404


@pytest.mark.parametrize(
    ("listener", "wrong"),
    [("delegate", RELAY_TOKEN), ("service", DELEGATE_TOKEN)],
)
async def test_bearer_gates_take_only_their_own_tokens(settings, listener, wrong):
    app = FACTORIES[listener](settings, None)
    for token in (None, "nope", wrong):
        async with client(app, "127.0.0.1", token) as c:
            response = await c.get(f"{PREFIX}/maps")
        assert response.status_code == 401, token
        assert response.json()["error"]["code"] == "unauthorized"
    async with client(app, "10.0.0.1", TOKENS[listener]) as c:
        assert (await c.get(f"{PREFIX}/nothing-here")).status_code == 404


def test_error_body_shape():
    response = error_response(409, "version_conflict", "the node changed", {"current_version": 3})
    assert response.status_code == 409
    assert json.loads(response.body) == {
        "error": {"code": "version_conflict", "message": "the node changed", "current_version": 3}
    }


async def test_api_error_renders_error_body(settings):
    app = create_web_app(settings, None)

    @app.get(f"{PREFIX}/boom")
    async def boom() -> None:
        raise ApiError(422, "parent_cycle", "the new parent is a descendant of the node")

    async with client(app) as c:
        response = await c.get(f"{PREFIX}/boom")
        missing = await c.get(f"{PREFIX}/nothing-here")
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "parent_cycle", "message": "the new parent is a descendant of the node"}
    }
    assert missing.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_delegate_app_without_tokens_is_refused(settings):
    with pytest.raises(ValueError, match="MINDMAP_DELEGATE_TOKENS"):
        create_delegate_app(settings.model_copy(update={"delegate_tokens": []}), None)
