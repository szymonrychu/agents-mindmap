"""Edge routes: create from a writable node to any readable node; delete from either end."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from agents_mindmap.api.actors import ActorDep
from agents_mindmap.api.common import lookup_of, pool_of
from agents_mindmap.api.errors import ApiError
from agents_mindmap.api.schemas import EdgeIn, EdgeView
from agents_mindmap.db import edges as edges_db


def build_router() -> APIRouter:
    router = APIRouter()

    @router.post("/edges", response_model=EdgeView, status_code=201)
    async def create_edge(request: Request, actor: ActorDep, body: EdgeIn) -> EdgeView:
        readable = await actor.readable(lookup_of(request))
        async with pool_of(request).connection() as conn:
            row = await edges_db.create_edge(
                conn,
                src_key=body.src,
                dst_key=body.dst,
                kind=body.kind,
                note=body.note,
                actor=actor,
                readable=readable,
            )
        return EdgeView.of(row)

    @router.delete("/edges/{key}", status_code=204)
    async def delete_edge(request: Request, actor: ActorDep, key: str) -> Response:
        async with pool_of(request).connection() as conn:
            if not await edges_db.delete_edge(conn, key, actor=actor):
                raise ApiError(404, "not_found", "not found")
        return Response(status_code=204)

    return router
