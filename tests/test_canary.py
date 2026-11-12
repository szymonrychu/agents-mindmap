"""Secrets-model checklist: a relay token and note text planted in requests never reach a
response body, a log line, an event or (token) any table."""

import base64
import json
import logging
import re
from pathlib import Path

import httpx
import psycopg
import pytest
from conftest import fake_orchestrator, make_settings
from test_maps_api import MAP

from agents_mindmap.api.app import PREFIX, create_service_app, create_web_app
from agents_mindmap.projects import ProjectLookup

TOKEN = "CANARY-TOKEN-0123456789abcdef"
NOTE = "CANARY-NOTE-secret-looking-text"


def forms(value):
    raw = value.encode()
    return [value, base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()]


def all_cells(db_url):
    """Every text-like cell of every table, as text."""
    with psycopg.connect(db_url) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        ]
        for table in tables:
            for row in conn.execute(f'SELECT t::text FROM "{table}" t').fetchall():
                yield table, row[0]


async def test_canary_nowhere(db_url, pool, caplog):
    caplog.set_level(logging.DEBUG)
    service = create_service_app(make_settings(db_url, relay_token=TOKEN), pool)
    web = create_web_app(make_settings(db_url), pool)
    for app in (service, web):
        app.state.projects = ProjectLookup("http://o.invalid", "t", transport=fake_orchestrator())
    bodies: list[str] = []

    def client(app, headers):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
            base_url="http://test" + PREFIX,
            headers=headers,
        )

    async with (
        client(
            service,
            {"Authorization": f"Bearer {TOKEN}", "X-Orch-Session": "s1", "X-Orch-Projects": "p1"},
        ) as agent,
        client(web, {"X-Forwarded-Email": "alice@example.test"}) as user,
    ):

        async def call(c, method, path, **kw):
            response = await getattr(c, method)(path, **kw)
            bodies.append(response.text)
            return response

        made = (await call(agent, "post", "/maps", json=MAP)).json()
        node = (
            await call(
                agent,
                "post",
                f"/maps/{made['key']}/nodes",
                json={
                    "topic": NOTE,
                    "notes": NOTE,
                    "summary": NOTE,
                    "growth_areas": [NOTE],
                    "links": [{"ref_key": "T-1", "note": NOTE}],
                },
            )
        ).json()
        patched = await call(
            agent,
            "patch",
            f"/nodes/{node['key']}",
            json={"version": 1, "notes": NOTE + "2", "summary": NOTE},
        )
        assert patched.status_code == 200
        await call(agent, "post", f"/maps/{made['key']}/directions", json={"text": NOTE})
        await call(
            agent, "post", "/knowledge/notes", json={"topic": NOTE, "notes": NOTE, "summary": NOTE}
        )
        await call(
            agent, "post", "/edges", json={"src": node["key"], "dst": made["root"], "note": NOTE}
        )
        # error paths: scope, unknown key, validation, missing header
        await call(agent, "post", "/maps", json={**MAP, "project": "p2", "slug": NOTE[:20].lower()})
        await call(agent, "get", "/nodes/N-99999")
        await call(
            agent, "post", f"/maps/{made['key']}/nodes", json={"topic": NOTE, "CANARY-FIELD": 1}
        )
        await call(user, "patch", f"/nodes/{node['key']}", json={"version": 9, "topic": NOTE})
        await call(user, "get", "/maps")

    assert not any(form in "\n".join(bodies) for form in forms(TOKEN))
    logs = caplog.text
    assert not any(form in logs for form in forms(TOKEN))
    assert NOTE not in logs
    for table, cell in all_cells(db_url):
        assert not any(form in cell for form in forms(TOKEN)), table
        if table == "events":
            assert NOTE not in cell
    with psycopg.connect(db_url) as conn:
        kinds = {r[0] for r in conn.execute("SELECT kind FROM events")}
    assert {"growth_area_changed", "link_created", "direction_created", "edge_created"} <= kinds


ROOT = Path(__file__).resolve().parent.parent
ROUTES = [
    (name, method, path)
    for name in ("openapi.json", "openapi-service.json")
    for path, methods in json.loads((ROOT / name).read_text())["paths"].items()
    for method in methods
]


@pytest.mark.parametrize(("doc", "method", "path"), ROUTES)
async def test_canary_every_route(doc, method, path, db_url, pool, caplog):
    """Every path and method of both documents, called with caller-chosen text in the path,
    the query and the body and the relay token as bearer: neither marker is echoed back or
    logged. A route added later joins through its document."""
    caplog.set_level(logging.DEBUG)
    service = doc == "openapi-service.json"
    if service:
        app = create_service_app(make_settings(db_url, relay_token=TOKEN), pool)
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "X-Orch-Session": NOTE,
            "X-Orch-Projects": "p1",
        }
    else:
        app = create_web_app(make_settings(db_url), pool)
        headers = {"X-Forwarded-Email": "alice@example.test"}
    app.state.projects = ProjectLookup("http://o.invalid", "t", transport=fake_orchestrator())
    url = PREFIX + re.sub(r"\{[^}]+\}", NOTE, path.removeprefix(PREFIX))
    query = {
        name: NOTE
        for name in (
            "q",
            "project",
            "map",
            "status",
            "after",
            "after_id",
            "limit",
            "key",
            "depth",
            "kind",
            "scope",
            "mode",
        )
    }
    kwargs = {"params": query}
    if method in ("post", "patch", "put", "delete"):
        kwargs["json"] = {"topic": NOTE, "notes": NOTE, "text": NOTE, "CANARY-FIELD": NOTE}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://test",
        headers=headers,
    ) as client:
        response = await client.request(method.upper(), url, **kwargs)
    assert response.status_code < 500, response.status_code
    for marker in (TOKEN, NOTE):
        assert not any(form in response.text for form in forms(marker))
    # the test client's own request log carries the URL it was given: not the service's
    logs = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("http"))
    assert not any(form in logs for form in forms(TOKEN))
    assert NOTE not in logs
