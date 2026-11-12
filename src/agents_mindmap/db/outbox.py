"""The notice outboxes. The API process writes rows in the change's own transaction; the
notifier process claims, sends and marks them. Rows change only through status-guarded updates.

`task_notices`: one pending row per task; later changes coalesce into it (`changes` counts
them, `due_at` stays at the first change plus the debounce). `direction_notices`: the fallback
for a direction on a map no task is linked to."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import class_row

DEFAULT_DEBOUNCE_SECONDS = 60.0
_debounce_seconds = DEFAULT_DEBOUNCE_SECONDS


def configure(debounce_seconds: float) -> None:
    """Set the debounce of new task notices (`MINDMAP_NOTICE_DEBOUNCE_SECONDS`), at app start."""
    global _debounce_seconds
    _debounce_seconds = debounce_seconds


@dataclass(frozen=True)
class TaskNotice:
    id: int
    task_key: str
    map_id: int
    actor_session: str | None
    attempts: int
    changes: int


@dataclass(frozen=True)
class DirectionNotice:
    id: int
    direction_id: int
    map_id: int
    project: str
    attempts: int


async def enqueue_task_notices(
    conn: psycopg.AsyncConnection, map_id: int, actor_session: str | None
) -> int:
    """Upsert one pending notice per `T-n` linked to the map (at map or node level). The first
    insert sets `due_at`; a later change keeps it and drops `actor_session` unless it is the
    same session, so only a writer whose changes are all its own is excluded. Returns the number
    of tasks touched."""
    cur = await conn.execute(
        "INSERT INTO task_notices (task_key, map_id, actor_session, due_at)"
        " SELECT DISTINCT ref_key, %s, %s, now() + make_interval(secs => %s)"
        " FROM links WHERE map_id = %s AND ref_key ~ '^T-'"
        " ON CONFLICT (task_key) WHERE status = 'pending' DO UPDATE SET"
        " changes = task_notices.changes + 1,"
        " actor_session = CASE WHEN task_notices.actor_session IS NOT DISTINCT FROM"
        " EXCLUDED.actor_session THEN task_notices.actor_session ELSE NULL END",
        (map_id, actor_session, _debounce_seconds, map_id),
    )
    return cur.rowcount


async def claim_due(conn: psycopg.AsyncConnection, limit: int) -> list[TaskNotice]:
    """Due pending notices, row-locked until the caller's transaction ends. The caller ends it
    before any outbound call; a crash before the mark leaves the row pending (at-least-once)."""
    async with conn.cursor(row_factory=class_row(TaskNotice)) as cur:
        await cur.execute(
            "SELECT id, task_key, map_id, actor_session, attempts, changes FROM task_notices"
            " WHERE status = 'pending' AND due_at <= now() AND next_attempt_at <= now()"
            " ORDER BY due_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        )
        return await cur.fetchall()


async def mark_sent(conn: psycopg.AsyncConnection, notice: TaskNotice) -> bool:
    """Sent, unless a change coalesced into the row while the call was out: then it stays
    pending (False) and is sent again, so no change goes unannounced."""
    cur = await conn.execute(
        "UPDATE task_notices SET status = 'sent', sent_at = now()"
        " WHERE id = %s AND status = 'pending' AND changes = %s",
        (notice.id, notice.changes),
    )
    return cur.rowcount == 1


async def mark_dead(conn: psycopg.AsyncConnection, notice_id: int) -> None:
    await conn.execute(
        "UPDATE task_notices SET status = 'dead' WHERE id = %s AND status = 'pending'",
        (notice_id,),
    )


async def schedule_retry(
    conn: psycopg.AsyncConnection, notice_id: int, base: float, cap: float
) -> None:
    """Exponential backoff in `attempts`, capped."""
    await conn.execute(
        "UPDATE task_notices SET attempts = attempts + 1,"
        " next_attempt_at = now() + make_interval(secs => least(%s * power(2, attempts), %s))"
        " WHERE id = %s AND status = 'pending'",
        (base, cap, notice_id),
    )


async def has_linked_task(conn: psycopg.AsyncConnection, map_id: int) -> bool:
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM links WHERE map_id = %s AND ref_key ~ '^T-')", (map_id,)
    )
    return bool((await cur.fetchone())[0])  # type: ignore[index]


async def enqueue_direction_notice(conn: psycopg.AsyncConnection, direction_id: int) -> None:
    await conn.execute("INSERT INTO direction_notices (direction_id) VALUES (%s)", (direction_id,))


async def claim_direction(conn: psycopg.AsyncConnection) -> DirectionNotice | None:
    async with conn.cursor(row_factory=class_row(DirectionNotice)) as cur:
        await cur.execute(
            "SELECT n.id, n.direction_id, d.map_id, d.project, n.attempts"
            " FROM direction_notices n JOIN directions d ON d.id = n.direction_id"
            " WHERE n.status = 'pending' AND n.next_attempt_at <= now()"
            " ORDER BY n.id LIMIT 1 FOR UPDATE OF n SKIP LOCKED"
        )
        return await cur.fetchone()


async def mark_direction_sent(conn: psycopg.AsyncConnection, notice_id: int) -> None:
    await conn.execute(
        "UPDATE direction_notices SET status = 'sent', sent_at = now()"
        " WHERE id = %s AND status = 'pending'",
        (notice_id,),
    )


async def schedule_direction_retry(
    conn: psycopg.AsyncConnection, notice_id: int, base: float, cap: float
) -> None:
    await conn.execute(
        "UPDATE direction_notices SET attempts = attempts + 1,"
        " next_attempt_at = now() + make_interval(secs => least(%s * power(2, attempts), %s))"
        " WHERE id = %s AND status = 'pending'",
        (base, cap, notice_id),
    )
