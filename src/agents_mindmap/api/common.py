"""Shared route dependencies: the pool and the project lookup."""

from __future__ import annotations

from fastapi import Request
from psycopg_pool import AsyncConnectionPool

from agents_mindmap.projects import ProjectLookup

USER_HEADERS = ("x-forwarded-email", "x-forwarded-user")
USER_MAX = 200


def pool_of(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


def lookup_of(request: Request) -> ProjectLookup:
    return request.app.state.projects
