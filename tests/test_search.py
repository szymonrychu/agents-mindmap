"""Lexical search: hostile input, fuzzy topics, growth areas, scope, limits, pending count."""

import pytest
from graph_seed import add_growth, key, seed_map, set_text

HOSTILE = ['"a" OR -b "', "<script>alert(1)</script>", "żółć ☃ naïve", "'; DROP TABLE nodes;--",
           "a & b | !c", ":*", "((", "\\", "%", "-", '"', "\x00"]  # fmt: skip


async def search(client, **params):
    response = await client.get("/search", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("q", HOSTILE)
async def test_search_hostile_input(web_client, db_url, q):
    _, (n,) = seed_map(db_url, "p1", "m", ["a b c zażółć naïve script alert"])
    set_text(db_url, n, notes="plain notes about a and b and script alert")
    body = await search(web_client, q=q, scope="all")
    assert body["semantic"] is False
    for hit in body["items"]:
        assert "<" not in hit["snippet"] and ">" not in hit["snippet"]
        assert "<" not in hit["topic"]


async def test_search_hostile_markers_are_plain_text(web_client, db_url):
    _, (n,) = seed_map(db_url, "p1", "m", ["pgvector notes"])
    set_text(db_url, n, notes="how pgvector stores vectors in postgres")
    body = await search(web_client, q="pgvector")
    assert body["items"][0]["key"] == key("N", n)
    snippet = body["items"][0]["snippet"]
    assert "[[pgvector]]" in snippet and "<b>" not in snippet
    assert "fts" in body["items"][0]["signals"]


async def test_search_q_too_long_422(web_client):
    assert (await web_client.get("/search", params={"q": "a" * 200})).status_code == 200
    response = await web_client.get("/search", params={"q": "a" * 201})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert (await web_client.get("/search", params={"q": ""})).status_code == 422
    assert (await web_client.get("/search")).status_code == 422


async def test_search_trgm_typo(web_client, db_url):
    _, (n,) = seed_map(db_url, "p1", "m", ["pgvector"])
    body = await search(web_client, q="pgvektor")
    assert [h["key"] for h in body["items"]] == [key("N", n)]
    assert body["items"][0]["signals"] == ["trgm"]
    assert body["items"][0]["snippet"] == "pgvector"


async def test_growth_area_hit_maps_to_node(web_client, db_url):
    _, (n, other) = seed_map(db_url, "p1", "m", ["databases", "unrelated"])
    add_growth(db_url, n, "benchmark hnsw recall under load")
    body = await search(web_client, q="hnsw recall")
    assert [h["key"] for h in body["items"]] == [key("N", n)]
    hit = body["items"][0]
    assert hit["topic"] == "databases" and "[[hnsw]]" in hit["snippet"]
    assert hit["signals"] == ["fts"]


async def test_search_filters_map_status_project(web_client, db_url):
    m1, (a,) = seed_map(db_url, "p1", "one", ["kubernetes operators"])
    m2, (b,) = seed_map(db_url, "p2", "two", ["kubernetes upgrades"])
    both = await search(web_client, q="kubernetes", scope="all")
    assert {h["key"] for h in both["items"]} == {key("N", a), key("N", b)}
    only = await search(web_client, q="kubernetes", map=key("M", m2))
    assert [h["key"] for h in only["items"]] == [key("N", b)]
    proj = await search(web_client, q="kubernetes", project="p1")
    assert [h["key"] for h in proj["items"]] == [key("N", a)]
    assert (await search(web_client, q="kubernetes", status="settled"))["items"] == []
    assert len((await search(web_client, q="kubernetes", status="seed"))["items"]) == 2


async def test_search_scope_own_vs_all(agent, db_url):
    _, (a,) = seed_map(db_url, "p1", "one", ["rollout strategy"])
    _, (b,) = seed_map(db_url, "p2", "two", ["rollout checklist"])
    client = agent("p1")
    own = await search(client, q="rollout")
    assert [h["key"] for h in own["items"]] == [key("N", a)]
    default = await search(client, q="rollout", scope="own")
    assert default == own
    everything = await search(client, q="rollout", scope="all")
    assert {h["key"] for h in everything["items"]} == {key("N", a), key("N", b)}
    assert (await search(client, q="rollout", scope="own", project="p2"))["items"] == []


async def test_limit_capped_50(web_client, db_url):
    seed_map(db_url, "p1", "many", [f"widget {i}" for i in range(60)])
    assert len((await search(web_client, q="widget", limit=500))["items"]) == 50
    assert len((await search(web_client, q="widget", limit=3))["items"]) == 3
    assert len((await search(web_client, q="widget"))["items"]) == 20
    assert (await web_client.get("/search", params={"q": "widget", "limit": 0})).status_code == 422


AGENTS = {"home": "p1", "grant": "p2,p1", "superleader": "*", "other": "p2"}


@pytest.mark.parametrize("name", AGENTS)
async def test_scope_matrix_search_traverse(web_client, agent, db_url, name):
    _, (a,) = seed_map(db_url, "p1", "one", ["zebra one"])
    _, (b,) = seed_map(db_url, "p2", "two", ["zebra two"])
    _, (o,) = seed_map(db_url, "old", "gone", ["zebra old"])
    client = agent(AGENTS[name])
    found = await search(client, q="zebra", scope="all")
    assert {h["key"] for h in found["items"]} == {key("N", a), key("N", b)}
    for start, status in ((a, 200), (b, 200), (o, 404)):
        response = await client.get(f"/nodes/{key('N', start)}/traverse")
        assert response.status_code == status
    # the user is unscoped (spec 3.5): archived projects are searched too
    user = await search(web_client, q="zebra")
    assert {h["key"] for h in user["items"]} == {key("N", a), key("N", b), key("N", o)}
    only_old = await search(web_client, q="zebra", project="old")
    assert {h["key"] for h in only_old["items"]} == {key("N", o)}
    assert (await search(client, q="zebra", project="old", scope="all"))["items"] == []


async def test_pending_embedding_count_lexical(web_client, agent, db_url):
    import psycopg

    _, (a, b) = seed_map(db_url, "p1", "one", ["alpha", "beta"])
    _, (c,) = seed_map(db_url, "p2", "two", ["gamma"])
    _, (o,) = seed_map(db_url, "old", "gone", ["delta"])
    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE nodes SET embedded_rev = content_rev WHERE id = %s", (b,))
        conn.commit()
    body = await search(web_client, q="alpha", mode="semantic", scope="all")
    assert body["pending_embedding"] == 3  # a, c and old (user is unscoped); b is embedded
    assert body["semantic"] is False
    assert [h["key"] for h in body["items"]] == [key("N", a)]
    all_agent = await search(agent("p1"), q="alpha", scope="all")
    assert all_agent["pending_embedding"] == 2  # a and c; the archived project is not counted
    own = await search(agent("p1"), q="alpha", mode="hybrid")
    assert own["pending_embedding"] == 1
    assert (await search(web_client, q="alpha", mode="lexical"))["semantic"] is False
