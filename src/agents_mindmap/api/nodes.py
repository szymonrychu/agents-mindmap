"""Node and version routes (web and delegate listeners, acting as the user)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from agents_mindmap.api.common import lookup_of, pool_of, user_author
from agents_mindmap.api.errors import ApiError
from agents_mindmap.api.schemas import (
    NodeIn,
    NodePatch,
    NodeView,
    VersionMetaView,
    VersionView,
)
from agents_mindmap.db import nodes as nodes_db
from agents_mindmap.db import versions as versions_db


def build_router() -> APIRouter:
    router = APIRouter()

    @router.post("/maps/{key}/nodes", response_model=NodeView, status_code=201)
    async def create_node(request: Request, key: str, body: NodeIn) -> NodeView:
        async with pool_of(request).connection() as conn:
            row = await nodes_db.create_node(
                conn,
                map_key=key,
                parent_key=body.parent,
                topic=body.topic,
                notes=body.notes,
                summary=body.summary,
                status=body.status,
                actor=user_author(request),
                client_key=body.client_key,
            )
        return NodeView.of(row)

    @router.get("/nodes/{key}", response_model=NodeView)
    async def get_node(request: Request, key: str) -> NodeView:
        readable = await lookup_of(request).active_slugs()
        async with pool_of(request).connection() as conn:
            row = await nodes_db.get_node(conn, key)
        if row is None or row.project not in readable:
            raise ApiError(404, "not_found", "not found")
        return NodeView.of(row)

    @router.patch("/nodes/{key}", response_model=NodeView)
    async def patch_node(request: Request, key: str, body: NodePatch) -> NodeView:
        async with pool_of(request).connection() as conn:
            row = await nodes_db.update_node(
                conn,
                key,
                version=body.version,
                topic=body.topic,
                notes=body.notes,
                summary=body.summary,
                status=body.status,
                parent_key=body.parent,
                actor=user_author(request),
            )
        return NodeView.of(row)

    @router.delete("/nodes/{key}", status_code=204)
    async def delete_node(request: Request, key: str) -> Response:
        async with pool_of(request).connection() as conn:
            if not await nodes_db.delete_node(conn, key, actor=user_author(request)):
                raise ApiError(404, "not_found", "not found")
        return Response(status_code=204)

    @router.get("/nodes/{key}/versions", response_model=list[VersionMetaView])
    async def list_versions(request: Request, key: str) -> list[VersionMetaView]:
        readable = await lookup_of(request).active_slugs()
        async with pool_of(request).connection() as conn:
            rows = await versions_db.list_versions(conn, key, readable=readable)
        return [VersionMetaView.of(r) for r in rows]

    @router.get("/nodes/{key}/versions/{v}", response_model=VersionView)
    async def get_version(request: Request, key: str, v: int) -> VersionView:
        readable = await lookup_of(request).active_slugs()
        async with pool_of(request).connection() as conn:
            row = await versions_db.get_version(conn, key, v, readable=readable)
        return VersionView.of(row)

    return router
