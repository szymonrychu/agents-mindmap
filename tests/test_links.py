"""Task and spec links on maps and nodes."""

from test_maps_api import events, make_map


async def test_links_many_to_many(web_client):
    m1 = await make_map(web_client)
    m2 = await make_map(web_client, slug="two", title="two")
    node = (await web_client.post(f"/maps/{m1['key']}/nodes", json={"topic": "n"})).json()
    a = await web_client.post(f"/maps/{m1['key']}/links", json={"ref_key": "T-12"})
    b = await web_client.post(f"/maps/{m2['key']}/links", json={"ref_key": "T-12", "note": "x"})
    c = await web_client.post(
        f"/nodes/{node['key']}/links", json={"ref_key": "T-12", "relation": "implements"}
    )
    assert (a.status_code, b.status_code, c.status_code) == (201, 201, 201)
    listed = (await web_client.get("/links", params={"key": "T-12"})).json()
    assert [(k["map"], k["node"], k["project"]) for k in listed] == [
        (m1["key"], None, "p1"),
        (m2["key"], None, "p1"),
        (m1["key"], node["key"], "p1"),
    ]
    assert listed[2]["relation"] == "implements"
    assert (await web_client.get("/links", params={"key": "T-13"})).json() == []
    assert (await web_client.get("/links", params={"key": "X-1"})).status_code == 422
    view = (await web_client.get(f"/nodes/{node['key']}")).json()
    assert [k["ref_key"] for k in view["links"]] == ["T-12"]


async def test_link_to_unknown_task_ok(web_client):
    m = await make_map(web_client)
    assert (
        await web_client.post(f"/maps/{m['key']}/links", json={"ref_key": "T-99999"})
    ).status_code == 201
    assert (
        await web_client.post(f"/maps/{m['key']}/links", json={"ref_key": "S-3"})
    ).status_code == 201


async def test_duplicate_link_409_and_bad_input(web_client):
    m = await make_map(web_client)
    await web_client.post(f"/maps/{m['key']}/links", json={"ref_key": "T-1"})
    again = await web_client.post(f"/maps/{m['key']}/links", json={"ref_key": "T-1"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "link_exists")
    other_relation = await web_client.post(
        f"/maps/{m['key']}/links", json={"ref_key": "T-1", "relation": "cites"}
    )
    assert other_relation.status_code == 201
    assert (
        await web_client.post(f"/maps/{m['key']}/links", json={"ref_key": "T-1", "relation": "x"})
    ).status_code == 422
    assert (await web_client.post("/maps/M-999/links", json={"ref_key": "T-1"})).status_code == 404


async def test_link_delete_and_visibility(web_client, agent, db_url):
    m = await make_map(web_client)
    link = (await web_client.post(f"/maps/{m['key']}/links", json={"ref_key": "T-4"})).json()
    other_listed = (await agent("p2").get("/links", params={"key": "T-4"})).json()
    assert [k["id"] for k in other_listed] == [link["id"]]
    assert (await agent("p2").delete(f"/links/{link['id']}")).status_code == 403
    assert (await agent("p1").delete(f"/links/{link['id']}")).status_code == 204
    assert (await web_client.delete(f"/links/{link['id']}")).status_code == 404
    assert [e[0] for e in events(db_url)][-2:] == ["link_created", "link_deleted"]
