"""Node versions: every topic or notes change keeps the full text, its author and a one-line
summary."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row

from agents_mindmap.api.errors import ApiError
from agents_mindmap.db.keys import parse_key


@dataclass(frozen=True)
class VersionMeta:
    version: int
    author: str
    summary: str
    created_at: datetime


@dataclass(frozen=True)
class VersionRow:
    version: int
    topic: str
    notes: str
    author: str
    summary: str
    created_at: datetime


async def insert_version(
    conn: psycopg.AsyncConnection,
    node_id: int,
    version: int,
    *,
    topic: str,
    notes: str,
    author: str,
    summary: str,
) -> None:
    await conn.execute(
        "INSERT INTO node_versions (node_id, version, topic, notes, author, summary)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (node_id, version, topic, notes, author, summary),
    )


async def _require_node(
    conn: psycopg.AsyncConnection, node_key: str, readable: Sequence[str]
) -> int:
    node_id = parse_key("N", node_key)
    cur = await conn.execute(
        "SELECT 1 FROM nodes WHERE id = %s AND project = ANY(%s)", (node_id, list(readable))
    )
    if await cur.fetchone() is None:
        raise ApiError(404, "not_found", "not found")
    return node_id


async def list_versions(
    conn: psycopg.AsyncConnection, node_key: str, *, readable: Sequence[str]
) -> list[VersionMeta]:
    """Versions of the node, oldest first, without the texts; 404 for an unknown node or one
    outside the readable (active) projects."""
    node_id = await _require_node(conn, node_key, readable)
    async with conn.cursor(row_factory=class_row(VersionMeta)) as cur:
        await cur.execute(
            "SELECT version, author, summary, created_at FROM node_versions"
            " WHERE node_id = %s ORDER BY version",
            (node_id,),
        )
        return await cur.fetchall()


async def get_version(
    conn: psycopg.AsyncConnection, node_key: str, v: int, *, readable: Sequence[str]
) -> VersionRow:
    node_id = await _require_node(conn, node_key, readable)
    async with conn.cursor(row_factory=class_row(VersionRow)) as cur:
        await cur.execute(
            "SELECT version, topic, notes, author, summary, created_at FROM node_versions"
            " WHERE node_id = %s AND version = %s",
            (node_id, v),
        )
        row = await cur.fetchone()
    if row is None:
        raise ApiError(404, "not_found", "not found")
    return row
