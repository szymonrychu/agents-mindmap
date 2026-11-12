"""Links from a map or a node to a task (`T-n`) or spec (`S-n`) of the taskboard. Many to many;
the target is not validated (a task may be created later or live in another project)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg import errors
from psycopg.rows import class_row

from agents_mindmap.api.actors import Actor
from agents_mindmap.api.errors import ApiError
from agents_mindmap.db.events import append_event
from agents_mindmap.db.keys import fmt_key, parse_key

LINK_COLUMNS = "id, map_id, project, node_id, ref_key, relation, note, created_by, created_at"
LIMIT = 200


@dataclass(frozen=True)
class LinkRow:
    id: int
    map_id: int
    project: str
    node_id: int | None
    ref_key: str
    relation: str
    note: str
    created_by: str
    created_at: datetime

    @property
    def map_key(self) -> str:
        return fmt_key("M", self.map_id)

    @property
    def node_key(self) -> str | None:
        return None if self.node_id is None else fmt_key("N", self.node_id)


async def create_link(
    conn: psycopg.AsyncConnection,
    *,
    map_id: int,
    project: str,
    node_id: int | None,
    ref_key: str,
    relation: str,
    note: str,
    actor: Actor,
    ignore_existing: bool = False,
) -> LinkRow | None:
    """Insert the link; 409 `link_exists` for an identical one (None with `ignore_existing`)."""
    try:
        async with conn.transaction(), conn.cursor(row_factory=class_row(LinkRow)) as cur:
            await cur.execute(
                f"INSERT INTO links (map_id, project, node_id, ref_key, relation, note,"
                f" created_by) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {LINK_COLUMNS}",
                (map_id, project, node_id, ref_key, relation, note, actor.author),
            )
            link = await cur.fetchone()
    except errors.UniqueViolation:
        if ignore_existing:
            return None
        raise ApiError(409, "link_exists", "the link exists") from None
    assert link is not None
    await append_event(
        conn,
        project=project,
        map_id=map_id,
        node_id=node_id,
        kind="link_created",
        actor=actor.author,
        actor_session=actor.session,
        detail={"link": link.id, "ref": ref_key, "relation": relation},
    )
    return link


async def delete_link(conn: psycopg.AsyncConnection, link_id: int, *, actor: Actor) -> bool:
    cur = await conn.execute(
        "DELETE FROM links WHERE id = %s RETURNING map_id, project, node_id, ref_key, relation",
        (link_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    await append_event(
        conn,
        project=row[1],
        map_id=row[0],
        node_id=row[2],
        kind="link_deleted",
        actor=actor.author,
        actor_session=actor.session,
        detail={"link": link_id, "ref": row[3], "relation": row[4]},
    )
    return True


async def list_by_ref(
    conn: psycopg.AsyncConnection, ref_key: str, *, readable: Sequence[str]
) -> list[LinkRow]:
    async with conn.cursor(row_factory=class_row(LinkRow)) as cur:
        await cur.execute(
            f"SELECT {LINK_COLUMNS} FROM links WHERE ref_key = %s AND project = ANY(%s)"
            f" ORDER BY id LIMIT {LIMIT}",
            (ref_key, list(readable)),
        )
        return await cur.fetchall()


async def list_for_node(conn: psycopg.AsyncConnection, node_id: int) -> list[LinkRow]:
    async with conn.cursor(row_factory=class_row(LinkRow)) as cur:
        await cur.execute(
            f"SELECT {LINK_COLUMNS} FROM links WHERE node_id = %s ORDER BY id", (node_id,)
        )
        return await cur.fetchall()


async def node_target(conn: psycopg.AsyncConnection, node_key: str) -> tuple[int, str, int]:
    """(map id, project, node id) of a node; 404 unknown."""
    node_id = parse_key("N", node_key)
    cur = await conn.execute("SELECT map_id, project FROM nodes WHERE id = %s", (node_id,))
    row = await cur.fetchone()
    if row is None:
        raise ApiError(404, "not_found", "not found")
    return row[0], row[1], node_id


async def map_target(conn: psycopg.AsyncConnection, map_key: str) -> tuple[int, str]:
    map_id = parse_key("M", map_key)
    cur = await conn.execute("SELECT project FROM maps WHERE id = %s", (map_id,))
    row = await cur.fetchone()
    if row is None:
        raise ApiError(404, "not_found", "not found")
    return map_id, row[0]
