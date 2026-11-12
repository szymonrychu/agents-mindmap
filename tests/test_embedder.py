"""The embedder process: picks pending nodes, never holds a transaction across the model call,
discards stale results, backs off while the model is down, and `reembed` resets every node."""

import asyncio
import os

import httpx
import psycopg
import pytest
from conftest import make_settings
from fake_embedder import DIM, FakeEmbedder
from graph_seed import add_growth, seed_map

from agents_mindmap import embedder as emb
from agents_mindmap.cli import main
from agents_mindmap.db import chunks as chunks_db
from agents_mindmap.embeddings import QUERY_PREFIX, EmbedderUnavailable, OpenAIShapedProvider


def node_state(db_url: str, node_id: int) -> tuple[int, int]:
    with psycopg.connect(db_url) as conn:
        return conn.execute(
            "SELECT content_rev, embedded_rev FROM nodes WHERE id = %s", (node_id,)
        ).fetchone()


def chunk_rows(db_url: str, node_id: int) -> list[tuple]:
    with psycopg.connect(db_url) as conn:
        return conn.execute(
            "SELECT ord, project, content_rev, text, vector_dims(embedding) FROM node_chunks"
            " WHERE node_id = %s ORDER BY ord",
            (node_id,),
        ).fetchall()


async def drain(pool, provider) -> int:
    total = 0
    while n := await emb.embed_once(pool, provider):
        total += n
    return total


async def test_embedder_picks_pending(pool, db_url):
    _, (a, b) = seed_map(db_url, "p1", "m", ["alpha topic", "beta topic"])
    add_growth(db_url, a, "grow alpha further")
    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE nodes SET embedded_rev = content_rev WHERE id = %s", (b,))
        conn.commit()
    fake = FakeEmbedder()
    assert await emb.embed_once(pool, fake) == 1
    assert fake.calls and all(query is False for _, query in fake.calls)
    rows = chunk_rows(db_url, a)
    assert len(rows) == 1
    ord_, project, rev, text, dims = rows[0]
    assert (ord_, project, rev, dims) == (0, "p1", 1, DIM)
    assert text.startswith("alpha topic") and "grow alpha further" in text
    assert node_state(db_url, a) == (1, 1)
    assert chunk_rows(db_url, b) == []
    with psycopg.connect(db_url) as conn:
        events = conn.execute("SELECT kind, actor FROM events WHERE node_id = %s", (a,)).fetchall()
        notices = conn.execute("SELECT count(*) FROM task_notices").fetchone()[0]
    assert events == [("node_embedded", "system:embedder")]
    assert notices == 0
    assert await emb.embed_once(pool, fake) == 0
    async with pool.connection() as conn:
        assert await chunks_db.pending_count(conn) == 0


async def test_no_tx_across_embed_call(pool, db_url):
    _, (a, b) = seed_map(db_url, "p1", "m", ["one", "two"])
    observed = []

    def check(texts, query):
        with psycopg.connect(db_url, autocommit=True) as conn:
            open_tx = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%%'"
            ).fetchone()[0]
            # the node rows are not locked while the model runs
            locked = conn.execute(
                "SELECT count(*) FROM (SELECT id FROM nodes FOR UPDATE NOWAIT) s"
            ).fetchone()[0]
        for c in pool._pool:  # every idle pool connection is outside a transaction
            assert c.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        observed.append((open_tx, locked))

    assert await drain(pool, FakeEmbedder(on_call=check)) == 2
    assert observed and all(o == (0, 2) for o in observed)


async def test_stale_result_discarded(pool, db_url):
    _, (a,) = seed_map(db_url, "p1", "m", ["first words"])
    calls = 0

    def edit_during_call(texts, query):
        nonlocal calls
        calls += 1
        if calls == 1:
            with psycopg.connect(db_url) as conn:
                conn.execute(
                    "UPDATE nodes SET notes = 'changed words', content_rev = content_rev + 1"
                    " WHERE id = %s",
                    (a,),
                )
                conn.commit()

    fake = FakeEmbedder(on_call=edit_during_call)
    assert await emb.embed_once(pool, fake) == 0  # the result was stale: nothing stored
    assert chunk_rows(db_url, a) == []
    assert node_state(db_url, a) == (2, 0)
    assert await emb.embed_once(pool, fake) == 1  # retried with the new content
    rows = chunk_rows(db_url, a)
    assert [r[2] for r in rows] == [2] and "changed words" in rows[0][3]
    assert node_state(db_url, a) == (2, 2)


async def test_stale_keeps_previous_chunks(pool, db_url):
    _, (a,) = seed_map(db_url, "p1", "m", ["kept topic"])
    await drain(pool, FakeEmbedder())
    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE nodes SET content_rev = content_rev + 1 WHERE id = %s", (a,))
        conn.commit()

    def bump(texts, query):
        with psycopg.connect(db_url) as conn:
            conn.execute("UPDATE nodes SET content_rev = content_rev + 1 WHERE id = %s", (a,))
            conn.commit()

    assert await emb.embed_once(pool, FakeEmbedder(on_call=bump)) == 0
    assert [r[2] for r in chunk_rows(db_url, a)] == [1]  # old chunks still serve search


async def test_backoff_when_down(pool, db_url):
    seed_map(db_url, "p1", "m", ["down topic"])
    settings = make_settings(
        db_url,
        embedder_url="http://emb.invalid",
        embedder_poll_seconds=1,
        embedder_backoff_max_seconds=8,
    )
    delays: list[float] = []
    stop = asyncio.Event()

    async def fake_sleep(seconds, wake, stop_event):
        delays.append(seconds)
        if len(delays) >= 6:
            stop_event.set()

    down = FakeEmbedder(fail=True)
    await emb.run_embedder(settings, stop, pool=pool, provider=down, sleep=fake_sleep)
    assert delays == [1, 2, 4, 8, 8, 8]
    assert len(down.calls) == 6
    async with pool.connection() as conn:
        assert await chunks_db.pending_count(conn) == 1
    delays.clear()
    stop = asyncio.Event()

    async def stop_when_idle(seconds, wake, stop_event):
        delays.append(seconds)
        stop_event.set()

    await emb.run_embedder(settings, stop, pool=pool, provider=FakeEmbedder(), sleep=stop_when_idle)
    assert delays == [1]
    async with pool.connection() as conn:
        assert await chunks_db.pending_count(conn) == 0


async def test_embedder_metrics(pool, db_url):
    seed_map(db_url, "p1", "m", ["metric one", "metric two"])
    before = emb.REGISTRY.get_sample_value("mindmap_embed_seconds_count") or 0
    stop = asyncio.Event()

    async def stop_when_idle(seconds, wake, stop_event):
        stop_event.set()

    settings = make_settings(db_url, embedder_url="http://emb.invalid")
    await emb.run_embedder(settings, stop, pool=pool, provider=FakeEmbedder(), sleep=stop_when_idle)
    assert emb.REGISTRY.get_sample_value("mindmap_embed_pending") == 0
    assert emb.REGISTRY.get_sample_value("mindmap_embed_seconds_count") >= before + 2
    assert b"mindmap_embed_pending" in emb.render_metrics()


async def test_pending_embedding_count(pool, db_url, web_client):
    _, (a, b) = seed_map(db_url, "p1", "m", ["count one", "count two"])
    seed_map(db_url, "p2", "n", ["count three"])
    async with pool.connection() as conn:
        assert await chunks_db.pending_count(conn) == 3
    await drain(pool, FakeEmbedder())
    async with pool.connection() as conn:
        assert await chunks_db.pending_count(conn) == 0
    node = (await web_client.get(f"/nodes/N-{a}")).json()
    assert node["embedding_pending"] is False
    r = await web_client.patch(
        f"/nodes/N-{a}",
        json={"notes": "new text", "summary": "edit", "version": node["version"]},
    )
    assert r.status_code == 200, r.text
    async with pool.connection() as conn:
        assert await chunks_db.pending_count(conn) == 1
    body = (await web_client.get("/search", params={"q": "count"})).json()
    assert body["pending_embedding"] == 1


async def test_reembed_resets_all(pool, db_url, monkeypatch, capsys):
    _, ids = seed_map(db_url, "p1", "m", ["re one", "re two", "re three"])
    await drain(pool, FakeEmbedder())
    for key in list(os.environ):
        if key.startswith("MINDMAP_"):
            monkeypatch.delenv(key)
    for key, value in {
        "DATABASE_URL": db_url,
        "ORCHESTRATOR_URL": "http://o.invalid",
        "PUBLIC_BASE_URL": "https://lab.example.test",
    }.items():
        monkeypatch.setenv(f"MINDMAP_{key}", value)
    assert await asyncio.to_thread(main, ["reembed"]) == 0
    assert "3 nodes" in capsys.readouterr().out
    async with pool.connection() as conn:
        assert await chunks_db.pending_count(conn) == 3
    for node_id in ids:
        assert node_state(db_url, node_id)[1] == 0
        assert len(chunk_rows(db_url, node_id)) == 1  # old vectors serve until replaced
    assert await drain(pool, FakeEmbedder()) == 3


def test_embedder_command_needs_url(monkeypatch, capsys):
    for key in list(os.environ):
        if key.startswith("MINDMAP_"):
            monkeypatch.delenv(key)
    for key, value in {
        "DATABASE_URL": "postgresql://u:p@db.invalid/m",
        "ORCHESTRATOR_URL": "http://o.invalid",
        "PUBLIC_BASE_URL": "https://lab.example.test",
    }.items():
        monkeypatch.setenv(f"MINDMAP_{key}", value)
    assert main(["embedder"]) == 78
    assert "MINDMAP_EMBEDDER_URL" in capsys.readouterr().err


async def test_provider_openai_shape():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen.append((request.url.path, body))
        data = [
            {"object": "embedding", "index": i, "embedding": [3.0, 4.0] + [0.0] * (DIM - 2)}
            for i in range(len(body["input"]))
        ]
        return httpx.Response(200, json={"object": "list", "data": list(reversed(data))})

    provider = OpenAIShapedProvider(
        "http://emb.test/", "m/model", timeout=5, batch_size=2,
        transport=httpx.MockTransport(handler),
    )  # fmt: skip
    vectors = await provider.embed(["a", "b", "c"], query=False)
    assert len(vectors) == 3 and vectors[0][:2] == [0.6, 0.8]
    assert [p for p, _ in seen] == ["/v1/embeddings", "/v1/embeddings"]
    assert seen[0][1] == {"model": "m/model", "input": ["a", "b"]}
    assert seen[1][1]["input"] == ["c"]
    seen.clear()
    await provider.embed(["what"], query=True)
    assert seen[0][1]["input"] == [QUERY_PREFIX + "what"]
    await provider.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"error": "loading"}),
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),
    ],
)
async def test_provider_errors_are_unavailable(response):
    provider = OpenAIShapedProvider(
        "http://emb.test", "m", timeout=5, transport=httpx.MockTransport(lambda r: response)
    )
    with pytest.raises(EmbedderUnavailable):
        await provider.embed(["a"], query=False)

    def boom(request):
        raise httpx.ConnectError("refused")

    down = OpenAIShapedProvider(
        "http://emb.test", "m", timeout=5, transport=httpx.MockTransport(boom)
    )
    with pytest.raises(EmbedderUnavailable):
        await down.embed(["a"], query=False)
