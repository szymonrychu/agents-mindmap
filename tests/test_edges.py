"""Edges across maps and projects: write the source, read the destination."""

import psycopg
from test_maps_api import _insert_inactive, events, make_map


async def two_projects(web_client):
    m1 = await make_map(web_client)
    m2 = await make_map(web_client, project="p2", slug="two", title="two")
    n1 = (await web_client.post(f"/maps/{m1['key']}/nodes", json={"topic": "n1"})).json()
    n2 = (await web_client.post(f"/maps/{m2['key']}/nodes", json={"topic": "n2"})).json()
    return n1, n2


async def test_edge_own_to_foreign_allowed(web_client, agent):
    n1, n2 = await two_projects(web_client)
    client = agent("p1")
    response = await client.post("/edges", json={"src": n1["key"], "dst": n2["key"]})
    assert response.status_code == 201, response.text
    edge = response.json()
    assert (edge["src_project"], edge["dst_project"], edge["kind"]) == ("p1", "p2", "relates_to")
    assert edge["created_by"] == "session:alpha"


async def test_edge_foreign_to_own_refused(web_client, agent, db_url):
    n1, n2 = await two_projects(web_client)
    client = agent("p1")
    response = await client.post("/edges", json={"src": n2["key"], "dst": n1["key"]})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "project_forbidden"
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 0


async def test_edge_to_inactive_or_unknown_node_404(web_client, agent, db_url):
    n1, _ = await two_projects(web_client)
    _, old_node = _insert_inactive(db_url)
    client = agent("p1")
    for dst in (old_node, "N-99999"):
        response = await client.post("/edges", json={"src": n1["key"], "dst": dst})
        assert response.status_code == 404, dst


async def test_edge_delete_by_either_end(web_client, agent):
    n1, n2 = await two_projects(web_client)
    edge = (await agent("p1").post("/edges", json={"src": n1["key"], "dst": n2["key"]})).json()
    assert (await agent("p3x").delete(f"/edges/{edge['key']}")).status_code == 403
    # the destination's project rejects the unwanted inbound link
    assert (await agent("p2").delete(f"/edges/{edge['key']}")).status_code == 204
    again = (await agent("p1").post("/edges", json={"src": n1["key"], "dst": n2["key"]})).json()
    assert (await agent("p1").delete(f"/edges/{again['key']}")).status_code == 204
    assert (await agent("p1").delete(f"/edges/{again['key']}")).status_code == 404


async def test_edge_listed_when_both_readable(web_client, agent, db_url):
    n1, n2 = await two_projects(web_client)
    await agent("p1").post("/edges", json={"src": n1["key"], "dst": n2["key"], "note": "see"})
    for key in (n1["key"], n2["key"]):
        view = (await agent("p2").get(f"/nodes/{key}")).json()
        assert [(e["src"], e["dst"], e["note"]) for e in view["edges"]] == [
            (n1["key"], n2["key"], "see")
        ]
    # an edge into an inactive project is not listed
    old_map, old_node = _insert_inactive(db_url)
    with psycopg.connect(db_url) as conn:
        conn.execute(
            "INSERT INTO edges (src_node_id, src_project, dst_node_id, dst_project, kind,"
            " created_by) VALUES (%s, 'p1', %s, 'old', 'see_also', 'user:x')",
            (int(n1["key"][2:]), int(old_node[2:])),
        )
        conn.commit()
    view = (await agent("p2").get(f"/nodes/{n1['key']}")).json()
    assert [e["dst"] for e in view["edges"]] == [n2["key"]]


async def test_edge_rules_and_events(web_client, db_url):
    n1, n2 = await two_projects(web_client)
    same = await web_client.post("/edges", json={"src": n1["key"], "dst": n1["key"]})
    assert same.status_code == 422
    assert same.json()["error"]["code"] == "self_edge"
    created = await web_client.post(
        "/edges", json={"src": n1["key"], "dst": n2["key"], "kind": "contradicts"}
    )
    assert created.status_code == 201
    dup = await web_client.post(
        "/edges", json={"src": n1["key"], "dst": n2["key"], "kind": "contradicts"}
    )
    assert (dup.status_code, dup.json()["error"]["code"]) == (409, "edge_exists")
    bad_kind = await web_client.post(
        "/edges", json={"src": n1["key"], "dst": n2["key"], "kind": "x"}
    )
    assert bad_kind.status_code == 422
    # one event per map the edge touches
    assert len(events(db_url, "edge_created")) == 2
    assert (await web_client.delete(f"/edges/{created.json()['key']}")).status_code == 204
    assert len(events(db_url, "edge_deleted")) == 2


async def test_edge_in_one_map_logs_one_event(web_client, db_url):
    m = await make_map(web_client)
    a = (await web_client.post(f"/maps/{m['key']}/nodes", json={"topic": "a"})).json()
    await web_client.post("/edges", json={"src": m["root"], "dst": a["key"]})
    assert len(events(db_url, "edge_created")) == 1
