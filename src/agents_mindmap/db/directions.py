"""Directions: free-text steering for a map or node, acknowledged and resolved by hives.

Transitions are guarded by the row `version` and the current status: open to acknowledged or
resolved, acknowledged to resolved, open or acknowledged to withdrawn; resolved and withdrawn
are final."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import class_row

from agents_mindmap.api.actors import Actor, require_writable
from agents_mindmap.api.errors import ApiError
from agents_mindmap.db import growth, links
from agents_mindmap.db.events import append_event
from agents_mindmap.db.keys import fmt_key, parse_key
from agents_mindmap.db.outbox import enqueue_direction_notice, has_linked_task

DIRECTION_COLUMNS = (
    "id, map_id, project, node_id, text, origin, status, created_by, acknowledged_by,"
    " acknowledged_at, resolution, resolved_by, resolved_at, created_at, updated_at, version"
)
LIMIT = 200
ACTIONS = {
    "acknowledge": ("acknowledged", ("open",)),
    "resolve": ("resolved", ("open", "acknowledged")),
    "withdraw": ("withdrawn", ("open", "acknowledged")),
}
DIRECTION_TASK_RELATION = "researched_by"


@dataclass(frozen=True)
class DirectionRow:
    id: int
    map_id: int
    project: str
    node_id: int | None
    text: str
    origin: str
    status: str
    created_by: str
    acknowledged_by: str | None
    acknowledged_at: datetime | None
    resolution: str
    resolved_by: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int

    @property
    def key(self) -> str:
        return fmt_key("D", self.id)


async def _get(conn: psycopg.AsyncConnection, did: int) -> DirectionRow | None:
    async with conn.cursor(row_factory=class_row(DirectionRow)) as cur:
        await cur.execute(f"SELECT {DIRECTION_COLUMNS} FROM directions WHERE id = %s", (did,))
        return await cur.fetchone()


async def get_direction(conn: psycopg.AsyncConnection, key: str) -> DirectionRow | None:
    return await _get(conn, parse_key("D", key))


async def list_directions(
    conn: psycopg.AsyncConnection,
    *,
    readable: Sequence[str],
    project: str | None = None,
    map_key: str | None = None,
    node_id: int | None = None,
    statuses: Sequence[str] | None = None,
) -> list[DirectionRow]:
    where, params = ["project = ANY(%s)"], [list(readable)]
    for clause, value in (
        ("project = %s", project),
        ("map_id = %s", None if map_key is None else parse_key("M", map_key)),
        ("node_id = %s", node_id),
        ("status = ANY(%s)", None if statuses is None else list(statuses)),
    ):
        if value is not None:
            where.append(clause)
            params.append(value)
    async with conn.cursor(row_factory=class_row(DirectionRow)) as cur:
        await cur.execute(
            f"SELECT {DIRECTION_COLUMNS} FROM directions WHERE {' AND '.join(where)}"
            f" ORDER BY id DESC LIMIT {LIMIT}",
            params,
        )
        return await cur.fetchall()


async def seeds_of(
    conn: psycopg.AsyncConnection, direction_ids: Sequence[int]
) -> dict[int, list[str]]:
    """Growth area keys seeded by each direction."""
    cur = await conn.execute(
        "SELECT direction_id, id FROM growth_areas WHERE direction_id = ANY(%s) ORDER BY id",
        (list(direction_ids),),
    )
    out: dict[int, list[str]] = {}
    for did, gid in await cur.fetchall():
        out.setdefault(did, []).append(fmt_key("G", gid))
    return out


async def create_direction(
    conn: psycopg.AsyncConnection,
    *,
    map_key: str,
    node_key: str | None,
    text: str,
    seeds: Sequence[str],
    task_key: str | None,
    actor: Actor,
) -> DirectionRow:
    """Create an open direction on the map (optionally on one of its nodes), seed growth areas
    on that node (default the root) and link a task, in the caller's transaction. 404 unknown
    map, 403 `project_forbidden`, 422 `node_invalid`."""
    map_id = parse_key("M", map_key)
    cur = await conn.execute("SELECT project, root_node_id FROM maps WHERE id = %s", (map_id,))
    found = await cur.fetchone()
    if found is None:
        raise ApiError(404, "not_found", "not found")
    project, root_id = found
    require_writable(actor, project)
    node_id: int | None = None
    if node_key is not None:
        node_id = parse_key("N", node_key)
        cur = await conn.execute(
            "SELECT 1 FROM nodes WHERE id = %s AND map_id = %s", (node_id, map_id)
        )
        if await cur.fetchone() is None:
            raise ApiError(422, "node_invalid", "the node is not in this map")
    cur = await conn.execute(
        "INSERT INTO directions (map_id, project, node_id, text, origin, created_by)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (map_id, project, node_id, text, actor.origin, actor.author),
    )
    did = (await cur.fetchone())[0]  # type: ignore[index]
    if seeds:
        await growth.add_growth_areas(conn, node_id or root_id, seeds, actor, direction_id=did)
    if task_key is not None:
        await links.create_link(
            conn,
            map_id=map_id,
            project=project,
            node_id=node_id,
            ref_key=task_key,
            relation=DIRECTION_TASK_RELATION,
            note="",
            actor=actor,
            ignore_existing=True,
        )
    await _event(conn, actor, "direction_created", project, map_id, node_id, did)
    if not await has_linked_task(conn, map_id):
        await enqueue_direction_notice(conn, did)
    row = await _get(conn, did)
    assert row is not None
    return row


async def _event(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    kind: str,
    project: str,
    map_id: int,
    node_id: int | None,
    did: int,
) -> None:
    await append_event(
        conn,
        project=project,
        map_id=map_id,
        node_id=node_id,
        kind=kind,
        actor=actor.author,
        actor_session=actor.session,
        detail={"direction": fmt_key("D", did), "map": fmt_key("M", map_id)},
    )


async def transition(
    conn: psycopg.AsyncConnection,
    key: str,
    *,
    action: str,
    version: int,
    resolution: str,
    actor: Actor,
) -> DirectionRow:
    """Acknowledge, resolve or withdraw. 404 unknown, 403 `project_forbidden` (outside the
    caller's projects) or `withdraw_forbidden` (a session withdraws only its own agent-origin
    direction), 409 `version_conflict` (with the row `version`) or `invalid_transition`."""
    new_status, allowed = ACTIONS[action]
    did = parse_key("D", key)
    cur = await conn.execute("SELECT id FROM directions WHERE id = %s FOR UPDATE", (did,))
    if await cur.fetchone() is None:
        raise ApiError(404, "not_found", "not found")
    row = await _get(conn, did)
    assert row is not None
    require_writable(actor, row.project)
    own = row.origin == "agent" and row.created_by == actor.author
    if action == "withdraw" and actor.kind == "agent" and not own:
        raise ApiError(403, "withdraw_forbidden", "a session withdraws only its own direction")
    if row.version != version:
        raise ApiError(
            409,
            "version_conflict",
            "the direction changed since it was read",
            {"version": row.version},
        )
    if row.status not in allowed:
        raise ApiError(409, "invalid_transition", "the direction cannot make this transition")
    sets: dict[str, Any] = {"status": new_status}
    if action == "acknowledge":
        sets.update(acknowledged_by=actor.author)
    elif action == "resolve":
        sets.update(resolved_by=actor.author, resolution=resolution)
    assignments = [f"{c} = %s" for c in sets]
    for column, state in (("acknowledged_at", "acknowledge"), ("resolved_at", "resolve")):
        if action == state:
            assignments.append(f"{column} = now()")
    await conn.execute(
        f"UPDATE directions SET {', '.join(assignments)}, version = version + 1,"
        " updated_at = now() WHERE id = %s",
        (*sets.values(), did),
    )
    await _event(conn, actor, f"direction_{new_status}", row.project, row.map_id, row.node_id, did)
    updated = await _get(conn, did)
    assert updated is not None
    return updated
