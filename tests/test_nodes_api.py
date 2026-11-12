"""Node routes: create, versioned update, concurrency, reparenting, deletion, event hygiene."""

import asyncio

import psycopg
from test_maps_api import make_map


async def setup(client):
    created = await make_map(client)
    return created["key"], created["root"]


async def test_create_node_under_root_by_default(web_client):
    map_key, root = await setup(web_client)
    response = await web_client.post(
        f"/maps/{map_key}/nodes", json={"topic": "HNSW", "notes": "graph index"}
    )
    assert response.status_code == 201
    node = response.json()
    assert (node["parent"], node["map"], node["status"]) == (root, map_key, "seed")
    assert node["embedding_pending"] is True
    assert (await web_client.get(f"/nodes/{node['key']}/versions/1")).json()["summary"] == "created"


async def test_create_node_parent_must_be_in_map(web_client):
    map_key, _ = await setup(web_client)
    other = await make_map(web_client, slug="other")
    response = await web_client.post(
        f"/maps/{map_key}/nodes", json={"topic": "x", "parent": other["root"]}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "parent_invalid"
    assert (await web_client.post("/maps/M-999/nodes", json={"topic": "x"})).status_code == 404


async def test_idempotent_node_create(web_client):
    map_key, _ = await setup(web_client)
    body = {"topic": "a", "client_key": "same"}
    first = (await web_client.post(f"/maps/{map_key}/nodes", json=body)).json()
    second = (await web_client.post(f"/maps/{map_key}/nodes", json=body)).json()
    assert first["key"] == second["key"]


async def test_update_notes_requires_summary(web_client):
    map_key, root = await setup(web_client)
    for body in ({"version": 1, "notes": "new"}, {"version": 1, "topic": "t2"}):
        response = await web_client.patch(f"/nodes/{root}", json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "summary_required"
    assert (await web_client.get(f"/nodes/{root}")).json()["version"] == 1


async def test_update_bumps_version_and_content_rev(web_client):
    _, root = await setup(web_client)
    before = (await web_client.get(f"/nodes/{root}")).json()
    patched = (
        await web_client.patch(
            f"/nodes/{root}", json={"version": 1, "notes": "n2", "summary": "added notes"}
        )
    ).json()
    assert patched["version"] == before["version"] + 1
    assert patched["content_rev"] == before["content_rev"] + 1
    assert patched["current_version"] == 2
    status_only = (
        await web_client.patch(f"/nodes/{root}", json={"version": 2, "status": "growing"})
    ).json()
    assert (status_only["version"], status_only["content_rev"], status_only["current_version"]) == (
        3,
        2,
        2,
    )


async def test_unchanged_patch_is_a_noop(web_client, db_url):
    _, root = await setup(web_client)
    same = (await web_client.patch(f"/nodes/{root}", json={"version": 1, "status": "seed"})).json()
    assert same["version"] == 1


async def test_concurrent_update_one_wins(web_client, db_url):
    _, root = await setup(web_client)

    async def patch(text):
        return await web_client.patch(
            f"/nodes/{root}", json={"version": 1, "notes": text, "summary": f"by {text}"}
        )

    first, second = await asyncio.gather(patch("one"), patch("two"))
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    loser = first if first.status_code == 409 else second
    assert loser.json()["error"]["code"] == "version_conflict"
    assert loser.json()["error"]["current_version"] == 2
    with psycopg.connect(db_url) as conn:
        versions = conn.execute("SELECT version FROM node_versions ORDER BY version").fetchall()
    assert versions == [(1,), (2,)]


async def test_reparent_cycle_refused(web_client):
    map_key, root = await setup(web_client)
    a = (await web_client.post(f"/maps/{map_key}/nodes", json={"topic": "a"})).json()
    b = (
        await web_client.post(f"/maps/{map_key}/nodes", json={"topic": "b", "parent": a["key"]})
    ).json()
    cycle = await web_client.patch(f"/nodes/{a['key']}", json={"version": 1, "parent": b["key"]})
    assert (cycle.status_code, cycle.json()["error"]["code"]) == (422, "parent_cycle")
    selfparent = await web_client.patch(
        f"/nodes/{a['key']}", json={"version": 1, "parent": a["key"]}
    )
    assert selfparent.json()["error"]["code"] == "parent_cycle"
    moved = await web_client.patch(f"/nodes/{b['key']}", json={"version": 1, "parent": root})
    assert moved.status_code == 200
    assert moved.json()["parent"] == root
    rootmove = await web_client.patch(f"/nodes/{root}", json={"version": 1, "parent": b["key"]})
    assert rootmove.json()["error"]["code"] == "root_immovable"


async def test_reparent_across_maps_refused(web_client):
    map_key, _ = await setup(web_client)
    other = await make_map(web_client, slug="other")
    node = (await web_client.post(f"/maps/{map_key}/nodes", json={"topic": "a"})).json()
    response = await web_client.patch(
        f"/nodes/{node['key']}", json={"version": 1, "parent": other["root"]}
    )
    assert response.json()["error"]["code"] == "parent_invalid"


async def test_events_hold_no_note_text(web_client, db_url):
    map_key, root = await setup(web_client)
    node = (
        await web_client.post(
            f"/maps/{map_key}/nodes",
            json={"topic": "CANARY-TOPIC", "notes": "CANARY-NOTE", "summary": "CANARY-SUMMARY"},
        )
    ).json()
    await web_client.patch(
        f"/nodes/{node['key']}", json={"version": 1, "notes": "CANARY-NOTE-2", "summary": "s"}
    )
    with psycopg.connect(db_url) as conn:
        rows = conn.execute("SELECT kind, detail::text FROM events ORDER BY id").fetchall()
    assert {k for k, _ in rows} >= {"map_created", "node_created", "node_updated", "node_versioned"}
    assert not [d for _, d in rows if "CANARY" in d]


async def test_delete_node_subtree_and_root_guard(web_client):
    map_key, root = await setup(web_client)
    a = (await web_client.post(f"/maps/{map_key}/nodes", json={"topic": "a"})).json()
    b = (
        await web_client.post(f"/maps/{map_key}/nodes", json={"topic": "b", "parent": a["key"]})
    ).json()
    assert (await web_client.delete(f"/nodes/{root}")).status_code == 422
    assert (await web_client.delete(f"/nodes/{a['key']}")).status_code == 204
    assert (await web_client.get(f"/nodes/{b['key']}")).status_code == 404
    assert (await web_client.delete(f"/nodes/{a['key']}")).status_code == 404


async def test_text_limits_422(web_client):
    map_key, _ = await setup(web_client)
    too_long = {"topic": "t" * 201}
    assert (await web_client.post(f"/maps/{map_key}/nodes", json=too_long)).status_code == 422
    big = {"topic": "t", "notes": "é" * 40000}
    assert (await web_client.post(f"/maps/{map_key}/nodes", json=big)).status_code == 422
