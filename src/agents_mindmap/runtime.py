"""Process entry point of `mindmap serve-web | serve-service | serve-delegate`: one pool and one
listener per process (each runs in its own pod, holding only its own tokens), until SIGTERM or
SIGINT."""

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


LISTENERS = ("web", "service", "delegate")


def _app(settings: Settings, pool: AsyncConnectionPool, listener: str) -> tuple[FastAPI, int]:
    if listener == "web":
        return create_web_app(settings, pool), settings.http_port
    if listener == "service":
        return create_service_app(settings, pool), settings.service_port
    if listener == "delegate":
        return create_delegate_app(settings, pool), settings.delegate_port
    raise ValueError(f"unknown listener {listener!r}")


async def serve(settings: Settings, stop: asyncio.Event, listener: str) -> None:
    """Serve one listener until `stop` is set or it ends."""
    pool = await open_pool(settings)
    try:
        await verify_embedding_config(pool, settings)
        app, port = _app(settings, pool, listener)
        server = _Server(_config(settings, app, port))
        serving = asyncio.create_task(server.serve())
        stopping = asyncio.create_task(stop.wait())
        await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
        server.should_exit = True
        stopping.cancel()
        await serving
    finally:
        await pool.close()


def main(settings: Settings, listener: str) -> int:
    """`mindmap serve-<listener>`."""
    logging.basicConfig(level=settings.log_level.upper(), format=LOG_FORMAT, force=True)
    return asyncio.run(
        run_service(
            lambda stop: serve(settings, stop, listener),
            grace_seconds=settings.shutdown_grace_seconds,
        )
    )
