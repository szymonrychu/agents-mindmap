"""Typed edges between nodes, possibly of different maps and projects.

Rules (spec 3.5): create needs write on the source's project and read on the destination;
delete needs write on either end's project; an edge is listed when both ends are readable."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg import errors
from psycopg.rows import class_row

from agents_mindmap.api.actors import Actor, require_writable
from agents_mindmap.api.errors import ApiError
from agents_mindmap.db.events import append_event
from agents_mindmap.db.keys import fmt_key, parse_key

EDGE_COLUMNS = (
    "id, src_node_id, src_project, dst_node_id, dst_project, kind, note, created_by, created_at"
)


@dataclass(frozen=True)
class EdgeRow:
    id: int
    src_node_id: int
    src_project: str
    dst_node_id: int
    dst_project: str
    kind: str
    note: str
    created_by: str
    created_at: datetime

    @property
    def key(self) -> str:
        return fmt_key("E", self.id)


async def _node_info(conn: psycopg.AsyncConnection, node_id: int) -> tuple[str, int] | None:
    cur = await conn.execute("SELECT project, map_id FROM nodes WHERE id = %s", (node_id,))
    return await cur.fetchone()


async def _events(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    kind: str,
    edge: EdgeRow,
    ends: Sequence[tuple[str, int, int]],
) -> None:
    """One event per distinct map the edge touches (project, map id, node id of that end)."""
    seen: set[int] = set()
    for project, map_id, node_id in ends:
        if map_id in seen:
            continue
        seen.add(map_id)
        await append_event(
            conn,
            project=project,
            map_id=map_id,
            node_id=node_id,
            kind=kind,
            actor=actor.author,
            actor_session=actor.session,
            detail={
                "edge": edge.key,
                "src": fmt_key("N", edge.src_node_id),
                "dst": fmt_key("N", edge.dst_node_id),
                "kind": edge.kind,
            },
        )


async def create_edge(
    conn: psycopg.AsyncConnection,
    *,
    src_key: str,
    dst_key: str,
    kind: str,
    note: str,
    actor: Actor,
    readable: Sequence[str],
) -> EdgeRow:
    """Create the edge; 404 for an unknown or unreadable end, 403 `project_forbidden` when the
    source project is not writable, 409 `edge_exists`, 422 `self_edge`."""
    src_id, dst_id = parse_key("N", src_key), parse_key("N", dst_key)
    src, dst = await _node_info(conn, src_id), await _node_info(conn, dst_id)
    if src is None or dst is None:
        raise ApiError(404, "not_found", "not found")
    if actor.kind == "agent" and (src[0] not in readable or dst[0] not in readable):
        raise ApiError(404, "not_found", "not found")
    require_writable(actor, src[0])
    if src_id == dst_id:
        raise ApiError(422, "self_edge", "an edge needs two different nodes")
    try:
        async with conn.transaction(), conn.cursor(row_factory=class_row(EdgeRow)) as cur:
            await cur.execute(
                f"INSERT INTO edges (src_node_id, src_project, dst_node_id, dst_project,"
                f" kind, note, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                f" RETURNING {EDGE_COLUMNS}",
                (src_id, src[0], dst_id, dst[0], kind, note, actor.author),
            )
            edge = await cur.fetchone()
    except errors.UniqueViolation:
        raise ApiError(409, "edge_exists", "the edge exists") from None
    assert edge is not None
    await _events(
        conn,
        actor,
        "edge_created",
        edge,
        [(src[0], src[1], src_id), (dst[0], dst[1], dst_id)],
    )
    return edge


async def delete_edge(conn: psycopg.AsyncConnection, key: str, *, actor: Actor) -> bool:
    """Delete when the caller may write either end's project (403 otherwise)."""
    edge_id = parse_key("E", key)
    async with conn.cursor(row_factory=class_row(EdgeRow)) as cur:
        await cur.execute(f"SELECT {EDGE_COLUMNS} FROM edges WHERE id = %s FOR UPDATE", (edge_id,))
        edge = await cur.fetchone()
    if edge is None:
        return False
    if not (actor.may_write(edge.src_project) or actor.may_write(edge.dst_project)):
        raise ApiError(403, "project_forbidden", "the project is outside the caller's scope")
    src = await _node_info(conn, edge.src_node_id)
    dst = await _node_info(conn, edge.dst_node_id)
    await conn.execute("DELETE FROM edges WHERE id = %s", (edge_id,))
    assert src is not None and dst is not None
    await _events(
        conn,
        actor,
        "edge_deleted",
        edge,
        [(src[0], src[1], edge.src_node_id), (dst[0], dst[1], edge.dst_node_id)],
    )
    return True


async def list_for_node(
    conn: psycopg.AsyncConnection, node_id: int, *, readable: Sequence[str]
) -> list[EdgeRow]:
    """Edges touching the node whose both ends lie in readable projects."""
    async with conn.cursor(row_factory=class_row(EdgeRow)) as cur:
        await cur.execute(
            f"SELECT {EDGE_COLUMNS} FROM edges WHERE (src_node_id = %s OR dst_node_id = %s)"
            " AND src_project = ANY(%s) AND dst_project = ANY(%s) ORDER BY id",
            (node_id, node_id, list(readable), list(readable)),
        )
        return await cur.fetchall()
