"""The pool sets the idle-in-transaction timeout; `serve` runs every listener and stops."""

import asyncio
import socket

import httpx
import pytest
from conftest import DELEGATE_TOKEN, RELAY_TOKEN, make_settings

from agents_mindmap import runtime


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_pool_sets_idle_in_tx_timeout(empty_db_url):
    pool = await runtime.open_pool(make_settings(empty_db_url))
    try:
        for _ in range(2):
            async with pool.connection() as conn:
                cur = await conn.execute("SHOW idle_in_transaction_session_timeout")
                assert (await cur.fetchone())[0] == "30s"
    finally:
        await pool.close()


async def test_pool_timeout_follows_setting(empty_db_url):
    pool = await runtime.open_pool(make_settings(empty_db_url, idle_in_transaction_timeout_ms=1500))
    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SHOW idle_in_transaction_session_timeout")
            assert (await cur.fetchone())[0] == "1500ms"
    finally:
        await pool.close()


async def _get(port: int, path: str, token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers) as client:
        for _ in range(200):
            try:
                return await client.get(path)
            except httpx.TransportError:
                await asyncio.sleep(0.05)
    pytest.fail(f"listener on {port} did not come up")


@pytest.mark.parametrize("delegate", [True, False])
async def test_serve_runs_every_listener_then_stops(empty_db_url, delegate):
    ports = {"http_port": free_port(), "delegate_port": free_port(), "service_port": free_port()}
    settings = make_settings(
        empty_db_url,
        http_host="127.0.0.1",
        delegate_tokens=[DELEGATE_TOKEN] if delegate else [],
        **ports,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.serve(settings, stop))
    try:
        assert (await _get(ports["http_port"], "/readyz")).status_code == 200
        service = await _get(ports["service_port"], "/mindmap/api/v1/x", RELAY_TOKEN)
        assert service.status_code == 404
        if delegate:
            assert (await _get(ports["delegate_port"], "/readyz")).status_code == 200
        else:
            with pytest.raises(httpx.ConnectError):
                async with httpx.AsyncClient() as client:
                    await client.get(f"http://127.0.0.1:{ports['delegate_port']}/healthz")
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
