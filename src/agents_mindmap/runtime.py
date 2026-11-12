"""Process entry point of `mindmap serve`: one pool and the web, service and (when delegate
tokens are set) delegate listeners in one process, until SIGTERM or SIGINT."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator

import uvicorn
from agents_common.runtime import run_service
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from agents_mindmap.api.app import create_delegate_app, create_service_app, create_web_app
from agents_mindmap.settings import Settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

log = logging.getLogger(__name__)


class EmbeddingConfigError(RuntimeError):
    """The settings and the stored `embedding_config` row disagree."""


async def verify_embedding_config(pool: AsyncConnectionPool, settings: Settings) -> None:
    """Refuse to start when the configured embedding model or dimension differs from the one
    the stored vectors were built with; the message names the env var to fix, never a value
    other than the two model facts."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT model, dim FROM embedding_config")
        row = await cur.fetchone()
    if row is None:
        raise EmbeddingConfigError("embedding_config has no row")
    model, dim = row
    if dim != settings.embed_dim:
        raise EmbeddingConfigError(
            f"MINDMAP_EMBED_DIM={settings.embed_dim} but the database holds {dim}-dimension "
            "vectors: change the env var or migrate and reembed"
        )
    if model != settings.embedder_model:
        raise EmbeddingConfigError(
            f"MINDMAP_EMBEDDER_MODEL={settings.embedder_model} but the database holds vectors "
            f"of {model}: change the env var or migrate and reembed"
        )


class _Server(uvicorn.Server):
    """uvicorn without its own signal handling: run_service owns SIGTERM and SIGINT."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def open_pool(
    settings: Settings, min_size: int = 1, max_size: int = 10
) -> AsyncConnectionPool:
    """The connection pool; every connection starts with `idle_in_transaction_session_timeout`
    set, so a transaction left open (an outbound call by mistake) is cut by the server."""
    options = f"-c idle_in_transaction_session_timeout={settings.idle_in_transaction_timeout_ms}"
    pool = AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        min_size=min_size,
        max_size=max_size,
        kwargs={"options": options},
        open=False,
    )
    await pool.open(wait=True)
    return pool


def _config(settings: Settings, app: FastAPI, port: int) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=settings.http_host,
        port=port,
        log_level=settings.log_level.lower(),
        timeout_graceful_shutdown=int(settings.shutdown_grace_seconds),
        proxy_headers=False,  # the allowlist must see the socket peer, never X-Forwarded-For
    )


def _apps(settings: Settings, pool: AsyncConnectionPool) -> list[tuple[FastAPI, int]]:
    apps = [
        (create_web_app(settings, pool), settings.http_port),
        (create_service_app(settings, pool), settings.service_port),
    ]
    if settings.delegate_tokens:
        apps.append((create_delegate_app(settings, pool), settings.delegate_port))
    else:
        log.info("delegate listener off: MINDMAP_DELEGATE_TOKENS is empty")
    return apps


async def serve(settings: Settings, stop: asyncio.Event) -> None:
    """Serve every listener until `stop` is set or one listener ends; then stop them all."""
    pool = await open_pool(settings)
    try:
        await verify_embedding_config(pool, settings)
        servers = [_Server(_config(settings, app, port)) for app, port in _apps(settings, pool)]
        serving = [asyncio.create_task(s.serve()) for s in servers]
        stopping = asyncio.create_task(stop.wait())
        await asyncio.wait({*serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
        for server in servers:
            server.should_exit = True
        stopping.cancel()
        await asyncio.gather(*serving)
    finally:
        await pool.close()


def main(settings: Settings) -> int:
    """`mindmap serve`."""
    logging.basicConfig(level=settings.log_level.upper(), format=LOG_FORMAT, force=True)
    return asyncio.run(
        run_service(
            lambda stop: serve(settings, stop), grace_seconds=settings.shutdown_grace_seconds
        )
    )
