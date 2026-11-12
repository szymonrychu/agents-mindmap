"""Directions: steering text with seed growth areas, a task link and a guarded lifecycle."""

import psycopg
from test_maps_api import events, make_map


async def setup(web_client):
    m = await make_map(web_client)
    node = (await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "n"})).json()
    return m, node


async def test_direction_transitions(web_client):
    m, _ = await setup(web_client)
    d = (await web_client.post(f"/maps/{m['key']}/directions", json={"text": "grow it"})).json()
    assert (d["status"], d["origin"], d["version"]) == ("open", "user", 1)
    acked = await web_client.patch(
        f"/directions/{d['key']}", json={"version": 1, "action": "acknowledge"}
    )
    assert acked.status_code == 200
    assert acked.json()["status"] == "acknowledged"
    assert acked.json()["acknowledged_by"] == "user:alice@example.test"
    again = await web_client.patch(
        f"/directions/{d['key']}", json={"version": 2, "action": "acknowledge"}
    )
    assert (again.status_code, again.json()["error"]["code"]) == (409, "invalid_transition")
    stale = await web_client.patch(
        f"/directions/{d['key']}", json={"version": 1, "action": "resolve"}
    )
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "version_conflict")
    assert stale.json()["error"]["version"] == 2
    done = await web_client.patch(
        f"/directions/{d['key']}",
        json={"version": 2, "action": "resolve", "resolution": "grown into N-1"},
    )
    assert done.status_code == 200
    assert (done.json()["status"], done.json()["resolution"]) == ("resolved", "grown into N-1")
    assert done.json()["resolved_by"] == "user:alice@example.test"
    for action in ("acknowledge", "resolve", "withdraw"):
        final = await web_client.patch(
            f"/directions/{d['key']}", json={"version": 3, "action": action}
        )
        assert (final.status_code, final.json()["error"]["code"]) == (409, "invalid_transition")


async def test_resolve_directly_from_open(web_client):
    m, _ = await setup(web_client)
    d = (await web_client.post(f"/maps/{m['key']}/directions", json={"text": "x"})).json()
    done = await web_client.patch(
        f"/directions/{d['key']}", json={"version": 1, "action": "resolve"}
    )
    assert done.json()["status"] == "resolved"


async def test_agent_direction_own_projects_only(web_client, agent):
    m, _ = await setup(web_client)
    other = await make_map(web_client, project="p2", slug="two", title="two")
    client = agent("p1")
    ok = await client.post(f"/maps/{m['key']}/directions", json={"text": "from an agent"})
    assert ok.status_code == 201
    assert (ok.json()["origin"], ok.json()["created_by"]) == ("agent", "session:alpha")
    foreign = await client.post(f"/maps/{other['key']}/directions", json={"text": "x"})
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (403, "project_forbidden")


async def test_withdraw_rules(web_client, agent):
    m, _ = await setup(web_client)
    mine = (await agent("p1").post(f"/maps/{m['key']}/directions", json={"text": "a"})).json()
    theirs = (await web_client.post(f"/maps/{m['key']}/directions", json={"text": "u"})).json()
    other_session = agent("p1", session="s2", slug="beta")
    refused = await other_session.patch(
        f"/directions/{mine['key']}", json={"version": 1, "action": "withdraw"}
    )
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "withdraw_forbidden")
    user_direction = await agent("p1").patch(
        f"/directions/{theirs['key']}", json={"version": 1, "action": "withdraw"}
    )
    assert user_direction.status_code == 403
    own = await agent("p1").patch(
        f"/directions/{mine['key']}", json={"version": 1, "action": "withdraw"}
    )
    assert (own.status_code, own.json()["status"]) == (200, "withdrawn")
    any_ = await web_client.patch(
        f"/directions/{theirs['key']}", json={"version": 1, "action": "withdraw"}
    )
    assert any_.json()["status"] == "withdrawn"


async def test_direction_seeds_growth_areas(web_client, db_url):
    m, node = await setup(web_client)
    d = (
        await web_client.post(
            f"/maps/{m['key']}/directions",
            json={"text": "dig", "node": node["key"], "growth_areas": ["a", "b"], "task": "T-7"},
        )
    ).json()
    assert d["node"] == node["key"]
    assert len(d["growth_areas"]) == 2
    view = (await web_client.get(f"/nodes/{node['key']}")).json()
    assert [g["direction"] for g in view["growth_areas"]] == [d["key"], d["key"]]
    assert [x["key"] for x in view["directions"]] == [d["key"]]
    assert view["directions"][0]["growth_areas"] == d["growth_areas"]
    assert [(k["ref_key"], k["node"]) for k in view["links"]] == [("T-7", node["key"])]
    detail = [e for e in events(db_url, "growth_area_changed")]
    assert all(e[2]["direction"] == d["key"] for e in detail)
    # the root takes the seeds without a node
    root_d = (
        await web_client.post(
            f"/maps/{m['key']}/directions", json={"text": "r", "growth_areas": ["c"]}
        )
    ).json()
    root_view = (await web_client.get(f"/nodes/{m['root']}")).json()
    assert [g["direction"] for g in root_view["growth_areas"]] == [root_d["key"]]


async def test_direction_list_filters(web_client, agent):
    m, _ = await setup(web_client)
    m2 = await make_map(web_client, project="p2", slug="two", title="two")
    d1 = (await web_client.post(f"/maps/{m['key']}/directions", json={"text": "1"})).json()
    await web_client.post(f"/maps/{m2['key']}/directions", json={"text": "2"})
    await web_client.patch(f"/directions/{d1['key']}", json={"version": 1, "action": "withdraw"})
    everything = (await agent("p1").get("/directions")).json()
    assert len(everything) == 2
    assert [
        d["project"] for d in (await web_client.get("/directions", params={"project": "p2"})).json()
    ] == ["p2"]
    assert [
        d["key"] for d in (await web_client.get("/directions", params={"map": m["key"]})).json()
    ] == [d1["key"]]
    assert (await web_client.get("/directions", params={"status": "open"})).json()[0][
        "project"
    ] == "p2"
    assert (await web_client.get("/directions", params={"status": "bogus"})).status_code == 422


async def test_direction_input_errors(web_client, db_url):
    m, _ = await setup(web_client)
    other = await make_map(web_client, slug="two", title="two")
    wrong_node = await web_client.post(
        f"/maps/{m['key']}/directions", json={"text": "x", "node": other["root"]}
    )
    assert (wrong_node.status_code, wrong_node.json()["error"]["code"]) == (422, "node_invalid")
    assert (await web_client.post("/maps/M-999/directions", json={"text": "x"})).status_code == 404
    assert (
        await web_client.post(f"/maps/{m['key']}/directions", json={"text": ""})
    ).status_code == 422
    assert (
        await web_client.patch("/directions/D-999", json={"version": 1, "action": "resolve"})
    ).status_code == 404
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT count(*) FROM directions").fetchone()[0] == 0
