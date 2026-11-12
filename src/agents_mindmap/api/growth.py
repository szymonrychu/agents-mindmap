"""Growth area routes (agents archive with status `dropped`; only users hard delete)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from agents_mindmap.api.actors import ActorDep, require_writable
from agents_mindmap.api.common import pool_of
from agents_mindmap.api.errors import ApiError
from agents_mindmap.api.schemas import GrowthIn, GrowthPatch, GrowthView
from agents_mindmap.db import growth as growth_db
from agents_mindmap.db.keys import parse_key
from agents_mindmap.db.scope import project_of


def build_router(*, agent: bool = False) -> APIRouter:
    router = APIRouter()

    @router.post("/nodes/{key}/growth-areas", response_model=GrowthView, status_code=201)
    async def add_growth_area(
        request: Request, actor: ActorDep, key: str, body: GrowthIn
    ) -> GrowthView:
        async with pool_of(request).connection() as conn:
            require_writable(actor, await project_of(conn, "node", key))
            (row,) = await growth_db.add_growth_areas(conn, parse_key("N", key), [body.text], actor)
        return GrowthView.of(row)

    @router.patch("/growth-areas/{key}", response_model=GrowthView)
    async def patch_growth_area(
        request: Request, actor: ActorDep, key: str, body: GrowthPatch
    ) -> GrowthView:
        changes = body.model_dump(exclude={"version", "grown_node"}, exclude_unset=True)
        if "grown_node" in body.model_fields_set:
            changes["grown_node_id"] = (
                None if body.grown_node is None else parse_key("N", body.grown_node)
            )
        for name in ("status", "text", "position"):
            if name in changes and changes[name] is None:
                del changes[name]
        async with pool_of(request).connection() as conn:
            require_writable(actor, await project_of(conn, "growth", key))
            row = await growth_db.update_growth_area(
                conn, key, version=body.version, changes=changes, actor=actor
            )
        return GrowthView.of(row)

    if not agent:

        @router.delete("/growth-areas/{key}", status_code=204)
        async def delete_growth_area(request: Request, actor: ActorDep, key: str) -> Response:
            async with pool_of(request).connection() as conn:
                if not await growth_db.delete_growth_area(conn, key, actor=actor):
                    raise ApiError(404, "not_found", "not found")
            return Response(status_code=204)

    return router
