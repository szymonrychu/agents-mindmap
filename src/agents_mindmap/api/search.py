"""Read routes over the graph: node traversal, map graph and lexical search. Every actor reads
the active projects (spec 3.5); a user addressing a key in an archived project is unscoped, so
the walk may also enter that start project, and no other inactive one."""

from __future__ import annotations

from typing import Annotated, Literal, get_args

from fastapi import APIRouter, Query, Request

from agents_mindmap.api.actors import Actor, ActorDep, require_readable
from agents_mindmap.api.common import lookup_of, pool_of
from agents_mindmap.api.errors import ApiError
from agents_mindmap.api.schemas import (
    EdgeKind,
    HitView,
    MapKey,
    Project,
    SearchResponse,
    SubgraphView,
)
from agents_mindmap.db import maps as maps_db
from agents_mindmap.db import nodes as nodes_db
from agents_mindmap.db import search as search_db
from agents_mindmap.db import traverse as traverse_db
from agents_mindmap.projects import ProjectLookup

Scope = Literal["own", "all"]
Follow = Literal["out", "in", "both"]
Mode = Literal["hybrid", "lexical", "semantic"]
EDGE_KINDS = list(get_args(EdgeKind))
Q_MAX = 200


async def scoped(actor: Actor, lookup: ProjectLookup, scope: Scope) -> list[str]:
    """The projects a read covers: every active project, or with `own` the caller's own."""
    active = await actor.readable(lookup)
    if scope == "own" and actor.projects is not None:
        return [p for p in active if p in actor.projects]
    return active


def parse_kinds(kinds: str | None) -> list[str]:
    if kinds is None:
        return EDGE_KINDS
    listed = kinds.split(",")
    if not all(k in EDGE_KINDS for k in listed):
        raise ApiError(422, "invalid_request", "kinds: unknown edge kind")
    return listed


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/nodes/{key}/traverse", response_model=SubgraphView)
    async def traverse(
        request: Request,
        actor: ActorDep,
        key: str,
        depth: Annotated[int, Query(ge=0)] = traverse_db.DEFAULT_DEPTH,
        kinds: Annotated[str | None, Query(max_length=200)] = None,
        follow: Follow = "both",
        tree: bool = True,
        scope: Scope = "all",
    ) -> SubgraphView:
        kind_list = parse_kinds(kinds)
        readable = await scoped(actor, lookup_of(request), scope)
        async with pool_of(request).connection() as conn:
            node = await nodes_db.get_node(conn, key)
            if node is None:
                raise ApiError(404, "not_found", "not found")
            await require_readable(actor, lookup_of(request), node.project)
            if node.project not in readable:
                if actor.kind == "agent":
                    raise ApiError(404, "not_found", "not found")
                readable = [*readable, node.project]
            graph = await traverse_db.traverse(
                conn,
                node.id,
                depth=depth,
                kinds=kind_list,
                follow=follow,
                tree=tree,
                readable=readable,
            )
        return SubgraphView.of(graph)

    @router.get("/maps/{key}/graph", response_model=SubgraphView)
    async def map_graph(
        request: Request,
        actor: ActorDep,
        key: str,
        depth: Annotated[int, Query(ge=0)] = traverse_db.DEFAULT_GRAPH_DEPTH,
    ) -> SubgraphView:
        readable = await scoped(actor, lookup_of(request), "all")
        async with pool_of(request).connection() as conn:
            row = await maps_db.get_map(conn, key)
            if row is None:
                raise ApiError(404, "not_found", "not found")
            await require_readable(actor, lookup_of(request), row.project)
            if row.project not in readable:
                readable = [*readable, row.project]
            graph = await traverse_db.map_graph(conn, row.id, depth=depth, readable=readable)
        return SubgraphView.of(graph)

    @router.get("/search", response_model=SearchResponse)
    async def search(
        request: Request,
        actor: ActorDep,
        q: Annotated[str, Query(min_length=1, max_length=Q_MAX)],
        project: Annotated[Project | None, Query()] = None,
        map: Annotated[MapKey | None, Query()] = None,  # noqa: A002 - the query parameter name
        status: Literal["seed", "growing", "settled", "stale", "archived"] | None = None,
        scope: Scope = "own",
        mode: Mode = "hybrid",
        limit: Annotated[int, Query(ge=1)] = 20,
    ) -> SearchResponse:
        # no embedder yet: every mode answers with the lexical signals and `semantic: false`
        # a user searches unscoped (spec 3.5, archived projects included); agents the active ones
        projects = None if actor.kind == "user" else await scoped(actor, lookup_of(request), scope)
        if project is not None:
            projects = [project] if projects is None else [p for p in projects if p == project]
        text = q.replace("\x00", " ").strip()
        async with pool_of(request).connection() as conn:
            hits = (
                await search_db.lexical_search(
                    conn,
                    text,
                    projects=projects,
                    map_key=map,
                    status=status,
                    limit=min(limit, search_db.MAX_LIMIT),
                )
                if text and projects != []
                else []
            )
            pending = await search_db.pending_embedding(conn, projects)
        return SearchResponse(
            items=[HitView.of(h) for h in hits], semantic=False, pending_embedding=pending
        )

    return router
