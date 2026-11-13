"""The embedder process (`mindmap embedder`): chunks and embeds nodes whose content changed.

One embedder cluster-wide (advisory lock). Each pass claims a few pending nodes in a short
transaction, calls the model with no transaction open, then stores the chunks in a new
transaction guarded on `content_rev` (a node edited meanwhile stays pending and is retried).
While the model is down the loop backs off exponentially up to a cap. Metrics
`mindmap_embed_pending` and `mindmap_embed_seconds` are served on `MINDMAP_METRICS_PORT`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from agents_common.runtime import advisory_singleton, run_service
from prometheus_client import CollectorRegistry, Gauge, Histogram, generate_latest
from psycopg_pool import AsyncConnectionPool

from agents_mindmap.db import chunks as chunks_db
from agents_mindmap.embeddings import (
    EmbedderUnavailable,
    EmbeddingProvider,
    OpenAIShapedProvider,
    chunk_node,
)
from agents_mindmap.loop import sleep_or_wake, wake_on_notify
from agents_mindmap.runtime import open_pool, verify_embedding_config
from agents_mindmap.settings import Settings

log = logging.getLogger(__name__)

LOCK_NAME = "agents-mindmap-embedder"
BATCH_NODES = 4

REGISTRY = CollectorRegistry()
EMBED_PENDING = Gauge(
    "mindmap_embed_pending", "Nodes whose embedding lags their content.", registry=REGISTRY
)
EMBED_SECONDS = Histogram(
    "mindmap_embed_seconds",
    "Seconds of the model call that embeds one node.",
    buckets=(0.25, 0.5, 1, 2, 5, 10, 20, 40, 80, 160),
    registry=REGISTRY,
)

SleepFn = Callable[[float, asyncio.Event, asyncio.Event], Awaitable[None]]


def render_metrics() -> bytes:
    return generate_latest(REGISTRY)


async def _embed_batch(
    pool: AsyncConnectionPool, provider: EmbeddingProvider, batch: int = BATCH_NODES
) -> tuple[int, int]:
    """(claimed, stored) for one pass. Raises EmbedderUnavailable when the model fails."""
    async with pool.connection() as conn:
        nodes = await chunks_db.claim_pending(conn, batch)
    # the claim committed and the connection is back in the pool: no transaction is open
    stored = 0
    for node in nodes:
        texts = [c.text for c in chunk_node(node.topic, node.growth, node.notes)]
        started = time.monotonic()
        vectors = await provider.embed(texts, query=False)
        EMBED_SECONDS.observe(time.monotonic() - started)
        async with pool.connection() as conn:
            if await chunks_db.store_chunks(conn, node, texts, vectors):
                stored += 1
            else:
                log.info("node %s changed while it was embedded; it stays pending", node.id)
    return len(nodes), stored


async def embed_once(pool: AsyncConnectionPool, provider: EmbeddingProvider) -> int:
    """One pass; the number of nodes whose chunks were stored."""
    return (await _embed_batch(pool, provider))[1]


async def _update_pending(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        EMBED_PENDING.set(await chunks_db.pending_count(conn))


async def run_embedder(
    settings: Settings,
    stop: asyncio.Event,
    *,
    pool: AsyncConnectionPool,
    provider: EmbeddingProvider | None = None,
    sleep: SleepFn = sleep_or_wake,
) -> None:
    """Embed pending nodes until `stop` is set; idle, wait for NOTIFY mindmap_changed or the
    poll interval; down, back off from the poll interval doubling up to the cap."""
    owned = provider is None
    if provider is None:
        provider = OpenAIShapedProvider(
            settings.embedder_url,
            settings.embedder_model,
            timeout=settings.embedder_timeout_seconds,
            dim=settings.embed_dim,
            batch_size=settings.embedder_batch_size,
        )
    wake = asyncio.Event()
    listener = asyncio.create_task(wake_on_notify(settings.database_url.get_secret_value(), wake))
    poll, cap = settings.embedder_poll_seconds, settings.embedder_backoff_max_seconds
    delay = poll
    try:
        async with advisory_singleton(pool, LOCK_NAME):
            while not stop.is_set():
                wake.clear()
                try:
                    claimed, _ = await _embed_batch(pool, provider)
                except EmbedderUnavailable as exc:
                    log.warning("embeddings endpoint unavailable, retry in %.0fs: %s", delay, exc)
                    # a NOTIFY must not cut the backoff short: wait on a private event
                    await sleep(delay, asyncio.Event(), stop)
                    delay = min(delay * 2, cap)
                    continue
                delay = poll
                await _update_pending(pool)
                if claimed == 0:
                    await sleep(poll, wake, stop)
    finally:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        if owned:
            await provider.aclose()


async def serve(settings: Settings, stop: asyncio.Event) -> None:
    """The `mindmap embedder` process body: checks the stored model and dimension, serves the
    metrics, runs the loop."""
    from prometheus_client import start_http_server

    pool = await open_pool(settings, min_size=2, max_size=4)
    server = None
    try:
        await verify_embedding_config(pool, settings)
        server, _ = start_http_server(settings.metrics_port, registry=REGISTRY)
        await run_embedder(settings, stop, pool=pool)
    finally:
        if server is not None:
            server.shutdown()
        await pool.close()


def main(settings: Settings) -> int:
    """Run the embedder until SIGTERM or SIGINT; the exit code."""
    logging.basicConfig(level=settings.log_level.upper(), force=True)
    return asyncio.run(
        run_service(
            lambda stop: serve(settings, stop), grace_seconds=settings.shutdown_grace_seconds
        )
    )


async def reembed(settings: Settings) -> int:
    """`mindmap reembed`: mark every node pending; the number of nodes."""
    pool = await open_pool(settings, min_size=1, max_size=1)
    try:
        async with pool.connection() as conn:
            return await chunks_db.reset_all(conn)
    finally:
        await pool.close()
