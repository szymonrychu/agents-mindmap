"""Shared route dependencies: the pool, the project lookup and the user author string."""

from __future__ import annotations

from fastapi import Request
from psycopg_pool import AsyncConnectionPool

from agents_mindmap.projects import ProjectLookup

USER_HEADERS = ("x-forwarded-email", "x-forwarded-user")
USER_MAX = 200
ANONYMOUS = "user:unknown"


def pool_of(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


def lookup_of(request: Request) -> ProjectLookup:
    return request.app.state.projects


def user_author(request: Request) -> str:
    """`user:<who>` from the identity headers of the oauth2-proxy sidecar (email first, then
    user, as agents-vault); `user:unknown` when both are absent, over-long or not printable
    (delegate callers act for the user without a proxy)."""
    for header in USER_HEADERS:
        who = request.headers.get(header, "").strip()
        if who and len(who) <= USER_MAX and who.isprintable():
            return f"user:{who}"
    return ANONYMOUS
