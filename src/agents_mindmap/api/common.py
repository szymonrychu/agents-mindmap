"""Shared route dependencies: the pool, the project lookup and the user author string."""

from __future__ import annotations

import re

from fastapi import Request
from psycopg_pool import AsyncConnectionPool

from agents_mindmap.projects import ProjectLookup

EMAIL_HEADER = "X-Forwarded-Email"
_EMAIL = re.compile(r"[^\s@<>\"']{1,64}@[^\s@<>\"']{1,255}")


def pool_of(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


def lookup_of(request: Request) -> ProjectLookup:
    return request.app.state.projects


def user_author(request: Request) -> str:
    """`user:<email>` from the header the oauth2-proxy sidecar sets; `user:unknown` when it is
    absent or malformed (delegate callers act for the user without a proxy)."""
    email = request.headers.get(EMAIL_HEADER, "").strip()
    return f"user:{email}" if _EMAIL.fullmatch(email) else "user:unknown"
