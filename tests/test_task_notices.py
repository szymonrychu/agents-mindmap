"""The task-notice outbox: writes enqueue one coalesced pending row per linked task."""

import psycopg
import pytest
from test_maps_api import make_map

from agents_mindmap.db.events import EVENT_KINDS, SILENT_KINDS, append_event


def rows(db_url, where="true"):
    with psycopg.connect(db_url) as conn:
        return conn.execute(
            "SELECT task_key, map_id, actor_session, status, attempts"
            f" FROM task_notices WHERE {where} ORDER BY task_key"
        ).fetchall()


async def linked_map(client, *refs, slug="pgvector"):
    m = await make_map(client, slug=slug, title=slug)
    for ref in refs:
        r = await client.post(f"/maps/{m['key']}/links", json={"ref_key": ref})
        assert r.status_code == 201
    return m


async def emit(pool, map_id, kind, actor_session=None):
    async with pool.connection() as conn:
        await append_event(
            conn,
            project="p1",
            map_id=map_id,
            node_id=None,
            kind=kind,
            actor="user:x",
            actor_session=actor_session,
        )


async def test_change_enqueues_per_linked_task(web_client, db_url):
    m = await linked_map(web_client, "T-1", "T-2")
    with psycopg.connect(db_url) as conn:
        conn.execute("DELETE FROM task_notices")
        conn.commit()
    r = await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})
    assert r.status_code == 201
    assert [(x[0], x[3]) for x in rows(db_url)] == [("T-1", "pending"), ("T-2", "pending")]
    other = await make_map(web_client, slug="other", title="other")
    await web_client.post(f"/maps/{other['key']}/nodes", json={"topic": "n"})
    assert len(rows(db_url)) == 2  # an unlinked map notifies nobody


async def test_spec_links_do_not_enqueue(web_client, db_url):
    await linked_map(web_client, "S-3")
    assert rows(db_url) == []


@pytest.mark.parametrize("kind", EVENT_KINDS)
async def test_every_event_kind_enqueues(web_client, pool, db_url, kind):
    m = await linked_map(web_client, "T-1", "T-2")
    with psycopg.connect(db_url) as conn:
        conn.execute("DELETE FROM task_notices")
        conn.commit()
    await emit(pool, int(m["key"][2:]), kind)
    expected = [] if kind in SILENT_KINDS else ["T-1", "T-2"]
    assert [r[0] for r in rows(db_url)] == expected


def test_node_embedded_is_the_only_silent_kind():
    assert {"node_embedded"} == SILENT_KINDS
    assert "node_embedded" in EVENT_KINDS


async def test_embedder_events_silent(web_client, pool, db_url):
    m = await linked_map(web_client, "T-1")
    with psycopg.connect(db_url) as conn:
        conn.execute("DELETE FROM task_notices")
        conn.commit()
    for _ in range(5):
        await emit(pool, int(m["key"][2:]), "node_embedded")
    assert rows(db_url) == []


async def test_burst_coalesces(web_client, db_url):
    m = await linked_map(web_client, "T-1", "T-2")
    for i in range(50):
        r = await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": f"n{i}"})
        assert r.status_code == 201
    found = rows(db_url, "status = 'pending'")
    assert [r[0] for r in found] == ["T-1", "T-2"]
    with psycopg.connect(db_url) as conn:
        due = conn.execute(
            "SELECT count(*) FROM task_notices WHERE due_at > now() + interval '55 seconds'"
        ).fetchone()[0]
        assert due == 2  # first change + 60 s, kept by every later change


async def test_actor_kept_only_if_same(web_client, pool, db_url):
    m = await linked_map(web_client, "T-1")
    map_id = int(m["key"][2:])
    with psycopg.connect(db_url) as conn:
        conn.execute("DELETE FROM task_notices")
        conn.commit()
    await emit(pool, map_id, "node_updated", "s1")
    await emit(pool, map_id, "node_updated", "s1")
    assert rows(db_url)[0][2] == "s1"
    await emit(pool, map_id, "node_updated", "s2")
    assert rows(db_url)[0][2] is None
    await emit(pool, map_id, "node_updated", "s2")
    assert rows(db_url)[0][2] is None  # once mixed, never narrowed again


async def test_edge_enqueues_for_both_maps(web_client, db_url):
    a = await linked_map(web_client, "T-1", slug="a")
    b = await linked_map(web_client, "T-2", slug="b")
    with psycopg.connect(db_url) as conn:
        conn.execute("DELETE FROM task_notices")
        conn.commit()
    r = await web_client.post("/edges", json={"src": a["root"], "dst": b["root"]})
    assert r.status_code == 201, r.text
    assert [x[0] for x in rows(db_url)] == ["T-1", "T-2"]


async def test_direction_without_task_enqueues_fallback(web_client, db_url):
    m = await make_map(web_client)
    r = await web_client.post(f"/maps/{m['key']}/directions", json={"text": "grow this"})
    assert r.status_code == 201, r.text
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT count(*) FROM direction_notices").fetchone()[0] == 1
    assert rows(db_url) == []


async def test_direction_with_linked_task_has_no_fallback(web_client, db_url):
    m = await linked_map(web_client, "T-1")
    r = await web_client.post(f"/maps/{m['key']}/directions", json={"text": "grow this"})
    assert r.status_code == 201
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT count(*) FROM direction_notices").fetchone()[0] == 0
    assert [x[0] for x in rows(db_url)] == ["T-1"]
