"""The FastAPI apps: web listener (users, behind the source allowlist), delegate listener (the
same user routes behind a delegate bearer gate) and service listener (orchestrator relay,
behind the relay bearer gate). All share the error shape and the probes."""

from __future__ import annotations

import contextlib
import ipaddress
import logging
from collections.abc import AsyncIterator, Callable

from agents_common.db import listen
from agents_common.service_gate import EXEMPT_PATHS, ServiceTokenGate
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from agents_mindmap.api import (
    directions,
    edges,
    growth,
    knowledge,
    links,
    maps,
    nodes,
    search,
    stream,
)
from agents_mindmap.api.actors import Actor, agent_actor, user_actor
from agents_mindmap.api.errors import ApiError, error_response
from agents_mindmap.api.stream import DEFAULT_POLL_SECONDS, ChangeHub, ListenFn
from agents_mindmap.projects import ProjectLookup
from agents_mindmap.settings import Settings

log = logging.getLogger(__name__)

PREFIX = "/mindmap/api/v1"


class SourceAllowlist:
    """Refuses peers outside the allowlist. Only the socket peer is read: Forwarded,
    X-Forwarded-For and X-Real-IP are never consulted."""

    def __init__(self, app: ASGIApp, networks: list) -> None:
        self.app = app
        self.networks = list(networks)

    def allowed(self, scope: Scope) -> bool:
        client = scope.get("client")
        if not client:
            return False
        try:
            addr = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return any(addr.version == n.version and addr in n for n in self.networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if (scope["type"] == "http" and scope.get("path") in EXEMPT_PATHS) or self.allowed(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = error_response(403, "forbidden", "source address is not allowed")
        await response(scope, receive, send)


def _base_app(
    settings: Settings,
    pool: AsyncConnectionPool | None,
    title: str,
    resolve_actor: Callable[[Request], Actor],
    *,
    agent: bool,
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            hub = getattr(app.state, "hub", None)
            if hub is not None:
                await hub.stop()
            await app.state.projects.aclose()

    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.pool = pool
    app.state.resolve_actor = resolve_actor
    app.state.projects = ProjectLookup(
        settings.orchestrator_url, settings.orchestrator_token.get_secret_value()
    )

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        # loc and msg only: never the input of the failed field; an unknown field's loc is the
        # caller's own text, so it is not repeated
        first = exc.errors()[0] if exc.errors() else {}
        if first.get("type") == "extra_forbidden":
            return error_response(422, "invalid_request", "unknown field")
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"{where}: {first.get('msg', 'invalid request')}" if where else "invalid request"
        return error_response(422, "invalid_request", message)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            if pool is None:
                raise RuntimeError("no pool")
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        except Exception as exc:
            log.error("readiness check failed: %s", type(exc).__name__)
            return error_response(503, "not_ready", "database is not reachable")
        return JSONResponse({"status": "ok"})

    for module in (maps, nodes, growth):
        app.include_router(module.build_router(agent=agent), prefix=PREFIX)
    for module in (edges, links, directions, knowledge, search):
        app.include_router(module.build_router(), prefix=PREFIX)
    return app


def _user_app(
    settings: Settings,
    pool: AsyncConnectionPool | None,
    title: str,
    add_gate: Callable[[FastAPI], None],
    stream_poll_seconds: float,
    listen_fn: ListenFn,
) -> FastAPI:
    """The user-actor app with the change feed; only the admission gate differs between web
    and delegate."""
    app = _base_app(settings, pool, title, user_actor, agent=False)
    if pool is not None:
        app.state.hub = ChangeHub(
            settings.database_url.get_secret_value(), pool, stream_poll_seconds, listen_fn
        )
    app.include_router(stream.build_router(), prefix=PREFIX)
    add_gate(app)
    return app


def create_web_app(
    settings: Settings,
    pool: AsyncConnectionPool | None,
    stream_poll_seconds: float = DEFAULT_POLL_SECONDS,
    listen_fn: ListenFn = listen,
) -> FastAPI:
    """The user-facing app: source allowlist (the oauth2-proxy sidecar on loopback), no bearer."""

    def gate(app: FastAPI) -> None:
        app.add_middleware(SourceAllowlist, networks=settings.api_networks())

    return _user_app(settings, pool, "agents-mindmap", gate, stream_poll_seconds, listen_fn)


def create_delegate_app(
    settings: Settings,
    pool: AsyncConnectionPool | None,
    stream_poll_seconds: float = DEFAULT_POLL_SECONDS,
    listen_fn: ListenFn = listen,
) -> FastAPI:
    """The cluster-only delegate app: the web app's routes acting as a user, behind a bearer
    gate on the delegate token list (never the relay token)."""
    tokens = [t.get_secret_value() for t in settings.delegate_tokens]
    if not tokens:
        raise ValueError("MINDMAP_DELEGATE_TOKENS: required to serve the delegate listener")

    def gate(app: FastAPI) -> None:
        app.add_middleware(ServiceTokenGate, tokens=tokens)

    return _user_app(
        settings, pool, "agents-mindmap-delegate", gate, stream_poll_seconds, listen_fn
    )


def create_service_app(settings: Settings, pool: AsyncConnectionPool | None) -> FastAPI:
    """The relay-facing app: bearer gate on the relay token; agent identity and project scope
    from the relay's forwarded headers. Agents archive, never hard delete."""
    relay = settings.relay_token.get_secret_value()
    if not relay:
        raise ValueError("MINDMAP_RELAY_TOKEN: required to serve the service listener")
    app = _base_app(settings, pool, "agents-mindmap-service", agent_actor, agent=True)
    app.add_middleware(ServiceTokenGate, tokens=[relay])
    return app
