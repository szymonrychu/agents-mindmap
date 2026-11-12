"""Project lookups for scope checks: the project of a row addressed by key, 404 when unknown.
Projects never change, so the check needs no lock."""

from __future__ import annotations

import psycopg

from agents_mindmap.api.errors import ApiError
from agents_mindmap.db.keys import parse_key

_SQL = {
    "map": ("M", "SELECT project FROM maps WHERE id = %s"),
    "node": ("N", "SELECT project FROM nodes WHERE id = %s"),
    "growth": (
        "G",
        "SELECT n.project FROM growth_areas g JOIN nodes n ON n.id = g.node_id WHERE g.id = %s",
    ),
    "direction": ("D", "SELECT project FROM directions WHERE id = %s"),
}


async def project_of(conn: psycopg.AsyncConnection, kind: str, key: str) -> str:
    """The project of the map, node, growth area or direction `key`; ApiError 404 `not_found`."""
    prefix, sql = _SQL[kind]
    cur = await conn.execute(sql, (parse_key(prefix, key),))
    row = await cur.fetchone()
    if row is None:
        raise ApiError(404, "not_found", "not found")
    return row[0]


async def project_of_link(conn: psycopg.AsyncConnection, link_id: int) -> str:
    cur = await conn.execute("SELECT project FROM links WHERE id = %s", (link_id,))
    row = await cur.fetchone()
    if row is None:
        raise ApiError(404, "not_found", "not found")
    return row[0]
