"""The change feed (web and delegate listeners only): `GET /events` pages the events table as
JSON and `GET /stream` pushes `mindmap_changed` server-sent events, each with `id: <events.id>`
of the row that caused it and the data `{"project", "map", "node", "kind", "id"}` (keys and kind
only, never note text).

The events table is the only source: one hub per process polls it with an id cursor and fans
each change out to the in-process subscribers. NOTIFY on `mindmap_changed` (one shared LISTEN
connection) only wakes the poll early, so a missed NOTIFY, or a down listener, delays an update
by at most one poll interval and never duplicates one.

Catch-up: a client that reconnects with `Last-Event-ID: <n>` or `?after=<n>` (the larger
wins) first gets, in id order and within `?project=`, one event per map and node changed past
`n`, then the live feed. When `n` is below the oldest kept events row the stream sends one
`reset` instead: the client refetches.

The hub starts lazily with the first subscriber, stops when the last one leaves and on app
shutdown. FastAPI's SSE response sends the 15 s keepalive comment while the stream is idle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from agents_common.db import listen
from fastapi import APIRouter, Header, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from agents_mindmap.api.common import pool_of
from agents_mindmap.api.schemas import Project
from agents_mindmap.db.events import CHANNEL
from agents_mindmap.db.keys import fmt_key

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 5.0
BATCH = 1000
EVENTS_DEFAULT_LIMIT = 200
EVENTS_MAX_LIMIT = 1000
LISTEN_RETRY_SECONDS = 0.5
LISTEN_RETRY_MAX_SECONDS = 30.0
READY_TIMEOUT_SECONDS = 10.0
EVENT_NAME = "mindmap_changed"
_COLUMNS = "e.id, e.project, e.map_id, e.node_id, e.kind"

ListenFn = Callable[[str, str], AsyncIterator[str]]


def _key(prefix: str, id_: int | None) -> str | None:
    return None if id_ is None else fmt_key(prefix, id_)


@dataclass(frozen=True)
class Change:
    """One map or node change: the events row id that caused it and the SSE payload."""

    event_id: int
    subject: tuple[int | None, int | None]  # (map id, node id)
    project: str
    data: dict[str, Any]

    def sse(self) -> ServerSentEvent:
        return ServerSentEvent(data=self.data, event=EVENT_NAME, id=str(self.event_id))


def change_of(row: tuple) -> Change:
    """A Change from a `_COLUMNS` row."""
    event_id, project, map_id, node_id, kind = row
    data = {
        "project": project,
        "map": _key("M", map_id),
        "node": _key("N", node_id),
        "kind": kind,
        "id": event_id,
    }
    return Change(event_id, (map_id, node_id), project, data)


class Subscription:
    """The latest change per map and node since the subscriber last looked; a dict, so a slow
    client coalesces instead of growing a queue."""

    def __init__(self, hub: ChangeHub) -> None:
        self._hub = hub
        self._changes: dict[tuple[int | None, int | None], Change] = {}
        self._wake = asyncio.Event()

    def offer(self, change: Change) -> None:
        self._changes[change.subject] = change
        self._wake.set()

    async def get(self) -> list[Change]:
        await self._wake.wait()
        self._wake.clear()
        changes, self._changes = self._changes, {}
        return sorted(changes.values(), key=lambda c: c.event_id)

    def close(self) -> None:
        self._hub.unsubscribe(self)


class ChangeHub:
    def __init__(
        self,
        dsn: str,
        pool: AsyncConnectionPool,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        listen_fn: ListenFn = listen,
    ) -> None:
        self._dsn = dsn
        self._pool = pool
        self._poll_seconds = poll_seconds
        self._listen = listen_fn
        self._subs: set[Subscription] = set()
        self._tasks: list[asyncio.Task] = []
        self._wake = asyncio.Event()
        self._ready = asyncio.Event()

    @property
    def pool(self) -> AsyncConnectionPool:
        return self._pool

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    def subscriber_count(self) -> int:
        return len(self._subs)

    def subscribe(self) -> Subscription:
        sub = Subscription(self)
        self._subs.add(sub)
        if not self._tasks:
            self._ready.clear()
            self._tasks = [
                asyncio.create_task(self._listen_loop(), name="mindmap-listen"),
                asyncio.create_task(self._poll_loop(), name="mindmap-poll"),
            ]
        return sub

    async def ready(self, timeout: float = READY_TIMEOUT_SECONDS) -> None:
        """Wait until the poll cursor is set, so every change past it reaches subscribers."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout)

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)
        if not self._subs:
            self._cancel()

    def _cancel(self) -> list[asyncio.Task]:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        return tasks

    async def stop(self) -> None:
        self._subs.clear()
        for task in self._cancel():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _publish(self, change: Change) -> None:
        for sub in self._subs:
            sub.offer(change)

    async def _listen_loop(self) -> None:
        """Run the shared LISTEN that wakes the poll; any unexpected error restarts it after a
        capped backoff (agents-common reconnects only on connection errors)."""
        delay = LISTEN_RETRY_SECONDS
        while True:
            try:
                async for _ in self._listen(self._dsn, CHANNEL):
                    delay = LISTEN_RETRY_SECONDS
                    self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s listener failed; restarting in %.1fs", CHANNEL, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, LISTEN_RETRY_MAX_SECONDS)

    async def _poll_loop(self) -> None:
        cursor = None
        while True:
            try:
                self._wake.clear()
                if cursor is None:
                    cursor = await max_event_id(self._pool)
                    self._ready.set()
                else:
                    cursor = await self._poll_once(cursor)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("change poll failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._poll_seconds)

    async def _poll_once(self, cursor: int) -> int:
        """Publish every change past the cursor; return the new cursor."""
        while True:
            async with self._pool.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM events e WHERE e.id > %s ORDER BY e.id LIMIT %s",
                    (cursor, BATCH),
                )
                rows = await cur.fetchall()
            for row in rows:
                change = change_of(row)
                cursor = change.event_id
                self._publish(change)
            if len(rows) < BATCH:
                return cursor


async def max_event_id(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT coalesce(max(id), 0) FROM events")
        row = await cur.fetchone()
    return int(row[0])


async def replay(
    pool: AsyncConnectionPool, after: int, project: str | None
) -> tuple[int | None, list[Change]]:
    """(reset id, changes): the latest change per map and node past `after`, in id order and
    within `project`; or, when `after` is below the oldest kept events row, the id of the
    newest row to send with one `reset` and no changes."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT min(id), max(id) FROM events")
        oldest, newest = await cur.fetchone()
        if oldest is not None and after < oldest - 1:
            return newest, []
        only, params = ("", []) if project is None else (" AND e.project = %s", [project])
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM events e WHERE e.id IN ("
            " SELECT max(e.id) FROM events e"
            f" WHERE e.id > %s{only} GROUP BY e.map_id, e.node_id)"
            " ORDER BY e.id",
            (after, *params),
        )
        return None, [change_of(row) for row in await cur.fetchall()]


def _cursor(header: str | None, query: int | None) -> int | None:
    """The larger of the `Last-Event-ID` header (ignored when not a number) and `after`."""
    ids = [query] if query is not None else []
    if header is not None and header.strip().isdigit():
        ids.append(int(header.strip()))
    return max(ids) if ids else None


class EventView(BaseModel):
    id: int
    project: str
    map: str | None
    node: str | None
    kind: str
    actor: str
    at: datetime
    detail: dict[str, Any]


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/events", response_model=list[EventView])
    async def events(
        request: Request,
        project: Annotated[Project | None, Query()] = None,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=EVENTS_MAX_LIMIT)] = EVENTS_DEFAULT_LIMIT,
    ) -> list[EventView]:
        """Events past `after` in id order (users read unscoped, spec 3.5)."""
        only, params = ("", []) if project is None else (" AND e.project = %s", [project])
        async with pool_of(request).connection() as conn:
            cur = await conn.execute(
                "SELECT e.id, e.project, e.map_id, e.node_id, e.kind, e.actor, e.at, e.detail"
                f" FROM events e WHERE e.id > %s{only} ORDER BY e.id LIMIT %s",
                (after, *params, limit),
            )
            rows = await cur.fetchall()
        return [
            EventView(
                id=i,
                project=p,
                map=_key("M", m),
                node=_key("N", n),
                kind=k,
                actor=a,
                at=at,
                detail=d,
            )
            for i, p, m, n, k, a, at, d in rows
        ]

    @router.get("/stream", response_class=EventSourceResponse)
    async def stream(
        request: Request,
        project: Annotated[Project | None, Query()] = None,
        after: Annotated[int | None, Query(ge=0)] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> AsyncIterator[ServerSentEvent]:
        """Server-sent `mindmap_changed` events (and `reset` when the cursor is too old)."""
        hub: ChangeHub = request.app.state.hub
        sub = hub.subscribe()
        try:
            sent = 0
            cursor = _cursor(last_event_id, after)
            if cursor is not None:
                await hub.ready()
                reset, changes = await replay(hub.pool, cursor, project)
                if reset is not None:
                    sent = reset
                    yield ServerSentEvent(data={}, event="reset", id=str(reset))
                for change in changes:
                    sent = change.event_id
                    yield change.sse()
            while True:
                for change in await sub.get():
                    if change.event_id <= sent:
                        continue
                    if project is None or project == change.project:
                        yield change.sse()
        finally:
            sub.close()

    return router
