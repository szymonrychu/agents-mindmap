"""The event log. Every write path appends through `append_event`, in the writer's
transaction; the `mindmap_changed` NOTIFY is a hint for the SSE stream and the notifier."""

from __future__ import annotations

from typing import Any

import psycopg
from agents_common.db.pool import notify
from psycopg.types.json import Jsonb

CHANNEL = "mindmap_changed"


async def append_event(
    conn: psycopg.AsyncConnection,
    *,
    project: str,
    map_id: int | None,
    node_id: int | None,
    kind: str,
    actor: str,
    actor_session: str | None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Log one event. `detail` holds ids, keys and field names only, never note text. A write
    touching two maps appends one event per map."""
    await conn.execute(
        "INSERT INTO events (project, map_id, node_id, kind, actor, actor_session, detail)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (project, map_id, node_id, kind, actor, actor_session, Jsonb(detail or {})),
    )
    await notify(conn, CHANNEL, project)
