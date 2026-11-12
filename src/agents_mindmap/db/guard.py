"""Read-query guard: a transaction-local statement timeout, answered as 503 `query_timeout`."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import psycopg
from psycopg import errors

from agents_mindmap.api.errors import ApiError

STATEMENT_TIMEOUT_MS = 3000


@contextlib.asynccontextmanager
async def read_guard(conn: psycopg.AsyncConnection) -> AsyncIterator[None]:
    """Run the block in a transaction with `SET LOCAL statement_timeout`; a cancelled
    statement becomes ApiError 503 `query_timeout` (no query text, no caller input)."""
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('statement_timeout', %s, true)", (str(STATEMENT_TIMEOUT_MS),)
            )
            yield
    except errors.QueryCanceled:
        raise ApiError(503, "query_timeout", "the query took too long") from None
