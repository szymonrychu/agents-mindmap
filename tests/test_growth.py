"""Growth areas: create, transitions, reorder, text drives the embedding revision."""

import psycopg
from test_maps_api import events, make_map


async def node_of(client):
    created = await make_map(client)
    node = (await client.post(f"/maps/{created['key']}/nodes", json={"topic": "n"})).json()
    return created, node


async def add(client, node_key, text):
    response = await client.post(f"/nodes/{node_key}/growth-areas", json={"text": text})
    assert response.status_code == 201, response.text
    return response.json()


async def test_growth_area_transitions(web_client):
    _, node = await node_of(web_client)
    area = await add(web_client, node["key"], "what about recall?")
    assert (area["status"], area["position"], area["version"]) == ("open", 0, 1)
    no_task = await web_client.patch(
        f"/growth-areas/{area['key']}", json={"version": 1, "status": "taken"}
    )
    assert no_task.status_code == 422
    assert no_task.json()["error"]["code"] == "task_required"
    taken = await web_client.patch(
        f"/growth-areas/{area['key']}", json={"version": 1, "status": "taken", "task_key": "T-5"}
    )
    assert taken.status_code == 200
    assert (taken.json()["status"], taken.json()["task_key"], taken.json()["version"]) == (
        "taken",
        "T-5",
        2,
    )
    child = (await web_client.post(f"/maps/{node['map']}/nodes", json={"topic": "c"})).json()
    early = await web_client.patch(
        f"/growth-areas/{area['key']}", json={"version": 2, "grown_node": child["key"]}
    )
    assert early.status_code == 422
    assert early.json()["error"]["code"] == "grown_node_invalid"
    grown = await web_client.patch(
        f"/growth-areas/{area['key']}",
        json={"version": 2, "status": "grown", "grown_node": child["key"]},
    )
    assert grown.status_code == 200
    assert grown.json()["grown_node"] == child["key"]
    stale = await web_client.patch(f"/growth-areas/{area['key']}", json={"version": 2})
    assert stale.status_code == 409
    assert stale.json()["error"]["version"] == 3
    # a task can be cleared explicitly
    cleared = await web_client.patch(
        f"/growth-areas/{area['key']}", json={"version": 3, "task_key": None}
    )
    assert cleared.json()["task_key"] is None
    view = (await web_client.get(f"/nodes/{node['key']}")).json()
    assert [g["status"] for g in view["growth_areas"]] == ["grown"]


async def test_grown_node_must_share_the_project(web_client):
    _, node = await node_of(web_client)
    other = await make_map(web_client, project="p2", slug="two", title="two")
    area = await add(web_client, node["key"], "q")
    response = await web_client.patch(
        f"/growth-areas/{area['key']}",
        json={"version": 1, "status": "grown", "grown_node": other["root"]},
    )
    assert response.status_code == 422


async def test_growth_area_reorder(web_client):
    _, node = await node_of(web_client)
    a, b, c = [await add(web_client, node["key"], t) for t in ("a", "b", "c")]
    assert [x["position"] for x in (a, b, c)] == [0, 1, 2]
    moved = await web_client.patch(f"/growth-areas/{c['key']}", json={"version": 1, "position": 0})
    assert moved.status_code == 200
    assert moved.json()["position"] == 0
    order = [
        g["text"] for g in (await web_client.get(f"/nodes/{node['key']}")).json()["growth_areas"]
    ]
    assert order == ["c", "a", "b"]
    await web_client.patch(f"/growth-areas/{c['key']}", json={"version": 2, "position": 99})
    order = [
        g["text"] for g in (await web_client.get(f"/nodes/{node['key']}")).json()["growth_areas"]
    ]
    assert order == ["a", "b", "c"]


async def test_text_change_bumps_content_rev_not_note_version(web_client):
    _, node = await node_of(web_client)
    assert node["content_rev"] == 1
    area = await add(web_client, node["key"], "first")
    after_add = (await web_client.get(f"/nodes/{node['key']}")).json()
    assert (after_add["content_rev"], after_add["current_version"]) == (2, 1)
    await web_client.patch(f"/growth-areas/{area['key']}", json={"version": 1, "status": "dropped"})
    assert (await web_client.get(f"/nodes/{node['key']}")).json()["content_rev"] == 2
    await web_client.patch(f"/growth-areas/{area['key']}", json={"version": 2, "text": "second"})
    after_text = (await web_client.get(f"/nodes/{node['key']}")).json()
    assert (after_text["content_rev"], after_text["current_version"]) == (3, 1)
    assert after_text["embedding_pending"] is True


async def test_delete_growth_area_user_only(web_client, agent):
    _, node = await node_of(web_client)
    area = await add(web_client, node["key"], "q")
    assert (await agent("p1").delete(f"/growth-areas/{area['key']}")).status_code == 405
    assert (await web_client.delete(f"/growth-areas/{area['key']}")).status_code == 204
    assert (await web_client.delete(f"/growth-areas/{area['key']}")).status_code == 404
    assert (await web_client.get(f"/nodes/{node['key']}")).json()["growth_areas"] == []


async def test_publish_full_node_in_one_call(agent, db_url):
    client = agent("p1")
    created = (await client.post("/maps", json={"project": "p1", "slug": "m", "title": "m"})).json()
    other = (await client.post("/maps", json={"project": "p1", "slug": "o", "title": "o"})).json()
    body = {
        "topic": "full",
        "notes": "body",
        "growth_areas": ["one", "two"],
        "links": [{"ref_key": "T-9", "relation": "informs"}],
        "edges": [{"dst": other["root"], "kind": "builds_on"}],
        "client_key": "pub-1",
    }
    first = await client.post(f"/maps/{created['key']}/nodes", json=body)
    assert first.status_code == 201, first.text
    node = first.json()
    assert [g["text"] for g in node["growth_areas"]] == ["one", "two"]
    assert [k["ref_key"] for k in node["links"]] == ["T-9"]
    assert [e["dst"] for e in node["edges"]] == [other["root"]]
    again = await client.post(f"/maps/{created['key']}/nodes", json=body)
    assert again.json()["key"] == node["key"]
    assert len(again.json()["growth_areas"]) == 2


async def test_publish_rolls_back_on_bad_edge(agent, db_url):
    client = agent("p1")
    created = (await client.post("/maps", json={"project": "p1", "slug": "m", "title": "m"})).json()
    with psycopg.connect(db_url) as conn:
        before = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
    response = await client.post(
        f"/maps/{created['key']}/nodes",
        json={"topic": "x", "growth_areas": ["g"], "edges": [{"dst": "N-99999"}]},
    )
    assert response.status_code == 404
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT count(*) FROM nodes").fetchone()[0] == before
        assert conn.execute("SELECT count(*) FROM growth_areas").fetchone()[0] == 0


async def test_growth_events_hold_no_text(web_client, db_url):
    _, node = await node_of(web_client)
    await add(web_client, node["key"], "CANARY-NOTE growth text")
    rows = events(db_url, "growth_area_changed")
    assert len(rows) == 1
    assert "CANARY" not in str(rows)
