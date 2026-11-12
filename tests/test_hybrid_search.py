"""Hybrid search: RRF (k = 60) over full text, trigram and chunk similarity with a fake
embedder; scope on chunks; degradation when the embedder is down or slow."""

import time

import pytest
from fake_embedder import FakeEmbedder
from graph_seed import key, seed_map, set_text

from agents_mindmap import embedder as emb
from agents_mindmap import embeddings
from agents_mindmap.db.search import RRF_K


def use_embedder(client, provider) -> None:
    """Point the app behind a test client at `provider` (the fixtures build apps without one)."""
    client._transport.app.state.embedder = provider


async def drain(pool, provider=None) -> None:
    provider = provider or FakeEmbedder()
    while await emb.embed_once(pool, provider):
        pass


async def search(client, **params):
    response = await client.get("/search", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_hybrid_rrf_deterministic(web_client, pool, db_url):
    _, (root, a, b) = seed_map(
        db_url, "p1", "m", ["kubernetes operators overview", "controllers", "unrelated"]
    )
    set_text(db_url, a, notes="reconcile loops reconcile state; reconcile again")
    set_text(db_url, b, notes="gardening and tomatoes")
    await drain(pool)
    fake = FakeEmbedder()
    use_embedder(web_client, fake)
    first = await search(web_client, q="reconcile", mode="hybrid")
    second = await search(web_client, q="reconcile", mode="hybrid")
    assert first == second
    assert first["semantic"] is True
    top = first["items"][0]
    assert top["key"] == key("N", a)
    assert top["signals"] == ["fts", "semantic"]
    assert top["score"] == round(2 / (RRF_K + 1), 6)
    assert fake.calls == [(["reconcile"], True), (["reconcile"], True)]
    # every other hit came only from the chunk list: its snippet is the chunk text
    rest = first["items"][1:]
    assert rest and all(h["signals"] == ["semantic"] for h in rest)
    assert all("[[" not in h["snippet"] for h in rest)
    by_key = {h["key"]: h for h in rest}
    assert by_key[key("N", b)]["snippet"].startswith("unrelated")
    scores = [h["score"] for h in first["items"]]
    assert scores == sorted(scores, reverse=True)


async def test_mode_semantic_and_lexical(web_client, pool, db_url):
    _, (a, b) = seed_map(db_url, "p1", "m", ["vector index", "garden"])
    set_text(db_url, a, notes="hnsw graph for cosine distance")
    await drain(pool)
    use_embedder(web_client, FakeEmbedder())
    semantic = await search(web_client, q="cosine distance", mode="semantic")
    assert semantic["semantic"] is True
    assert semantic["items"][0]["key"] == key("N", a)
    assert all(h["signals"] == ["semantic"] for h in semantic["items"])
    lexical = await search(web_client, q="cosine distance", mode="lexical")
    assert lexical["semantic"] is False
    assert [h["key"] for h in lexical["items"]] == [key("N", a)]
    assert "semantic" not in lexical["items"][0]["signals"]


async def test_semantic_scope_applied_to_chunks(web_client, agent, pool, db_url):
    _, (a,) = seed_map(db_url, "p1", "one", ["rollout alpha"])
    _, (b,) = seed_map(db_url, "p2", "two", ["rollout beta"])
    _, (o,) = seed_map(db_url, "old", "gone", ["rollout gamma"])
    await drain(pool)
    client = agent("p1")
    use_embedder(client, FakeEmbedder())
    own = await search(client, q="rollout", mode="semantic")
    assert own["semantic"] is True
    assert [h["key"] for h in own["items"]] == [key("N", a)]
    every = await search(client, q="rollout", mode="semantic", scope="all")
    assert {h["key"] for h in every["items"]} == {key("N", a), key("N", b)}
    use_embedder(web_client, FakeEmbedder())
    user = await search(web_client, q="rollout", mode="semantic")
    assert {h["key"] for h in user["items"]} == {key("N", a), key("N", b), key("N", o)}
    only = await search(web_client, q="rollout", mode="semantic", project="p2")
    assert [h["key"] for h in only["items"]] == [key("N", b)]
    other = agent("p2")
    use_embedder(other, FakeEmbedder())
    assert (await search(other, q="rollout", mode="semantic", project="old", scope="all"))[
        "items"
    ] == []


async def test_semantic_map_and_status_filters(web_client, pool, db_url):
    m1, (a,) = seed_map(db_url, "p1", "one", ["shared words here"])
    m2, (b,) = seed_map(db_url, "p1", "two", ["shared words there"])
    await drain(pool)
    use_embedder(web_client, FakeEmbedder())
    only = await search(web_client, q="shared words", mode="semantic", map=key("M", m2))
    assert [h["key"] for h in only["items"]] == [key("N", b)]
    none = await search(web_client, q="shared words", mode="semantic", status="settled")
    assert none["items"] == []


@pytest.mark.parametrize("mode", ["hybrid", "semantic"])
async def test_search_degrades_without_embedder(web_client, pool, db_url, mode):
    _, (a,) = seed_map(db_url, "p1", "m", ["fallback topic"])
    set_text(db_url, a, notes="lexical words still match")
    await drain(pool)
    use_embedder(web_client, FakeEmbedder(fail=True))
    body = await search(web_client, q="lexical words", mode=mode)
    assert body["semantic"] is False
    assert [h["key"] for h in body["items"]] == [key("N", a)]
    assert body["items"][0]["signals"] == ["fts"]
    use_embedder(web_client, None)
    body = await search(web_client, q="lexical words", mode=mode)
    assert body["semantic"] is False and [h["key"] for h in body["items"]] == [key("N", a)]


async def test_query_embed_timeout_2s(web_client, pool, db_url, monkeypatch):
    assert embeddings.QUERY_TIMEOUT_SECONDS == 2.0
    _, (a,) = seed_map(db_url, "p1", "m", ["slow topic"])
    await drain(pool)
    monkeypatch.setattr(embeddings, "QUERY_TIMEOUT_SECONDS", 0.05)
    slow = FakeEmbedder(delay=1.0)
    use_embedder(web_client, slow)
    started = time.monotonic()
    body = await search(web_client, q="slow topic")
    assert time.monotonic() - started < 0.9
    assert body["semantic"] is False
    assert [h["key"] for h in body["items"]] == [key("N", a)]
    assert slow.calls == [(["slow topic"], True)]


async def test_hostile_input_with_embedder(web_client, pool, db_url):
    seed_map(db_url, "p1", "m", ["a b c script alert"])
    await drain(pool)
    use_embedder(web_client, FakeEmbedder())
    for q in ['"a" OR -b "', "<script>alert(1)</script>", "\x00", "((", "%"]:
        body = await search(web_client, q=q)
        for hit in body["items"]:
            assert "<" not in hit["snippet"] and "<" not in hit["topic"]
