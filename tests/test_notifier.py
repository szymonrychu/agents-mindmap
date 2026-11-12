"""The notifier: task notices to the taskboard, direction fallback to project sessions."""

import asyncio
import json

import httpx
import psycopg
import pytest
from agents_common.orchestrator import OrchestratorClient
from conftest import make_settings
from test_maps_api import make_map

from agents_mindmap import notifier
from agents_mindmap.notifier import SessionLookup, TaskboardClient

TB_TOKEN = "taskboard-fixture"


class FakeTaskboard:
    """Records calls; `plan` maps a task key to a status code (default 200)."""

    def __init__(self, plan=None, on_call=None):
        self.calls = []
        self.plan = plan or {}
        self.on_call = on_call

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if self.on_call:
                self.on_call()
            self.calls.append(
                (
                    request.url.path,
                    request.headers.get("authorization"),
                    json.loads(request.content),
                )
            )
            key = request.url.path.split("/")[-2]
            return httpx.Response(self.plan.get(key, 200), json={})

        return httpx.MockTransport(handler)

    def client(self) -> TaskboardClient:
        return TaskboardClient("http://tb.invalid", TB_TOKEN, transport=self.transport())


async def linked(web_client, *refs, slug="m"):
    m = await make_map(web_client, slug=slug, title=slug)
    for ref in refs:
        await web_client.post(f"/maps/{m['key']}/links", json={"ref_key": ref})
    return m


def due_now(db_url):
    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE task_notices SET due_at = now() - interval '1 second'")
        conn.commit()


def notice_rows(db_url):
    with psycopg.connect(db_url) as conn:
        return conn.execute(
            "SELECT task_key, status, attempts FROM task_notices ORDER BY task_key, id"
        ).fetchall()


async def drain(pool, tb, **kw):
    while await notifier.send_next_task(pool, tb.client(), 1.0, 60.0, **kw) is not None:
        pass


async def test_notifier_body(web_client, pool, db_url):
    m = await linked(web_client, "T-3")
    await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})
    due_now(db_url)
    tb = FakeTaskboard()
    await drain(pool, tb)
    assert tb.calls == [
        (
            "/taskboard/api/v1/tasks/T-3/external-changes",
            f"Bearer {TB_TOKEN}",
            {"source": "mindmap", "ref": m["key"], "actor_session": None},
        )
    ]
    assert notice_rows(db_url) == [("T-3", "sent", 0)]


async def test_not_due_is_not_sent(web_client, pool):
    await linked(web_client, "T-3")
    tb = FakeTaskboard()
    assert await notifier.send_next_task(pool, tb.client(), 1.0, 60.0) is None
    assert tb.calls == []


async def test_burst_calls_each_task_once(web_client, pool, db_url):
    m = await linked(web_client, "T-1", "T-2")
    for i in range(50):
        await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": f"n{i}"})
    tb = FakeTaskboard()
    await drain(pool, tb)
    assert tb.calls == []  # still inside the debounce
    due_now(db_url)
    await drain(pool, tb)
    assert sorted(c[0] for c in tb.calls) == [
        "/taskboard/api/v1/tasks/T-1/external-changes",
        "/taskboard/api/v1/tasks/T-2/external-changes",
    ]


async def test_unknown_task_dead(web_client, pool, db_url):
    m = await linked(web_client, "T-9")
    await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})
    due_now(db_url)
    tb = FakeTaskboard({"T-9": 404})
    await drain(pool, tb)
    await drain(pool, tb)
    assert len(tb.calls) == 1
    assert notice_rows(db_url) == [("T-9", "dead", 0)]


@pytest.mark.parametrize("code", [400, 401, 403, 422])
async def test_permanent_client_error_dead(web_client, pool, db_url, code):
    m = await linked(web_client, "T-9")
    await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})
    due_now(db_url)
    tb = FakeTaskboard({"T-9": code})
    assert await notifier.send_next_task(pool, tb.client(), 30.0, 60.0) is True
    assert await notifier.send_next_task(pool, tb.client(), 30.0, 60.0) is None
    assert len(tb.calls) == 1
    assert notice_rows(db_url) == [("T-9", "dead", 0)]


async def test_429_retries(web_client, pool, db_url):
    m = await linked(web_client, "T-1")
    await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})
    due_now(db_url)
    tb = FakeTaskboard({"T-1": 429})
    assert await notifier.send_next_task(pool, tb.client(), 30.0, 60.0) is False
    assert notice_rows(db_url) == [("T-1", "pending", 1)]


async def test_5xx_backoff_retry(web_client, pool, db_url):
    m = await linked(web_client, "T-1")
    await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})
    due_now(db_url)
    tb = FakeTaskboard({"T-1": 503})
    assert await notifier.send_next_task(pool, tb.client(), 30.0, 60.0) is False
    (row,) = notice_rows(db_url)
    assert row == ("T-1", "pending", 1)
    with psycopg.connect(db_url) as conn:
        wait = conn.execute(
            "SELECT extract(epoch FROM next_attempt_at - now()) FROM task_notices"
        ).fetchone()[0]
    assert 20 < wait <= 31
    assert await notifier.send_next_task(pool, tb.client(), 30.0, 60.0) is None  # backing off
    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE task_notices SET next_attempt_at = now()")
        conn.commit()
    tb.plan = {}
    assert await notifier.send_next_task(pool, tb.client(), 30.0, 60.0) is True
    assert notice_rows(db_url) == [("T-1", "sent", 1)]


async def test_network_error_retries(web_client, pool, db_url):
    m = await linked(web_client, "T-1")
    await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})
    due_now(db_url)

    def boom(request):
        raise httpx.ConnectError("down")

    tb = TaskboardClient("http://tb.invalid", TB_TOKEN, transport=httpx.MockTransport(boom))
    assert await notifier.send_next_task(pool, tb, 1.0, 60.0) is False
    assert notice_rows(db_url) == [("T-1", "pending", 1)]


async def test_at_least_once_after_crash(web_client, pool, db_url):
    m = await linked(web_client, "T-1")
    await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})
    due_now(db_url)

    class Crash(BaseException):
        pass

    def die():
        raise Crash

    first = FakeTaskboard(on_call=die)
    with pytest.raises(Crash):
        await notifier.send_next_task(pool, first.client(), 1.0, 60.0)
    assert notice_rows(db_url) == [("T-1", "pending", 0)]  # never marked
    second = FakeTaskboard()
    await drain(pool, second)
    assert len(second.calls) == 1
    assert notice_rows(db_url) == [("T-1", "sent", 0)]


async def test_change_during_send_is_not_lost(web_client, pool, db_url):
    m = await linked(web_client, "T-1")
    await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})
    due_now(db_url)
    loop = asyncio.get_running_loop()

    def change_midway():
        # runs inside the (synchronous) fake handler: a writer commits while the call is out
        with psycopg.connect(db_url) as conn:
            conn.execute(
                "UPDATE task_notices SET changes = changes + 1, actor_session = NULL"
                " WHERE status = 'pending'"
            )
            conn.commit()

    tb = FakeTaskboard(on_call=change_midway)
    assert loop is not None
    await notifier.send_next_task(pool, tb.client(), 1.0, 60.0)
    assert notice_rows(db_url) == [("T-1", "pending", 0)]  # sent flag refused; resent next time
    tb.on_call = None
    await drain(pool, tb)
    assert notice_rows(db_url) == [("T-1", "sent", 0)]
    assert len(tb.calls) == 2


async def test_actor_session_in_body(web_client, agent, pool, db_url):
    m = await linked(web_client, "T-1")
    with psycopg.connect(db_url) as conn:
        conn.execute("DELETE FROM task_notices")
        conn.commit()
    r = await agent("p1", session="7d9c1e0a-0000-4000-8000-000000000007").post(
        f"/maps/{m['key']}/nodes", json={"topic": "from agent"}
    )
    assert r.status_code == 201, r.text
    due_now(db_url)
    tb = FakeTaskboard()
    await drain(pool, tb)
    assert tb.calls[0][2]["actor_session"] == "7d9c1e0a-0000-4000-8000-000000000007"


async def test_non_uuid_actor_sent_as_null(web_client, agent, pool, db_url):
    m = await linked(web_client, "T-1")
    with psycopg.connect(db_url) as conn:
        conn.execute("DELETE FROM task_notices")
        conn.commit()
    await agent("p1", session="not-a-uuid").post(f"/maps/{m['key']}/nodes", json={"topic": "x"})
    due_now(db_url)
    tb = FakeTaskboard()
    await drain(pool, tb)
    assert tb.calls[0][2]["actor_session"] is None


async def test_no_tx_across_http(web_client, pool, db_url):
    m = await linked(web_client, "T-1")
    await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})
    due_now(db_url)
    seen = {}

    def inspect():
        with psycopg.connect(db_url) as other:
            seen["idle_in_tx"] = other.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE state LIKE 'idle in transaction%'"
                " AND datname = current_database()"
            ).fetchone()[0]
            # the row is not locked: a writer can take it without waiting
            seen["lock"] = other.execute(
                "SELECT count(*) FROM (SELECT 1 FROM task_notices FOR UPDATE NOWAIT) s"
            ).fetchone()[0]

    tb = FakeTaskboard(on_call=inspect)
    await drain(pool, tb)
    assert seen == {"idle_in_tx": 0, "lock": 1}


# --- direction fallback ---------------------------------------------------------------------


def fake_sessions(sessions, posted, gone=()):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/sessions":
            handler.query = dict(request.url.params.multi_items())
            return httpx.Response(200, json=sessions)
        if request.method == "POST" and path.endswith("/messages"):
            sid = path.split("/")[-2]
            if sid in gone:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            posted.append((sid, json.loads(request.content)["text"]))
            return httpx.Response(202, json={"message_id": 1, "status": "queued"})
        return httpx.Response(500)

    return handler


def orchestrator(handler):
    transport = httpx.MockTransport(handler)
    lookup = SessionLookup("http://orch.invalid", "t", transport=transport)
    client = OrchestratorClient("http://orch.invalid", "t", transport=transport)
    return lookup, client


SESSIONS = [
    {"id": "11111111-1111-4111-8111-111111111111", "desired": "active", "labels": ["a"]},
    {"id": "22222222-2222-4222-8222-222222222222", "desired": "active", "labels": []},
    {"id": "33333333-3333-4333-8333-333333333333", "desired": "stopped", "labels": []},
]


async def test_direction_fallback_content_free(web_client, pool, db_url):
    m = await make_map(web_client)
    secret_text = "CANARY-direction-text"
    r = await web_client.post(f"/maps/{m['key']}/directions", json={"text": secret_text})
    did = r.json()["key"]
    posted = []
    handler = fake_sessions(SESSIONS, posted)
    lookup, client = orchestrator(handler)
    done = await notifier.send_next_direction(
        pool, lookup, client, "https://lab.example.test/", 1.0, 60.0
    )
    assert done is True
    expected = (
        f"Mind map {m['key']} got direction {did}: "
        f"https://lab.example.test/mindmaps/{m['key']}?direction={did}"
    )
    assert posted == [
        ("11111111-1111-4111-8111-111111111111", expected),
        ("22222222-2222-4222-8222-222222222222", expected),
    ]
    assert secret_text not in json.dumps(posted)
    assert handler.query == {"project": "p1", "superleaders": "include"}
    assert await notifier.send_next_direction(pool, lookup, client, "https://x", 1.0, 60.0) is None
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT status FROM direction_notices").fetchone()[0] == "sent"


async def test_direction_fallback_session_gone_and_none_live(web_client, pool, db_url):
    m = await make_map(web_client)
    await web_client.post(f"/maps/{m['key']}/directions", json={"text": "x"})
    posted = []
    lookup, client = orchestrator(fake_sessions(SESSIONS, posted, gone={SESSIONS[0]["id"]}))
    assert await notifier.send_next_direction(pool, lookup, client, "https://x", 1.0, 60.0)
    assert [p[0] for p in posted] == [SESSIONS[1]["id"]]
    await web_client.post(f"/maps/{m['key']}/directions", json={"text": "y"})
    lookup, client = orchestrator(fake_sessions([], []))
    assert await notifier.send_next_direction(pool, lookup, client, "https://x", 1.0, 60.0)
    with psycopg.connect(db_url) as conn:
        assert {r[0] for r in conn.execute("SELECT status FROM direction_notices")} == {"sent"}


async def test_direction_fallback_retries_on_lookup_failure(web_client, pool, db_url):
    m = await make_map(web_client)
    await web_client.post(f"/maps/{m['key']}/directions", json={"text": "x"})

    def handler(request):
        return httpx.Response(503)

    lookup, client = orchestrator(handler)
    assert await notifier.send_next_direction(pool, lookup, client, "https://x", 1.0, 60.0) is False
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT status, attempts FROM direction_notices").fetchone() == (
            "pending",
            1,
        )


# --- process ----------------------------------------------------------------------------------


async def test_notifier_off_without_taskboard_url(db_url):
    settings = make_settings(db_url, taskboard_url="", taskboard_token="")
    stop = asyncio.Event()
    await asyncio.wait_for(notifier.serve(settings, stop), timeout=5)  # returns, never loops


def test_cli_notifier_needs_taskboard_url(monkeypatch, capsys):
    from agents_mindmap import cli

    monkeypatch.setenv("MINDMAP_DATABASE_URL", "postgresql://u:p@db.invalid/x")
    monkeypatch.setenv("MINDMAP_ORCHESTRATOR_URL", "http://o.invalid")
    monkeypatch.setenv("MINDMAP_PUBLIC_BASE_URL", "https://p.invalid")
    monkeypatch.setenv("MINDMAP_ORCHESTRATOR_TOKEN", "orch-fixture")
    monkeypatch.delenv("MINDMAP_TASKBOARD_URL", raising=False)
    assert cli.main(["notifier"]) == cli.EXIT_CONFIG
    assert "MINDMAP_TASKBOARD_URL" in capsys.readouterr().err


async def test_run_notifier_drains_and_stops(web_client, pool, db_url):
    m = await linked(web_client, "T-1")
    await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})
    due_now(db_url)
    tb = FakeTaskboard()
    posted = []
    lookup, client = orchestrator(fake_sessions([], posted))
    settings = make_settings(db_url, notifier_poll_seconds=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(
        notifier.run_notifier(
            settings, stop, pool=pool, taskboard=tb.client(), lookup=lookup, orchestrator=client
        )
    )
    for _ in range(100):
        if tb.calls:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert len(tb.calls) == 1
