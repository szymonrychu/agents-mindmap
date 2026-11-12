"""Nodes. All functions take an AsyncConnection inside the caller's transaction and raise
ApiError for the expected refusals (404, 409, 422)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import class_row

from agents_mindmap.api.errors import ApiError
from agents_mindmap.db.events import append_event
from agents_mindmap.db.keys import fmt_key, parse_key
from agents_mindmap.db.versions import insert_version

NODE_COLUMNS = (
    "id, map_id, project, parent_id, topic, notes, current_version, content_rev, embedded_rev,"
    " status, created_by, created_at, updated_by, updated_at, version, client_key"
)


@dataclass(frozen=True)
class NodeRow:
    id: int
    map_id: int
    project: str
    parent_id: int | None
    topic: str
    notes: str
    current_version: int
    content_rev: int
    embedded_rev: int
    status: str
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    version: int
    client_key: str | None

    @property
    def key(self) -> str:
        return fmt_key("N", self.id)

    @property
    def map_key(self) -> str:
        return fmt_key("M", self.map_id)

    @property
    def parent_key(self) -> str | None:
        return None if self.parent_id is None else fmt_key("N", self.parent_id)


async def _select(
    conn: psycopg.AsyncConnection, node_id: int, *, lock: bool = False
) -> NodeRow | None:
    async with conn.cursor(row_factory=class_row(NodeRow)) as cur:
        await cur.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE id = %s{' FOR UPDATE' if lock else ''}",
            (node_id,),
        )
        return await cur.fetchone()


async def get_node(conn: psycopg.AsyncConnection, key: str) -> NodeRow | None:
    return await _select(conn, parse_key("N", key))


async def list_nodes(conn: psycopg.AsyncConnection, map_key: str) -> list[NodeRow]:
    async with conn.cursor(row_factory=class_row(NodeRow)) as cur:
        await cur.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE map_id = %s ORDER BY id",
            (parse_key("M", map_key),),
        )
        return await cur.fetchall()


async def create_node(
    conn: psycopg.AsyncConnection,
    *,
    map_key: str,
    parent_key: str | None,
    topic: str,
    notes: str,
    summary: str,
    status: str,
    actor: str,
    actor_session: str | None = None,
    client_key: str | None = None,
) -> NodeRow:
    """Create a node (and its version 1) under `parent_key`, default the map's root. The same
    (actor, client_key) returns the node made the first time."""
    map_id = parse_key("M", map_key)
    cur = await conn.execute("SELECT project, root_node_id FROM maps WHERE id = %s", (map_id,))
    found = await cur.fetchone()
    if found is None:
        raise ApiError(404, "not_found", "not found")
    project, root_id = found
    parent_id = root_id if parent_key is None else parse_key("N", parent_key)
    cur = await conn.execute(
        "INSERT INTO nodes (map_id, project, parent_id, topic, notes, status, created_by,"
        " updated_by, client_key) SELECT %s, %s, p.id, %s, %s, %s, %s, %s, %s"
        " FROM nodes p WHERE p.id = %s AND p.map_id = %s"
        " ON CONFLICT (created_by, client_key) WHERE client_key IS NOT NULL DO NOTHING"
        " RETURNING id",
        (map_id, project, topic, notes, status, actor, actor, client_key, parent_id, map_id),
    )
    row = await cur.fetchone()
    if row is None:
        if client_key is not None:
            cur = await conn.execute(
                "SELECT id FROM nodes WHERE created_by = %s AND client_key = %s",
                (actor, client_key),
            )
            existing = await cur.fetchone()
            if existing is not None:
                node = await _select(conn, existing[0])
                assert node is not None
                return node
        raise ApiError(422, "parent_invalid", "the parent is not a node of this map")
    node_id = row[0]
    await insert_version(conn, node_id, 1, topic=topic, notes=notes, author=actor, summary=summary)
    await append_event(
        conn,
        project=project,
        map_id=map_id,
        node_id=node_id,
        kind="node_created",
        actor=actor,
        actor_session=actor_session,
        detail={"node": fmt_key("N", node_id), "parent": fmt_key("N", parent_id)},
    )
    node = await _select(conn, node_id)
    assert node is not None
    return node


async def _would_cycle(conn: psycopg.AsyncConnection, node_id: int, new_parent: int) -> bool:
    """True when `node_id` is `new_parent` or one of its ancestors."""
    cur = await conn.execute(
        "WITH RECURSIVE up(id) AS ("
        " SELECT %s::bigint UNION SELECT n.parent_id FROM nodes n JOIN up ON n.id = up.id"
        " WHERE n.parent_id IS NOT NULL) SELECT 1 FROM up WHERE id = %s",
        (new_parent, node_id),
    )
    return await cur.fetchone() is not None


async def update_node(
    conn: psycopg.AsyncConnection,
    key: str,
    *,
    version: int,
    topic: str | None = None,
    notes: str | None = None,
    summary: str | None = None,
    status: str | None = None,
    parent_key: str | None = None,
    actor: str,
    actor_session: str | None = None,
) -> NodeRow:
    """Guarded update. A changed topic or notes needs `summary`, stores the next version row and
    bumps `content_rev`; a changed parent is checked for cycles. 404 unknown, 409
    `version_conflict` (with `current_version`), 422 `summary_required`, `parent_cycle`,
    `parent_invalid`, `root_immovable`."""
    node_id = parse_key("N", key)
    node = await _select(conn, node_id, lock=True)
    if node is None:
        raise ApiError(404, "not_found", "not found")
    if node.version != version:
        raise ApiError(
            409,
            "version_conflict",
            "the node changed since it was read",
            {"current_version": node.version},
        )
    changes: dict[str, Any] = {}
    for name, new in (("topic", topic), ("notes", notes), ("status", status)):
        if new is not None and new != getattr(node, name):
            changes[name] = new
    content = bool(changes.keys() & {"topic", "notes"})
    if content and not summary:
        raise ApiError(422, "summary_required", "a topic or notes change needs a summary")
    if parent_key is not None:
        new_parent = parse_key("N", parent_key)
        if new_parent != node.parent_id:
            if node.parent_id is None:
                raise ApiError(422, "root_immovable", "the root node cannot be moved")
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (node.map_id,))
            cur = await conn.execute(
                "SELECT 1 FROM nodes WHERE id = %s AND map_id = %s", (new_parent, node.map_id)
            )
            if await cur.fetchone() is None:
                raise ApiError(422, "parent_invalid", "the parent is not a node of this map")
            if await _would_cycle(conn, node_id, new_parent):
                raise ApiError(422, "parent_cycle", "the move would put the node under itself")
            changes["parent_id"] = new_parent
    if not changes:
        return node
    sets = [f"{name} = %s" for name in changes]
    params: list[Any] = list(changes.values())
    if content:
        sets += ["current_version = current_version + 1", "content_rev = content_rev + 1"]
    sets += ["version = version + 1", "updated_by = %s", "updated_at = now()"]
    params += [actor, node_id]
    cur = await conn.execute(
        f"UPDATE nodes SET {', '.join(sets)} WHERE id = %s RETURNING current_version", params
    )
    note_version = (await cur.fetchone())[0]  # type: ignore[index]
    updated = await _select(conn, node_id)
    assert updated is not None
    detail = {
        "node": fmt_key("N", node_id),
        "fields": sorted(k.removesuffix("_id") for k in changes),
    }
    if content:
        await insert_version(
            conn,
            node_id,
            note_version,
            topic=updated.topic,
            notes=updated.notes,
            author=actor,
            summary=summary or "",
        )
    for kind, extra in (("node_updated", {}), ("node_versioned", {"version": note_version})):
        if kind == "node_versioned" and not content:
            continue
        await append_event(
            conn,
            project=node.project,
            map_id=node.map_id,
            node_id=node_id,
            kind=kind,
            actor=actor,
            actor_session=actor_session,
            detail={**detail, **extra},
        )
    return updated


async def delete_node(
    conn: psycopg.AsyncConnection, key: str, *, actor: str, actor_session: str | None = None
) -> bool:
    """Hard delete of a node and its subtree. The root of a map cannot be deleted on its own
    (422 `root_immovable`): delete the map."""
    node_id = parse_key("N", key)
    node = await _select(conn, node_id, lock=True)
    if node is None:
        return False
    if node.parent_id is None:
        raise ApiError(422, "root_immovable", "the root node cannot be deleted")
    await conn.execute("DELETE FROM nodes WHERE id = %s", (node_id,))
    await append_event(
        conn,
        project=node.project,
        map_id=node.map_id,
        node_id=node_id,
        kind="node_deleted",
        actor=actor,
        actor_session=actor_session,
        detail={"node": fmt_key("N", node_id)},
    )
    return True
