"""Wait helpers shared by the singleton worker processes (notifier, embedder)."""

from __future__ import annotations

import asyncio

from agents_common.db import listen

from agents_mindmap.db.events import CHANNEL


async def wake_on_notify(url: str, wake: asyncio.Event) -> None:
    """Set `wake` on every `mindmap_changed` NOTIFY (a hint; the workers also poll)."""
    async for _ in listen(url, CHANNEL):
        wake.set()


async def sleep_or_wake(seconds: float, wake: asyncio.Event, stop: asyncio.Event) -> None:
    """Sleep up to `seconds`, returning early when `wake` or `stop` is set."""
    waits = [asyncio.create_task(e.wait()) for e in (wake, stop)]
    try:
        await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()
