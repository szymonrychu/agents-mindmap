"""Direction routes: steering text for a map or node, handled by acknowledge, resolve or
withdraw."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from agents_mindmap.api.actors import ActorDep
from agents_mindmap.api.common import lookup_of, pool_of
from agents_mindmap.api.schemas import (
    DirectionIn,
    DirectionPatch,
    DirectionStatus,
    DirectionView,
    MapKey,
    Project,
)
from agents_mindmap.db import directions as directions_db


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/directions", response_model=list[DirectionView])
    async def list_directions(
        request: Request,
        actor: ActorDep,
        project: Annotated[Project | None, Query()] = None,
        map: Annotated[MapKey | None, Query()] = None,  # noqa: A002 - the documented query name
        status: DirectionStatus | None = None,
    ) -> list[DirectionView]:
        readable = await actor.readable(lookup_of(request))
        async with pool_of(request).connection() as conn:
            rows = await directions_db.list_directions(
                conn,
                readable=readable,
                project=project,
                map_key=map,
                statuses=None if status is None else [status],
            )
            seeds = await directions_db.seeds_of(conn, [r.id for r in rows]) if rows else {}
        return [DirectionView.of(r, seeds.get(r.id)) for r in rows]

    @router.post("/maps/{key}/directions", response_model=DirectionView, status_code=201)
    async def create_direction(
        request: Request, actor: ActorDep, key: str, body: DirectionIn
    ) -> DirectionView:
        async with pool_of(request).connection() as conn:
            row = await directions_db.create_direction(
                conn,
                map_key=key,
                node_key=body.node,
                text=body.text,
                seeds=body.growth_areas,
                task_key=body.task,
                actor=actor,
            )
            seeds = await directions_db.seeds_of(conn, [row.id])
        return DirectionView.of(row, seeds.get(row.id))

    @router.patch("/directions/{key}", response_model=DirectionView)
    async def patch_direction(
        request: Request, actor: ActorDep, key: str, body: DirectionPatch
    ) -> DirectionView:
        async with pool_of(request).connection() as conn:
            row = await directions_db.transition(
                conn,
                key,
                action=body.action,
                version=body.version,
                resolution=body.resolution,
                actor=actor,
            )
            seeds = await directions_db.seeds_of(conn, [row.id])
        return DirectionView.of(row, seeds.get(row.id))

    return router
