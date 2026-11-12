"""Link routes: a map or node linked to tasks (`T-n`) or specs (`S-n`)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from agents_mindmap.api.actors import ActorDep, require_writable
from agents_mindmap.api.common import lookup_of, pool_of
from agents_mindmap.api.errors import ApiError
from agents_mindmap.api.schemas import LinkIn, LinkView, RefKey
from agents_mindmap.db import links as links_db
from agents_mindmap.db.scope import project_of_link


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/links", response_model=list[LinkView])
    async def list_links(
        request: Request, actor: ActorDep, key: Annotated[RefKey, Query()]
    ) -> list[LinkView]:
        readable = await actor.readable(lookup_of(request))
        async with pool_of(request).connection() as conn:
            rows = await links_db.list_by_ref(conn, key, readable=readable)
        return [LinkView.of(r) for r in rows]

    @router.post("/maps/{key}/links", response_model=LinkView, status_code=201)
    async def link_map(request: Request, actor: ActorDep, key: str, body: LinkIn) -> LinkView:
        async with pool_of(request).connection() as conn:
            map_id, project = await links_db.map_target(conn, key)
            require_writable(actor, project)
            row = await links_db.create_link(
                conn,
                map_id=map_id,
                project=project,
                node_id=None,
                ref_key=body.ref_key,
                relation=body.relation,
                note=body.note,
                actor=actor,
            )
        assert row is not None
        return LinkView.of(row)

    @router.post("/nodes/{key}/links", response_model=LinkView, status_code=201)
    async def link_node(request: Request, actor: ActorDep, key: str, body: LinkIn) -> LinkView:
        async with pool_of(request).connection() as conn:
            map_id, project, node_id = await links_db.node_target(conn, key)
            require_writable(actor, project)
            row = await links_db.create_link(
                conn,
                map_id=map_id,
                project=project,
                node_id=node_id,
                ref_key=body.ref_key,
                relation=body.relation,
                note=body.note,
                actor=actor,
            )
        assert row is not None
        return LinkView.of(row)

    @router.delete("/links/{link_id}", status_code=204)
    async def delete_link(request: Request, actor: ActorDep, link_id: int) -> Response:
        async with pool_of(request).connection() as conn:
            require_writable(actor, await project_of_link(conn, link_id))
            if not await links_db.delete_link(conn, link_id, actor=actor):
                raise ApiError(404, "not_found", "not found")
        return Response(status_code=204)

    return router
