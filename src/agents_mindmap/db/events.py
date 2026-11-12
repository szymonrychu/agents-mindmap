"""The event log. Every write path appends through `append_event`, in the writer's
transaction; the `mindmap_changed` NOTIFY is a hint for the SSE stream and the notifier. The
same call enqueues the task notices of the map (the single choke point of every write)."""

from __future__ import annotations

from typing import Any

import psycopg
from agents_common.db.pool import notify
from psycopg.types.json import Jsonb

from agents_mindmap.db.outbox import enqueue_task_notices

CHANNEL = "mindmap_changed"

# Every kind a write path appends. `node_embedded` is the embedder's and never notifies.
EVENT_KINDS = (
    "map_created",
    "map_updated",
    "map_deleted",
    "node_created",
    "node_updated",
    "node_versioned",
    "node_deleted",
    "node_embedded",
    "growth_area_changed",
    "edge_created",
    "edge_deleted",
    "link_created",
    "link_deleted",
    "direction_created",
    "direction_acknowledged",
    "direction_resolved",
    "direction_withdrawn",
)
SILENT_KINDS = frozenset({"node_embedded"})


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
    if map_id is not None and kind not in SILENT_KINDS:
        await enqueue_task_notices(conn, map_id, actor_session)
    await notify(conn, CHANNEL, project)
