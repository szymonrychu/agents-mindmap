"""Maps. All functions take an AsyncConnection inside the caller's transaction and raise
ApiError for the expected refusals (404, 409, 422)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg import errors
from psycopg.rows import class_row

from agents_mindmap.api.errors import ApiError
from agents_mindmap.db.events import append_event
from agents_mindmap.db.keys import fmt_key, parse_key

MAP_COLUMNS = (
    "id, project, slug, kind, title, description, root_node_id, status, created_by,"
    " created_at, updated_at, version, client_key"
)
UPDATABLE = ("title", "description", "status")


@dataclass(frozen=True)
class MapRow:
    id: int
    project: str
    slug: str
    kind: str
    title: str
    description: str
    root_node_id: int | None
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    version: int
    client_key: str | None

    @property
    def key(self) -> str:
        return fmt_key("M", self.id)


async def _select(
    conn: psycopg.AsyncConnection, where: str, params: Sequence[Any]
) -> MapRow | None:
    async with conn.cursor(row_factory=class_row(MapRow)) as cur:
        await cur.execute(f"SELECT {MAP_COLUMNS} FROM maps WHERE {where}", params)
        return await cur.fetchone()


async def get_map(conn: psycopg.AsyncConnection, key: str) -> MapRow | None:
    return await _select(conn, "id = %s", (parse_key("M", key),))


async def list_maps(
    conn: psycopg.AsyncConnection,
    *,
    readable: Sequence[str],
    projects: Sequence[str] | None = None,
    status: str | None = None,
    kind: str | None = None,
) -> list[MapRow]:
    """Maps of the readable (active) projects, optionally narrowed by project, status, kind."""
    where = ["project = ANY(%s)"]
    params: list[Any] = [list(readable)]
    if projects is not None:
        where.append("project = ANY(%s)")
        params.append(list(projects))
    if status is not None:
        where.append("status = %s")
        params.append(status)
    if kind is not None:
        where.append("kind = %s")
        params.append(kind)
    async with conn.cursor(row_factory=class_row(MapRow)) as cur:
        await cur.execute(
            f"SELECT {MAP_COLUMNS} FROM maps WHERE {' AND '.join(where)} ORDER BY id", params
        )
        return await cur.fetchall()


async def create_map(
    conn: psycopg.AsyncConnection,
    *,
    project: str,
    slug: str,
    title: str,
    description: str,
    kind: str,
    root_topic: str,
    actor: str,
    actor_session: str | None,
    client_key: str | None,
) -> MapRow:
    """Create the map, its root node and the root's version 1 (summary `created`) in the
    caller's transaction. The same (actor, client_key) returns the map made the first time."""
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO maps (project, slug, kind, title, description, status, created_by,"
                " client_key) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s)"
                " ON CONFLICT (created_by, client_key) WHERE client_key IS NOT NULL DO NOTHING"
                " RETURNING id",
                (project, slug, kind, title, description, actor, client_key),
            )
            row = await cur.fetchone()
            if row is None:
                existing = await _select(
                    conn, "created_by = %s AND client_key = %s", (actor, client_key)
                )
                assert existing is not None
                return existing
            map_id = row[0]
            cur = await conn.execute(
                "INSERT INTO nodes (map_id, project, topic, status, created_by, updated_by)"
                " VALUES (%s, %s, %s, 'seed', %s, %s) RETURNING id",
                (map_id, project, root_topic, actor, actor),
            )
            node_id = (await cur.fetchone())[0]  # type: ignore[index]
            await conn.execute(
                "INSERT INTO node_versions (node_id, version, topic, notes, author, summary)"
                " VALUES (%s, 1, %s, '', %s, 'created')",
                (node_id, root_topic, actor),
            )
            await conn.execute("UPDATE maps SET root_node_id = %s WHERE id = %s", (node_id, map_id))
            await append_event(
                conn,
                project=project,
                map_id=map_id,
                node_id=node_id,
                kind="map_created",
                actor=actor,
                actor_session=actor_session,
                detail={"map": fmt_key("M", map_id), "root": fmt_key("N", node_id)},
            )
    except errors.UniqueViolation:
        raise ApiError(409, "slug_taken", "a map with this slug exists in the project") from None
    created = await _select(conn, "id = %s", (map_id,))
    assert created is not None
    return created


async def update_map(
    conn: psycopg.AsyncConnection,
    key: str,
    *,
    version: int,
    fields: dict[str, Any],
    actor: str,
    actor_session: str | None = None,
) -> MapRow:
    """Guarded update of title, description or status; 404 unknown, 409 `version_conflict`
    with the row `version` when the map changed since `version` was read."""
    unknown = set(fields) - set(UPDATABLE)
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    map_id = parse_key("M", key)
    sets = ", ".join(f"{name} = %s" for name in fields)
    cur = await conn.execute(
        f"UPDATE maps SET {sets + ', ' if sets else ''}version = version + 1, updated_at = now()"
        " WHERE id = %s AND version = %s RETURNING project",
        (*fields.values(), map_id, version),
    )
    row = await cur.fetchone()
    if row is None:
        cur = await conn.execute("SELECT version FROM maps WHERE id = %s", (map_id,))
        current = await cur.fetchone()
        if current is None:
            raise ApiError(404, "not_found", "not found")
        raise ApiError(
            409,
            "version_conflict",
            "the map changed since it was read",
            {"version": current[0]},
        )
    await append_event(
        conn,
        project=row[0],
        map_id=map_id,
        node_id=None,
        kind="map_updated",
        actor=actor,
        actor_session=actor_session,
        detail={"map": fmt_key("M", map_id), "fields": sorted(fields)},
    )
    updated = await _select(conn, "id = %s", (map_id,))
    assert updated is not None
    return updated


async def delete_map(
    conn: psycopg.AsyncConnection,
    key: str,
    *,
    actor: str,
    actor_session: str | None = None,
) -> bool:
    """Hard delete (cascades nodes, versions, growth areas, edges, links, directions)."""
    map_id = parse_key("M", key)
    cur = await conn.execute("DELETE FROM maps WHERE id = %s RETURNING project", (map_id,))
    row = await cur.fetchone()
    if row is None:
        return False
    await append_event(
        conn,
        project=row[0],
        map_id=map_id,
        node_id=None,
        kind="map_deleted",
        actor=actor,
        actor_session=actor_session,
        detail={"map": fmt_key("M", map_id)},
    )
    return True
