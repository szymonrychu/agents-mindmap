"""The change feed: `GET /events` (paged JSON) and `GET /stream` (SSE) on web and delegate."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import httpx
import psycopg
import pytest
import uvicorn
from agents_common.db import listen
from conftest import DELEGATE_TOKEN, fake_orchestrator, make_settings
from test_maps_api import make_map

from agents_mindmap.api import stream
from agents_mindmap.api.app import PREFIX, create_delegate_app, create_service_app, create_web_app
from agents_mindmap.projects import ProjectLookup

STREAM = f"{PREFIX}/stream"
EVENTS = f"{PREFIX}/events"
NOTE = "CANARY-NOTE-secret-looking-text"


async def never_listen(url: str, channel: str) -> AsyncIterator[str]:
    """A LISTEN connection that is down: it never yields a notification."""
    await asyncio.Event().wait()
    yield ""


def with_projects(app):
    app.state.projects = ProjectLookup("http://o.invalid", "t", transport=fake_orchestrator())
    return app


@pytest.fixture
def web_app(db_url, pool):
    return with_projects(create_web_app(make_settings(db_url), pool, stream_poll_seconds=0.2))


@contextlib.asynccontextmanager
async def serve(app) -> AsyncIterator[str]:
    """Real uvicorn on an ephemeral port: ASGITransport cannot stream incrementally."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


class SSE:
    def __init__(self, resp: httpx.Response):
        self._lines = resp.aiter_lines()

    async def next(self, timeout: float = 10) -> dict:
        async def read() -> dict:
            frame: dict = {}
            async for line in self._lines:
                if line == "":
                    if frame:
                        return frame
                    continue
                if line.startswith(":"):
                    continue
                key, _, value = line.partition(":")
                frame[key] = value.removeprefix(" ")
            raise AssertionError("stream ended")

        return await asyncio.wait_for(read(), timeout)


@contextlib.asynccontextmanager
async def open_stream(
    app, base: str, *, headers=None, params=None, wait_subscribed: bool = True
) -> AsyncIterator[SSE]:
    async with (
        httpx.AsyncClient(base_url=base, timeout=15, headers=headers or {}) as c,
        c.stream("GET", STREAM, params=params or {}) as resp,
    ):
        assert resp.status_code == 200
        if wait_subscribed:
            for _ in range(200):
                if app.state.hub.subscriber_count() == 1:
                    break
                await asyncio.sleep(0.02)
            assert app.state.hub.subscriber_count() == 1
            await app.state.hub.ready()
        yield SSE(resp)


def last_event_id(db_url) -> int:
    with psycopg.connect(db_url) as conn:
        return conn.execute("SELECT coalesce(max(id), 0) FROM events").fetchone()[0]


async def no_more(sse: SSE) -> None:
    with pytest.raises(TimeoutError):
        await sse.next(timeout=0.8)


async def test_stream_emits_on_notify_and_poll(db_url, pool, web_client, web_app):
    made = await make_map(web_client)
    async with serve(web_app) as base, open_stream(web_app, base) as sse:
        r = await web_client.post(f"/maps/{made['key']}/nodes", json={"topic": "first"})
        assert r.status_code == 201
        node = r.json()["key"]
        frame = await sse.next()
        assert frame["event"] == "mindmap_changed"
        assert json.loads(frame["data"]) == {
            "project": "p1",
            "map": made["key"],
            "node": node,
            "kind": "node_created",
            "id": int(frame["id"]),
        }
        assert int(frame["id"]) == last_event_id(db_url)
        # the NOTIFY and the poll both saw the row: it must arrive once
        await no_more(sse)

    # NOTIFY down: the poll alone delivers
    app = with_projects(
        create_web_app(make_settings(db_url), pool, stream_poll_seconds=0.2, listen_fn=never_listen)
    )
    async with serve(app) as base, open_stream(app, base) as sse:
        await web_client.patch(f"/maps/{made['key']}", json={"version": 1, "title": "renamed"})
        frame = await sse.next()
        assert json.loads(frame["data"])["map"] == made["key"]


async def test_stream_payload_has_no_note_text(web_client, web_app):
    made = await make_map(web_client)
    async with serve(web_app) as base, open_stream(web_app, base) as sse:
        await web_client.post(
            f"/maps/{made['key']}/nodes",
            json={"topic": NOTE, "notes": NOTE, "summary": NOTE, "growth_areas": [NOTE]},
        )
        frame = await sse.next()
        assert NOTE not in frame["data"]
        assert set(json.loads(frame["data"])) == {"project", "map", "node", "kind", "id"}


async def test_stream_project_filter(web_client, web_app):
    one = await make_map(web_client)
    two = await make_map(web_client, project="p2", slug="two", title="two")
    async with (
        serve(web_app) as base,
        httpx.AsyncClient(base_url=base, timeout=15) as c,
        c.stream("GET", STREAM, params={"project": "p2"}) as resp,
    ):
        for _ in range(200):
            if web_app.state.hub.subscriber_count() == 1:
                break
            await asyncio.sleep(0.02)
        await web_app.state.hub.ready()
        sse = SSE(resp)
        for made in (one, two):
            await web_client.patch(f"/maps/{made['key']}", json={"version": 1, "title": "x"})
        frame = await sse.next()
        assert json.loads(frame["data"])["map"] == two["key"]
        await no_more(sse)


async def test_last_event_id_replays_once_per_node(db_url, web_client, web_app):
    made = await make_map(web_client)
    node = (await web_client.post(f"/maps/{made['key']}/nodes", json={"topic": "a"})).json()
    mark = last_event_id(db_url)
    for step, status in enumerate(("growing", "settled")):
        r = await web_client.patch(
            f"/nodes/{node['key']}", json={"version": node["version"] + step, "status": status}
        )
        assert r.status_code == 200, r.text
    async with (
        serve(web_app) as base,
        open_stream(web_app, base, headers={"Last-Event-ID": str(mark)}) as sse,
    ):
        frame = await sse.next()
        assert json.loads(frame["data"])["node"] == node["key"]
        assert int(frame["id"]) == last_event_id(db_url)
        await no_more(sse)


async def test_after_id_query_and_larger_cursor_wins(db_url, web_client, web_app):
    made = await make_map(web_client)
    first = last_event_id(db_url)
    await web_client.post(f"/maps/{made['key']}/nodes", json={"topic": "a"})
    second = last_event_id(db_url)
    async with (
        serve(web_app) as base,
        open_stream(
            web_app, base, params={"after_id": first}, headers={"Last-Event-ID": str(second)}
        ) as sse,
    ):
        await no_more(sse)  # the header (larger) already covers every row
    async with (
        serve(web_app) as base,
        open_stream(web_app, base, params={"after_id": first}) as sse,
    ):
        assert int((await sse.next())["id"]) == second


async def test_hub_stops_with_last_subscriber_and_on_shutdown(web_app):
    hub = web_app.state.hub
    assert not hub.running
    async with serve(web_app) as base:
        async with open_stream(web_app, base):
            assert hub.running
        for _ in range(200):
            if not hub.running:
                break
            await asyncio.sleep(0.02)
        assert not hub.running
        async with open_stream(web_app, base):
            assert hub.running
    assert not hub.running


async def test_listener_restarts_after_unexpected_error(db_url, pool, web_client, monkeypatch):
    monkeypatch.setattr(stream, "LISTEN_RETRY_SECONDS", 0.05)
    calls = []

    async def flaky_listen(url: str, channel: str) -> AsyncIterator[str]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        async for payload in listen(url, channel):
            yield payload

    # poll far slower than the test waits, so only a live listener can deliver
    app = with_projects(
        create_web_app(make_settings(db_url), pool, stream_poll_seconds=30, listen_fn=flaky_listen)
    )
    made = await make_map(web_client)
    async with serve(app) as base, open_stream(app, base) as sse:
        await asyncio.sleep(0.5)
        assert len(calls) >= 2
        await web_client.patch(f"/maps/{made['key']}", json={"version": 1, "title": "x"})
        assert json.loads((await sse.next())["data"])["map"] == made["key"]


async def test_service_app_serves_neither_events_nor_stream(settings, pool):
    app = create_service_app(settings, pool)
    assert not hasattr(app.state, "hub")
    for path in (STREAM, EVENTS):
        assert all(getattr(r, "path", "") != path for r in app.routes)


async def test_delegate_app_serves_the_stream(db_url, pool):
    app = create_delegate_app(make_settings(db_url), pool)
    assert app.state.hub is not None
    async with serve(app) as base, httpx.AsyncClient(base_url=base, timeout=15) as c:
        assert (await c.get(EVENTS)).status_code == 401
        ok = await c.get(EVENTS, headers={"Authorization": f"Bearer {DELEGATE_TOKEN}"})
        assert ok.status_code == 200


async def test_events_route_pages_after_id(db_url, web_client):
    made = await make_map(web_client)
    other = await make_map(web_client, project="p2", slug="two", title="two")
    for _ in range(2):
        await web_client.post(
            f"/maps/{made['key']}/nodes", json={"topic": NOTE, "notes": NOTE, "summary": NOTE}
        )
    everything = (await web_client.get("/events")).json()
    ids = [e["id"] for e in everything]
    assert ids == sorted(ids) and len(ids) >= 4
    assert set(everything[0]) == {"id", "project", "map", "node", "kind", "actor", "at", "detail"}
    assert NOTE not in json.dumps(everything)
    only = (await web_client.get("/events", params={"project": "p2"})).json()
    assert {e["project"] for e in only} == {"p2"}
    assert {e["map"] for e in only} == {other["key"]}
    tail = (await web_client.get("/events", params={"after": ids[0], "limit": 2})).json()
    assert [e["id"] for e in tail] == ids[1:3]
    assert (await web_client.get("/events", params={"after": ids[-1]})).json() == []
    assert (await web_client.get("/events", params={"limit": 0})).status_code == 422
    assert (await web_client.get("/events", params={"after": -1})).status_code == 422
    assert (await web_client.get("/events", params={"project": "BAD KEY"})).status_code == 422


async def test_stream_rejects_bad_cursor(web_client):
    r = await web_client.get("/stream", params={"after_id": -1})
    assert r.status_code == 422
    assert set(r.json()) == {"error"}
