"""Node and version routes. The web and delegate listeners act as the user, the service
listener as the calling agent (same routes, no hard delete)."""

from __future__ import annotations

from collections.abc import Sequence

import psycopg
from fastapi import APIRouter, Request, Response

from agents_mindmap.api.actors import Actor, ActorDep, require_readable, require_writable
from agents_mindmap.api.common import lookup_of, pool_of
from agents_mindmap.api.errors import ApiError
from agents_mindmap.api.schemas import (
    DirectionView,
    EdgeSpec,
    EdgeView,
    GrowthView,
    LinkIn,
    LinkView,
    NodeIn,
    NodePatch,
    NodeView,
    VersionMetaView,
    VersionView,
)
from agents_mindmap.db import directions as directions_db
from agents_mindmap.db import edges as edges_db
from agents_mindmap.db import growth as growth_db
from agents_mindmap.db import links as links_db
from agents_mindmap.db import nodes as nodes_db
from agents_mindmap.db import versions as versions_db
from agents_mindmap.db.nodes import NodeRow
from agents_mindmap.db.scope import project_of


async def node_view(
    conn: psycopg.AsyncConnection, row: NodeRow, readable: Sequence[str]
) -> NodeView:
    """The node with its growth areas, edges (both ends readable), links and open directions."""
    live = await directions_db.list_directions(
        conn, readable=[row.project], node_id=row.id, statuses=("open", "acknowledged")
    )
    seeds = await directions_db.seeds_of(conn, [d.id for d in live]) if live else {}
    return NodeView.of(row).model_copy(
        update={
            "growth_areas": [GrowthView.of(g) for g in await growth_db.list_for_node(conn, row.id)],
            "edges": [
                EdgeView.of(e)
                for e in await edges_db.list_for_node(conn, row.id, readable=readable)
            ],
            "links": [LinkView.of(k) for k in await links_db.list_for_node(conn, row.id)],
            "directions": [DirectionView.of(d, seeds.get(d.id)) for d in live],
        }
    )


async def publish_node(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    readable: Sequence[str],
    *,
    map_key: str,
    parent: str | None,
    topic: str,
    notes: str,
    summary: str,
    status: str,
    client_key: str | None,
    growth_areas: Sequence[str],
    links: Sequence[LinkIn],
    edges: Sequence[EdgeSpec],
) -> NodeRow:
    """Create a node with its growth areas, links and edges in the caller's transaction. A
    repeated `client_key` returns the node made the first time and adds nothing."""
    require_writable(actor, await project_of(conn, "map", map_key))
    if client_key is not None:
        existing = await nodes_db.find_by_client_key(conn, actor.author, client_key)
        if existing is not None:
            return existing
    row = await nodes_db.create_node(
        conn,
        map_key=map_key,
        parent_key=parent,
        topic=topic,
        notes=notes,
        summary=summary,
        status=status,
        actor=actor.author,
        actor_session=actor.session,
        client_key=client_key,
    )
    await growth_db.add_growth_areas(conn, row.id, growth_areas, actor)
    for link in links:
        await links_db.create_link(
            conn,
            map_id=row.map_id,
            project=row.project,
            node_id=row.id,
            ref_key=link.ref_key,
            relation=link.relation,
            note=link.note,
            actor=actor,
            ignore_existing=True,
        )
    for edge in edges:
        await edges_db.create_edge(
            conn,
            src_key=row.key,
            dst_key=edge.dst,
            kind=edge.kind,
            note=edge.note,
            actor=actor,
            readable=readable,
        )
    fresh = await nodes_db.get_node(conn, row.key)
    assert fresh is not None
    return fresh


def build_router(*, agent: bool = False) -> APIRouter:
    router = APIRouter()

    @router.post("/maps/{key}/nodes", response_model=NodeView, status_code=201)
    async def create_node(request: Request, actor: ActorDep, key: str, body: NodeIn) -> NodeView:
        readable = await actor.readable(lookup_of(request))
        async with pool_of(request).connection() as conn:
            row = await publish_node(
                conn,
                actor,
                readable,
                map_key=key,
                parent=body.parent,
                topic=body.topic,
                notes=body.notes,
                summary=body.summary,
                status=body.status,
                client_key=body.client_key,
                growth_areas=body.growth_areas,
                links=body.links,
                edges=body.edges,
            )
            return await node_view(conn, row, readable)

    @router.get("/nodes/{key}", response_model=NodeView)
    async def get_node(request: Request, actor: ActorDep, key: str) -> NodeView:
        readable = await actor.readable(lookup_of(request))
        async with pool_of(request).connection() as conn:
            row = await nodes_db.get_node(conn, key)
            if row is None:
                raise ApiError(404, "not_found", "not found")
            await require_readable(actor, lookup_of(request), row.project)
            return await node_view(conn, row, readable)

    @router.patch("/nodes/{key}", response_model=NodeView)
    async def patch_node(request: Request, actor: ActorDep, key: str, body: NodePatch) -> NodeView:
        readable = await actor.readable(lookup_of(request))
        async with pool_of(request).connection() as conn:
            require_writable(actor, await project_of(conn, "node", key))
            row = await nodes_db.update_node(
                conn,
                key,
                version=body.version,
                topic=body.topic,
                notes=body.notes,
                summary=body.summary,
                status=body.status,
                parent_key=body.parent,
                actor=actor.author,
                actor_session=actor.session,
            )
            return await node_view(conn, row, readable)

    if not agent:

        @router.delete("/nodes/{key}", status_code=204)
        async def delete_node(request: Request, actor: ActorDep, key: str) -> Response:
            async with pool_of(request).connection() as conn:
                if not await nodes_db.delete_node(
                    conn, key, actor=actor.author, actor_session=actor.session
                ):
                    raise ApiError(404, "not_found", "not found")
            return Response(status_code=204)

    @router.get("/nodes/{key}/versions", response_model=list[VersionMetaView])
    async def list_versions(request: Request, actor: ActorDep, key: str) -> list[VersionMetaView]:
        async with pool_of(request).connection() as conn:
            await require_readable(actor, lookup_of(request), await project_of(conn, "node", key))
            rows = await versions_db.list_versions(conn, key)
        return [VersionMetaView.of(r) for r in rows]

    @router.get("/nodes/{key}/versions/{v}", response_model=VersionView)
    async def get_version(request: Request, actor: ActorDep, key: str, v: int) -> VersionView:
        async with pool_of(request).connection() as conn:
            await require_readable(actor, lookup_of(request), await project_of(conn, "node", key))
            row = await versions_db.get_version(conn, key, v)
        return VersionView.of(row)

    return router
