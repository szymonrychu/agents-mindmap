"""Knowledge capture: one lazily created capture map (slug knowledge) per project."""

import asyncio

import psycopg
from test_maps_api import make_map

NOTE = {"topic": "pgvector needs superuser", "notes": "create it in bootstrap", "summary": "found"}


async def test_knowledge_map_lazy_single(agent, db_url):
    client = agent("p1")
    first, second = await asyncio.gather(
        client.post("/knowledge/notes", json={**NOTE, "client_key": "a"}),
        client.post("/knowledge/notes", json={**NOTE, "topic": "second", "client_key": "b"}),
    )
    assert (first.status_code, second.status_code) == (201, 201), (first.text, second.text)
    assert first.json()["map"] == second.json()["map"]
    with psycopg.connect(db_url) as conn:
        rows = conn.execute("SELECT kind, slug, title, project FROM maps").fetchall()
        assert rows == [("knowledge", "knowledge", "p1 knowledge", "p1")]
        parents = {
            r[0] for r in conn.execute("SELECT parent_id FROM nodes WHERE parent_id IS NOT NULL")
        }
        root = conn.execute("SELECT root_node_id FROM maps").fetchone()[0]
    assert parents == {root}
    third = await client.post("/knowledge/notes", json={**NOTE, "topic": "third"})
    assert third.json()["map"] == first.json()["map"]


async def test_note_put_under_parent(agent, web_client):
    research = await make_map(web_client, project="p2", slug="r", title="r")
    parent = (await web_client.post(f"/maps/{research['key']}/nodes", json={"topic": "p"})).json()
    client = agent("p2,p1")
    note = await client.post(
        "/knowledge/notes",
        json={
            **NOTE,
            "parent": parent["key"],
            "links": [{"ref_key": "T-3", "relation": "cites"}],
            "edges": [{"dst": research["root"], "kind": "derived_from"}],
        },
    )
    assert note.status_code == 201, note.text
    body = note.json()
    assert (body["map"], body["parent"]) == (research["key"], parent["key"])
    assert [k["ref_key"] for k in body["links"]] == ["T-3"]
    assert [e["kind"] for e in body["edges"]] == ["derived_from"]
    maps = (await web_client.get("/maps", params={"kind": "knowledge"})).json()
    assert maps == []


async def test_note_project_defaults_and_scope(agent, web_client):
    home = await agent("p2,p1").post("/knowledge/notes", json=NOTE)
    assert home.json()["project"] == "p2"
    explicit = await agent("p2,p1").post("/knowledge/notes", json={**NOTE, "project": "p1"})
    assert explicit.json()["project"] == "p1"
    forbidden = await agent("p2").post("/knowledge/notes", json={**NOTE, "project": "p1"})
    assert forbidden.status_code == 403
    assert (await agent("*").post("/knowledge/notes", json=NOTE)).status_code == 422
    user = await web_client.post("/knowledge/notes", json={**NOTE, "project": "p1"})
    assert user.status_code == 201
    assert user.json()["created_by"] == "user:alice@example.test"
    unknown = await web_client.post("/knowledge/notes", json={**NOTE, "project": "nope"})
    assert unknown.json()["error"]["code"] == "project_unknown"
    mismatch = await web_client.post(
        "/knowledge/notes", json={**NOTE, "project": "p2", "parent": user.json()["key"]}
    )
    assert mismatch.status_code == 422


async def test_note_idempotent_client_key(agent):
    client = agent("p1")
    one = await client.post("/knowledge/notes", json={**NOTE, "client_key": "k"})
    two = await client.post("/knowledge/notes", json={**NOTE, "client_key": "k"})
    assert one.json()["key"] == two.json()["key"]


async def test_note_ignores_user_made_knowledge_map(agent, web_client):
    mine = await make_map(web_client, project="p1", slug="mine", title="mine", kind="knowledge")
    client = agent("p1")
    note = await client.post("/knowledge/notes", json=NOTE)
    assert note.status_code == 201, note.text
    assert note.json()["map"] != mine["key"]
    maps = (await web_client.get("/maps", params={"project": "p1"})).json()
    assert sorted(m["slug"] for m in maps) == ["knowledge", "mine"]
