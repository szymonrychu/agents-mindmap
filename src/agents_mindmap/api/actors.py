"""Who is acting: a user on the web and delegate listeners, an agent on the service listener.

The web and delegate listeners never read agent headers. On the service listener the bearer
gate (the orchestrator relay's token) has already run when a route resolves the actor, so the
`X-Orch-Session`, `X-Orch-Slug` and `X-Orch-Projects` headers it forwards can be trusted.
`X-Orch-Projects` is the caller's write scope: `*` (every project) or 1..10 distinct project
slugs, home project first. Reads are open to every active project for every actor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Request

from agents_mindmap.api.common import USER_HEADERS, USER_MAX
from agents_mindmap.api.errors import ApiError
from agents_mindmap.projects import ProjectLookup

SESSION_HEADER = "X-Orch-Session"
SLUG_HEADER = "X-Orch-Slug"
PROJECTS_HEADER = "X-Orch-Projects"
ALL_PROJECTS = "*"
ANONYMOUS = "unknown"
PROJECT_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
MAX_PROJECTS = 10


@dataclass(frozen=True)
class Actor:
    kind: Literal["user", "agent"]
    id: str  # user: the proxy identity; agent: the session id
    slug: str | None = None
    projects: frozenset[str] | None = None  # None: every project
    home_project: str | None = None

    @property
    def is_superleader(self) -> bool:
        """An agent with scope `*` (the relay sends it only for a superleader session)."""
        return self.kind == "agent" and self.projects is None

    @property
    def author(self) -> str:
        """The author string stored on rows: `user:<email>` or `session:<slug>`."""
        if self.kind == "user":
            return f"user:{self.id}"
        return f"session:{self.slug or self.id}"

    @property
    def session(self) -> str | None:
        """The writing session id, None for a user."""
        return self.id if self.kind == "agent" else None

    @property
    def origin(self) -> Literal["user", "agent"]:
        return self.kind

    def may_write(self, project: str) -> bool:
        return self.kind == "user" or self.projects is None or project in self.projects

    async def readable(self, lookup: ProjectLookup) -> list[str]:
        """The active project slugs: the read scope of every actor, user included."""
        return await lookup.active_slugs()


def require_writable(actor: Actor, project: str) -> None:
    if not actor.may_write(project):
        raise ApiError(403, "project_forbidden", "the project is outside the caller's scope")


async def require_readable(actor: Actor, lookup: ProjectLookup, project: str) -> None:
    """Agents read active projects only (404 otherwise, as an unknown key); a user reads by key
    unscoped (spec 3.5), archived projects included."""
    if actor.kind == "agent" and project not in await actor.readable(lookup):
        raise ApiError(404, "not_found", "not found")


def user_actor(request: Request) -> Actor:
    """The user behind the oauth2-proxy sidecar (email first, then user, as agents-vault);
    `unknown` when both are absent, over-long or not printable (delegate callers act for the
    user without a proxy)."""
    for header in USER_HEADERS:
        who = request.headers.get(header, "").strip()
        if who and len(who) <= USER_MAX and who.isprintable():
            return Actor(kind="user", id=who)
    return Actor(kind="user", id=ANONYMOUS)


def agent_actor(request: Request) -> Actor:
    session = request.headers.get(SESSION_HEADER, "").strip()
    if not session or len(session) > 200 or not session.isprintable():
        raise ApiError(400, "actor_required", "a session header is required")
    scope = request.headers.get(PROJECTS_HEADER, "").strip()
    slug = request.headers.get(SLUG_HEADER, "").strip() or None
    if slug is not None and (len(slug) > 200 or not slug.isprintable()):
        slug = None
    if scope == ALL_PROJECTS:
        return Actor(kind="agent", id=session, slug=slug)
    listed = scope.split(",")
    if (
        len(listed) > MAX_PROJECTS
        or len(set(listed)) != len(listed)
        or not all(PROJECT_SLUG.fullmatch(p) for p in listed)
    ):
        raise ApiError(400, "actor_required", "a project scope header is required")
    return Actor(
        kind="agent", id=session, slug=slug, projects=frozenset(listed), home_project=listed[0]
    )


def get_actor(request: Request) -> Actor:
    """Route dependency: each app sets `state.resolve_actor` (user or agent)."""
    return request.app.state.resolve_actor(request)


ActorDep = Annotated[Actor, Depends(get_actor)]
