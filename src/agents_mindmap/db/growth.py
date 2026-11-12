"""Growth areas: open questions on a node that a hive can take, grow and close. A text change
(or a new or deleted area) bumps the node `content_rev` so the embedder re-reads chunk 0."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import class_row

from agents_mindmap.api.actors import Actor
from agents_mindmap.api.errors import ApiError
from agents_mindmap.db.events import append_event
from agents_mindmap.db.keys import fmt_key, parse_key

GROWTH_COLUMNS = (
    "id, node_id, text, status, task_key, grown_node_id, direction_id, position, created_by,"
    " created_at, updated_at, version"
)
UPDATABLE = ("status", "task_key", "text", "position", "grown_node_id")


@dataclass(frozen=True)
class GrowthRow:
    id: int
    node_id: int
    text: str
    status: str
    task_key: str | None
    grown_node_id: int | None
    direction_id: int | None
    position: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    version: int

    @property
    def key(self) -> str:
        return fmt_key("G", self.id)


async def list_for_node(conn: psycopg.AsyncConnection, node_id: int) -> list[GrowthRow]:
    async with conn.cursor(row_factory=class_row(GrowthRow)) as cur:
        await cur.execute(
            f"SELECT {GROWTH_COLUMNS} FROM growth_areas WHERE node_id = %s ORDER BY position, id",
            (node_id,),
        )
        return await cur.fetchall()


async def _get(conn: psycopg.AsyncConnection, gid: int) -> GrowthRow | None:
    async with conn.cursor(row_factory=class_row(GrowthRow)) as cur:
        await cur.execute(f"SELECT {GROWTH_COLUMNS} FROM growth_areas WHERE id = %s", (gid,))
        return await cur.fetchone()


async def _node_scope(conn: psycopg.AsyncConnection, node_id: int) -> tuple[str, int]:
    cur = await conn.execute("SELECT project, map_id FROM nodes WHERE id = %s", (node_id,))
    row = await cur.fetchone()
    if row is None:
        raise ApiError(404, "not_found", "not found")
    return row[0], row[1]


async def _bump_content(conn: psycopg.AsyncConnection, node_id: int) -> None:
    await conn.execute("UPDATE nodes SET content_rev = content_rev + 1 WHERE id = %s", (node_id,))


async def _event(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    *,
    node_id: int,
    gid: int,
    action: str,
    fields: Sequence[str] = (),
    direction_id: int | None = None,
) -> None:
    project, map_id = await _node_scope(conn, node_id)
    detail: dict[str, Any] = {
        "growth_area": fmt_key("G", gid),
        "node": fmt_key("N", node_id),
        "action": action,
    }
    if fields:
        detail["fields"] = sorted(fields)
    if direction_id is not None:
        detail["direction"] = fmt_key("D", direction_id)
    await append_event(
        conn,
        project=project,
        map_id=map_id,
        node_id=node_id,
        kind="growth_area_changed",
        actor=actor.author,
        actor_session=actor.session,
        detail=detail,
    )


async def add_growth_areas(
    conn: psycopg.AsyncConnection,
    node_id: int,
    texts: Sequence[str],
    actor: Actor,
    *,
    direction_id: int | None = None,
) -> list[GrowthRow]:
    """Append growth areas (open, after the existing ones) to the node; 404 for an unknown
    node. Caller checks the scope."""
    await _node_scope(conn, node_id)
    if not texts:
        return []
    cur = await conn.execute("SELECT count(*) FROM growth_areas WHERE node_id = %s", (node_id,))
    start = (await cur.fetchone())[0]  # type: ignore[index]
    created: list[GrowthRow] = []
    for offset, text in enumerate(texts):
        cur = await conn.execute(
            "INSERT INTO growth_areas (node_id, text, direction_id, position, created_by)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (node_id, text, direction_id, start + offset, actor.author),
        )
        gid = (await cur.fetchone())[0]  # type: ignore[index]
        await _event(
            conn, actor, node_id=node_id, gid=gid, action="created", direction_id=direction_id
        )
        row = await _get(conn, gid)
        assert row is not None
        created.append(row)
    await _bump_content(conn, node_id)
    return created


async def update_growth_area(
    conn: psycopg.AsyncConnection,
    key: str,
    *,
    version: int,
    changes: dict[str, Any],
    actor: Actor,
) -> GrowthRow:
    """Guarded update of status, task_key, text, position (a zero-based index among the node's
    areas; siblings are renumbered) and grown_node_id. 404 unknown, 409 `version_conflict`, 422
    `task_required` (taken needs a task) and `grown_node_invalid`. A no-op returns the row."""
    unknown = set(changes) - set(UPDATABLE)
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    gid = parse_key("G", key)
    cur = await conn.execute("SELECT node_id FROM growth_areas WHERE id = %s FOR UPDATE", (gid,))
    found = await cur.fetchone()
    if found is None:
        raise ApiError(404, "not_found", "not found")
    node_id = found[0]
    row = await _get(conn, gid)
    assert row is not None
    if row.version != version:
        raise ApiError(
            409,
            "version_conflict",
            "the growth area changed since it was read",
            {"version": row.version},
        )
    status = changes.get("status", row.status)
    task_key = changes.get("task_key", row.task_key)
    if status == "taken" and not task_key:
        raise ApiError(422, "task_required", "a taken growth area needs a task key")
    grown = changes.get("grown_node_id", row.grown_node_id)
    if "grown_node_id" in changes and grown is not None:
        if status != "grown":
            raise ApiError(422, "grown_node_invalid", "only a grown growth area names a node")
        project, _ = await _node_scope(conn, node_id)
        cur = await conn.execute(
            "SELECT 1 FROM nodes WHERE id = %s AND project = %s", (grown, project)
        )
        if await cur.fetchone() is None:
            raise ApiError(422, "grown_node_invalid", "the grown node is not in this project")
    moved = "position" in changes
    columns = {
        name: value
        for name, value in changes.items()
        if name != "position" and value != getattr(row, name)
    }
    if not columns and not (moved and changes["position"] != row.position):
        return row
    sets = [f"{name} = %s" for name in columns] + ["version = version + 1", "updated_at = now()"]
    await conn.execute(
        f"UPDATE growth_areas SET {', '.join(sets)} WHERE id = %s", (*columns.values(), gid)
    )
    if moved:
        await _reorder(conn, node_id, gid, changes["position"])
    if "text" in columns:
        await _bump_content(conn, node_id)
    await _event(
        conn,
        actor,
        node_id=node_id,
        gid=gid,
        action="updated",
        fields=[*columns, *(["position"] if moved else [])],
        direction_id=row.direction_id,
    )
    updated = await _get(conn, gid)
    assert updated is not None
    return updated


async def _reorder(conn: psycopg.AsyncConnection, node_id: int, gid: int, position: int) -> None:
    cur = await conn.execute(
        "SELECT id, position FROM growth_areas WHERE node_id = %s ORDER BY position, id FOR UPDATE",
        (node_id,),
    )
    current = await cur.fetchall()
    order = [i for i, _ in current if i != gid]
    order.insert(max(0, min(position, len(order))), gid)
    old = dict(current)
    for index, area_id in enumerate(order):
        if old[area_id] != index:
            await conn.execute(
                "UPDATE growth_areas SET position = %s WHERE id = %s", (index, area_id)
            )


async def delete_growth_area(conn: psycopg.AsyncConnection, key: str, *, actor: Actor) -> bool:
    gid = parse_key("G", key)
    cur = await conn.execute("DELETE FROM growth_areas WHERE id = %s RETURNING node_id", (gid,))
    row = await cur.fetchone()
    if row is None:
        return False
    await _bump_content(conn, row[0])
    await _event(conn, actor, node_id=row[0], gid=gid, action="deleted")
    return True
