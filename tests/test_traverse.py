"""Traversal and the map graph: caps, cycles, scope, query count, timeout."""

import itertools

import psycopg
import pytest
from graph_seed import add_growth, key, seed_edges, seed_map

from agents_mindmap.db import guard
from agents_mindmap.db import traverse as traverse_db


def node_keys(body):
    return {n["key"] for n in body["nodes"]}


async def test_traverse_cycle_terminates(web_client, db_url):
    _, (a, b, c) = seed_map(db_url, "p1", "cyc", ["a", "b", "c"])
    # the tree also joins them; use edges only to prove the cycle
    seed_edges(db_url, [(a, b), (b, c), (c, a)])
    response = await web_client.get(f"/nodes/{key('N', a)}/traverse", params={"tree": "false"})
    assert response.status_code == 200
    body = response.json()
    assert node_keys(body) == {key("N", a), key("N", b), key("N", c)}
    assert len(body["nodes"]) == 3
    assert len(body["edges"]) == 3
    assert body["truncated"] is False


async def test_traverse_depth_cap(web_client, db_url):
    _, ids = seed_map(db_url, "p1", "chain", [f"n{i}" for i in range(8)])
    seed_edges(db_url, list(itertools.pairwise(ids[1:])))
    start = key("N", ids[1])
    response = await web_client.get(
        f"/nodes/{start}/traverse", params={"depth": 9, "tree": "false", "follow": "out"}
    )
    body = response.json()
    assert max(n["depth"] for n in body["nodes"]) == 4
    assert len(body["nodes"]) == 5
    default = (await web_client.get(f"/nodes/{start}/traverse", params={"tree": "false"})).json()
    assert max(n["depth"] for n in default["nodes"]) == 2
    assert (
        await web_client.get(f"/nodes/{start}/traverse", params={"depth": -1})
    ).status_code == 422


async def test_traverse_result_cap_truncated(web_client, db_url):
    _, ids = seed_map(db_url, "p1", "star", [f"n{i}" for i in range(251)])
    body = (await web_client.get(f"/nodes/{key('N', ids[0])}/traverse")).json()
    assert len(body["nodes"]) == 200
    assert body["truncated"] is True
    assert body["nodes"][0]["depth"] == 0


async def test_traverse_never_leaves_readable(web_client, db_url, pool):
    _, (a, a2) = seed_map(db_url, "p1", "one", ["a", "a2"])
    _, (b, b2) = seed_map(db_url, "p2", "two", ["b", "b2"])
    _, (o, o2) = seed_map(db_url, "old", "gone", ["o", "o2"])
    seed_edges(db_url, [(a, b), (b, o), (o, a2)])
    async with pool.connection() as conn:
        narrow = await traverse_db.traverse(
            conn, a, depth=4, kinds=["relates_to"], follow="both", tree=True, readable=["p1"]
        )
        wide = await traverse_db.traverse(
            conn, a, depth=4, kinds=["relates_to"], follow="both", tree=True, readable=["p1", "p2"]
        )
    assert {n.key for n in narrow.nodes} == {key("N", a), key("N", a2)}
    assert {n.key for n in wide.nodes} == {key("N", i) for i in (a, a2, b, b2)}
    assert all(e.src_project in ("p1", "p2") and e.dst_project in ("p1", "p2") for e in wide.edges)
    # the user route: active projects only, the inactive one never appears
    body = (await web_client.get(f"/nodes/{key('N', a)}/traverse", params={"depth": 4})).json()
    assert key("N", o) not in node_keys(body)
    assert {n["project"] for n in body["nodes"]} == {"p1", "p2"}


async def test_traverse_user_may_start_in_inactive_project(web_client, agent, db_url):
    _, (o, o2) = seed_map(db_url, "old", "gone", ["o", "o2"])
    body = (await web_client.get(f"/nodes/{key('N', o)}/traverse")).json()
    assert node_keys(body) == {key("N", o), key("N", o2)}
    assert (await agent("p1").get(f"/nodes/{key('N', o)}/traverse")).status_code == 404


async def test_traverse_scope_own(agent, db_url):
    _, (a,) = seed_map(db_url, "p1", "one", ["a"])
    _, (b,) = seed_map(db_url, "p2", "two", ["b"])
    seed_edges(db_url, [(a, b)])
    client = agent("p1")
    start = f"/nodes/{key('N', a)}/traverse"
    assert len((await client.get(start, params={"scope": "all"})).json()["nodes"]) == 2
    assert len((await client.get(start, params={"scope": "own"})).json()["nodes"]) == 1
    assert (
        await client.get(f"/nodes/{key('N', b)}/traverse", params={"scope": "own"})
    ).status_code == 404


async def test_traverse_follow_and_kinds(web_client, db_url):
    _, (a, b, c) = seed_map(db_url, "p1", "fk", ["a", "b", "c"])
    seed_edges(db_url, [(a, b)], kind="builds_on")
    seed_edges(db_url, [(c, a)], kind="see_also")
    base = f"/nodes/{key('N', a)}/traverse"

    async def reach(**params):
        return node_keys((await web_client.get(base, params={"tree": "false", **params})).json())

    assert await reach(follow="out") == {key("N", a), key("N", b)}
    assert await reach(follow="in") == {key("N", a), key("N", c)}
    assert await reach(follow="both", kinds="builds_on") == {key("N", a), key("N", b)}
    bad = await web_client.get(base, params={"kinds": "nope"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_request"
    assert (await web_client.get("/nodes/N-99999/traverse")).status_code == 404
    assert (await web_client.get("/nodes/bad/traverse")).status_code == 404


async def test_traverse_two_followup_queries(db_url):
    _, (a, b, c) = seed_map(db_url, "p1", "cnt", ["a", "b", "c"])
    seed_edges(db_url, [(a, b), (b, c)])
    seen: list[str] = []

    class CountingCursor(psycopg.AsyncCursor):
        async def execute(self, query, *args, **kwargs):
            seen.append(str(query))
            return await super().execute(query, *args, **kwargs)

    conn = await psycopg.AsyncConnection.connect(db_url, cursor_factory=CountingCursor)
    try:
        graph = await traverse_db.traverse(
            conn, a, depth=3, kinds=["relates_to"], follow="both", tree=True, readable=["p1"]
        )
    finally:
        await conn.close()
    assert len(graph.nodes) == 3
    # the statement timeout, the walk, then the node rows and the edges among the result
    assert len(seen) == 4, seen
    assert "set_config" in seen[0] and "FROM nodes" in seen[2] and "FROM edges" in seen[3]


async def test_traverse_timeout_503(web_client, db_url, monkeypatch):
    _, ids = seed_map(db_url, "p1", "dense", [f"n{i}" for i in range(36)])
    seed_edges(db_url, [(s, d) for s in ids for d in ids if s != d])
    monkeypatch.setattr(guard, "STATEMENT_TIMEOUT_MS", 1)
    response = await web_client.get(
        f"/nodes/{key('N', ids[0])}/traverse", params={"depth": 4, "tree": "false"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "query_timeout"


async def test_graph_cross_map_stubs(web_client, db_url):
    m1, (r1, c1, c2) = seed_map(db_url, "p1", "one", ["root", "c1", "c2"])
    _, (r2, d1) = seed_map(db_url, "p2", "two", ["other-root", "d1"])
    _, (o,) = seed_map(db_url, "old", "gone", ["hidden"])
    seed_edges(db_url, [(c1, d1), (r2, c2), (c1, c2), (c1, o)])
    body = (await web_client.get(f"/maps/{key('M', m1)}/graph")).json()
    by_key = {n["key"]: n for n in body["nodes"]}
    assert set(by_key) == {key("N", i) for i in (r1, c1, c2, d1, r2)}
    assert [by_key[key("N", i)]["stub"] for i in (r1, c1, c2)] == [False] * 3
    far = by_key[key("N", d1)]
    assert far["stub"] is True
    assert (far["topic"], far["map"], far["project"]) == ("d1", far["map"], "p2")
    assert far["map"] != by_key[key("N", r1)]["map"]
    assert by_key[key("N", c1)]["parent"] == key("N", r1)
    assert len(body["edges"]) == 3  # the edge into the inactive project is not shown
    assert body["truncated"] is False
    shallow = (await web_client.get(f"/maps/{key('M', m1)}/graph", params={"depth": 0})).json()
    assert key("N", c1) not in {n["key"] for n in shallow["nodes"] if not n["stub"]}
    assert (await web_client.get("/maps/M-99999/graph")).status_code == 404


async def test_node_stub_fields(web_client, db_url):
    _, (r, c) = seed_map(db_url, "p1", "stubs", ["root", "child"])
    add_growth(db_url, c, "first")
    add_growth(db_url, c, "second")
    add_growth(db_url, c, "done", status="grown")
    body = (await web_client.get(f"/nodes/{key('N', r)}/traverse")).json()
    child = next(n for n in body["nodes"] if n["key"] == key("N", c))
    assert set(child) == {
        "key", "map", "project", "topic", "parent", "status", "open_growth_areas",
        "embedding_pending", "depth", "stub",
    }  # fmt: skip
    assert child["parent"] == key("N", r)
    assert child["open_growth_areas"] == 2
    assert child["embedding_pending"] is True  # embedded_rev 0 < content_rev 1
    assert (child["depth"], child["stub"], child["status"]) == (1, False, "seed")
    root = body["nodes"][0]
    assert root["parent"] is None and root["open_growth_areas"] == 0


@pytest.mark.parametrize("scope", ["home", "grant", "superleader", "other"])
async def test_graph_agents_read_active_not_inactive(agent, db_url, scope):
    m1, _ = seed_map(db_url, "p1", "one", ["a"])
    mo, _ = seed_map(db_url, "old", "gone", ["o"])
    client = agent({"home": "p1", "grant": "p2,p1", "superleader": "*", "other": "p2"}[scope])
    assert (await client.get(f"/maps/{key('M', m1)}/graph")).status_code == 200
    assert (await client.get(f"/maps/{key('M', mo)}/graph")).status_code == 404
