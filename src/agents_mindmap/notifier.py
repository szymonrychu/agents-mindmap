"""The notifier process (`mindmap notifier`): drains the task-notice outbox to the taskboard
delegate listener and the direction fallback to the project's live sessions.

Delivery is at-least-once: a row is claimed in a short transaction, the call happens with no
transaction open, and the row is marked afterwards by a status-guarded update; a crash in
between re-sends it. Notices are content-free: a task key, a map key, a link."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

import httpx
from agents_common.db import listen
from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)
from agents_common.runtime import advisory_singleton, run_service
from psycopg_pool import AsyncConnectionPool

from agents_mindmap.db import outbox
from agents_mindmap.db.events import CHANNEL
from agents_mindmap.db.keys import fmt_key
from agents_mindmap.runtime import open_pool
from agents_mindmap.settings import Settings

log = logging.getLogger(__name__)

LOCK_NAME = "agents-mindmap-notifier"
TASKBOARD_TIMEOUT_SECONDS = 5.0
TASKBOARD_PREFIX = "/taskboard/api/v1"


def direction_text(map_key: str, direction_key: str, public_url: str) -> str:
    """The whole fallback message: keys and a link, never any direction content."""
    return (
        f"Mind map {map_key} got direction {direction_key}: "
        f"{public_url.rstrip('/')}/mindmaps/{map_key}?direction={direction_key}"
    )


class TaskboardClient:
    """`POST /taskboard/api/v1/tasks/{key}/external-changes` on the taskboard delegate listener,
    bearer `taskboard_token`. Returns the HTTP status; a network failure raises httpx.HTTPError.
    The response body is never read or logged."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = TASKBOARD_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def external_change(self, key: str, ref: str, actor_session: str | None) -> int:
        resp = await self._http.post(
            f"{TASKBOARD_PREFIX}/tasks/{key}/external-changes",
            json={"source": "mindmap", "ref": ref, "actor_session": actor_session},
        )
        return resp.status_code


class SessionLookup:
    """Live orchestrator sessions of a project: `GET /api/v1/sessions?project=..&superleaders=
    include` on the service port (copied from agents-taskboard, without the label filter: the
    fallback goes to every live session of the project). The response is a plain list."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def live_session_ids(self, project: str) -> list[str]:
        """Ids of the active sessions in `project` plus the superleaders; OrchestratorUnavailable
        on any failure, so the caller retries. A 422 (project unknown to the orchestrator) is not
        retryable: no sessions."""
        path = "/api/v1/sessions"
        try:
            resp = await self._http.get(
                path, params=[("project", project), ("superleaders", "include")]
            )
        except httpx.HTTPError as exc:
            raise OrchestratorUnavailable(f"GET {path}: {exc!r}") from exc
        if resp.status_code == 422:
            log.warning("session lookup: project %r unknown to the orchestrator", project)
            return []
        if resp.status_code != 200:
            raise OrchestratorUnavailable(f"GET {path}: HTTP {resp.status_code}")
        try:
            return [str(s["id"]) for s in resp.json() if s.get("desired", "active") == "active"]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise OrchestratorUnavailable(f"GET {path}: malformed response: {exc!r}") from exc


def _session_or_none(value: str | None) -> str | None:
    """The taskboard requires a UUID; anything else is sent as null."""
    if value is None:
        return None
    try:
        uuid.UUID(value)
    except ValueError:
        return None
    return value


async def send_next_task(
    pool: AsyncConnectionPool,
    client: TaskboardClient,
    backoff_base: float,
    backoff_max: float,
) -> bool | None:
    """Send one due task notice. None: nothing due; True: ended (sent, or dead on a 404);
    False: the call failed and the row waits on its own backoff."""
    async with pool.connection() as conn:
        found = await outbox.claim_due(conn, 1)
    if not found:
        return None
    (notice,) = found
    # no transaction is open here: the claim committed, the mark below opens its own
    ref = fmt_key("M", notice.map_id)
    try:
        status = await client.external_change(
            notice.task_key, ref, _session_or_none(notice.actor_session)
        )
    except httpx.HTTPError as exc:
        log.warning("task notice %s: call failed, will retry: %s", notice.id, type(exc).__name__)
        status = 599
    if status == 404:
        log.info("task notice %s: task unknown to the taskboard; dead", notice.id)
        async with pool.connection() as conn:
            await outbox.mark_dead(conn, notice.id)
        return True
    if 200 <= status < 300:
        async with pool.connection() as conn:
            await outbox.mark_sent(conn, notice)
        return True
    if status != 599:
        log.warning("task notice %s: taskboard answered HTTP %s, will retry", notice.id, status)
    async with pool.connection() as conn:
        await outbox.schedule_retry(conn, notice.id, backoff_base, backoff_max)
    return False


async def send_next_direction(
    pool: AsyncConnectionPool,
    lookup: SessionLookup,
    client: OrchestratorClient,
    public_url: str,
    backoff_base: float,
    backoff_max: float,
) -> bool | None:
    """Send one fallback direction notice to every live session of the map's project. None:
    nothing pending; True: ended (also with no live session); False: a lookup or send failed
    (sessions already messaged may get a duplicate on the retry)."""
    async with pool.connection() as conn:
        notice = await outbox.claim_direction(conn)
    if notice is None:
        return None
    text = direction_text(
        fmt_key("M", notice.map_id), fmt_key("D", notice.direction_id), public_url
    )
    try:
        for session_id in await lookup.live_session_ids(notice.project):
            try:
                await client.post_message(session_id, text)
            except NotFound:
                log.info("direction notice %s: session %s is gone", notice.id, session_id)
    except (OrchestratorError, OrchestratorUnavailable) as exc:
        log.warning("direction notice %s failed, will retry: %s", notice.id, type(exc).__name__)
        async with pool.connection() as conn:
            await outbox.schedule_direction_retry(conn, notice.id, backoff_base, backoff_max)
        return False
    async with pool.connection() as conn:
        await outbox.mark_direction_sent(conn, notice.id)
    return True


async def _wake_on_notify(url: str, wake: asyncio.Event) -> None:
    async for _ in listen(url, CHANNEL):
        wake.set()


async def _sleep(seconds: float, wake: asyncio.Event, stop: asyncio.Event) -> None:
    waits = [asyncio.create_task(e.wait()) for e in (wake, stop)]
    try:
        await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()


async def run_notifier(
    settings: Settings,
    stop: asyncio.Event,
    *,
    pool: AsyncConnectionPool,
    taskboard: TaskboardClient | None = None,
    lookup: SessionLookup | None = None,
    orchestrator: OrchestratorClient | None = None,
) -> None:
    """Drain due task notices and direction notices, then wait for NOTIFY mindmap_changed or the
    poll interval. A failed send only reschedules its own row. Runs until `stop` is set; one
    notifier cluster-wide (advisory lock)."""
    token = settings.orchestrator_token.get_secret_value()
    owned = []
    if taskboard is None:
        taskboard = TaskboardClient(
            settings.taskboard_url, settings.taskboard_token.get_secret_value()
        )
        owned.append(taskboard)
    if lookup is None:
        lookup = SessionLookup(settings.orchestrator_url, token)
        owned.append(lookup)
    if orchestrator is None:
        orchestrator = OrchestratorClient(settings.orchestrator_url, token)
        owned.append(orchestrator)
    wake = asyncio.Event()
    listener = asyncio.create_task(_wake_on_notify(settings.database_url.get_secret_value(), wake))
    base, cap = settings.notifier_poll_seconds, settings.notifier_backoff_max_seconds
    try:
        async with advisory_singleton(pool, LOCK_NAME):
            while not stop.is_set():
                wake.clear()
                task = await send_next_task(pool, taskboard, base, cap)
                direction = await send_next_direction(
                    pool, lookup, orchestrator, settings.public_base_url, base, cap
                )
                if task is None and direction is None:
                    await _sleep(base, wake, stop)
    finally:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        for resource in owned:
            await resource.aclose()


async def serve(settings: Settings, stop: asyncio.Event) -> None:
    """The `mindmap notifier` process body. Off (returns at once) without a taskboard url."""
    if not settings.taskboard_url:
        log.warning("MINDMAP_TASKBOARD_URL is not set: the notifier is off")
        return
    pool = await open_pool(settings, min_size=2, max_size=4)
    try:
        await run_notifier(settings, stop, pool=pool)
    finally:
        await pool.close()


def main(settings: Settings) -> int:
    """Run the notifier until SIGTERM or SIGINT; the exit code."""
    logging.basicConfig(level=settings.log_level.upper(), force=True)
    return asyncio.run(
        run_service(
            lambda stop: serve(settings, stop), grace_seconds=settings.shutdown_grace_seconds
        )
    )
