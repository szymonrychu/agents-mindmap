"""Map routes. The web and delegate listeners act as the user, the service listener as the
calling agent (same routes, no hard delete)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from agents_mindmap.api.actors import ActorDep, require_readable, require_writable
from agents_mindmap.api.common import lookup_of, pool_of
from agents_mindmap.api.errors import ApiError
from agents_mindmap.api.schemas import (
    MapIn,
    MapKind,
    MapPatch,
    MapStatus,
    MapView,
    Project,
    ProjectCount,
)
from agents_mindmap.db import maps as maps_db
from agents_mindmap.db.scope import project_of

PROJECT_COUNTS_SQL = """
SELECT m.project, count(DISTINCT m.id), count(n.id)
FROM maps m LEFT JOIN nodes n ON n.map_id = m.id
WHERE m.project = ANY(%s)
GROUP BY m.project ORDER BY m.project
"""


def build_router(*, agent: bool = False) -> APIRouter:
    router = APIRouter()

    @router.get("/projects", response_model=list[ProjectCount])
    async def projects(request: Request, actor: ActorDep) -> list[ProjectCount]:
        readable = await actor.readable(lookup_of(request))
        async with pool_of(request).connection() as conn:
            cur = await conn.execute(PROJECT_COUNTS_SQL, (readable,))
            return [ProjectCount(project=p, maps=m, nodes=n) for p, m, n in await cur.fetchall()]

    @router.get("/maps", response_model=list[MapView])
    async def list_maps(
        request: Request,
        actor: ActorDep,
        project: Annotated[Project | None, Query()] = None,
        status: MapStatus | None = None,
        kind: MapKind | None = None,
    ) -> list[MapView]:
        readable = await actor.readable(lookup_of(request))
        async with pool_of(request).connection() as conn:
            rows = await maps_db.list_maps(
                conn,
                readable=readable,
                projects=None if project is None else [project],
                status=status,
                kind=kind,
            )
        return [MapView.of(r) for r in rows]

    @router.post("/maps", response_model=MapView, status_code=201)
    async def create_map(request: Request, actor: ActorDep, body: MapIn) -> MapView:
        require_writable(actor, body.project)
        await lookup_of(request).require_active(body.project)
        async with pool_of(request).connection() as conn:
            row = await maps_db.create_map(
                conn,
                project=body.project,
                slug=body.slug,
                title=body.title,
                description=body.description,
                kind=body.kind,
                root_topic=body.root_topic or body.title,
                actor=actor.author,
                actor_session=actor.session,
                client_key=body.client_key,
            )
        return MapView.of(row)

    @router.get("/maps/{key}", response_model=MapView)
    async def get_map(request: Request, actor: ActorDep, key: str) -> MapView:
        async with pool_of(request).connection() as conn:
            row = await maps_db.get_map(conn, key)
        if row is None:
            raise ApiError(404, "not_found", "not found")
        await require_readable(actor, lookup_of(request), row.project)
        return MapView.of(row)

    @router.patch("/maps/{key}", response_model=MapView)
    async def patch_map(request: Request, actor: ActorDep, key: str, body: MapPatch) -> MapView:
        fields = body.model_dump(exclude={"version"}, exclude_none=True)
        async with pool_of(request).connection() as conn:
            require_writable(actor, await project_of(conn, "map", key))
            row = await maps_db.update_map(
                conn,
                key,
                version=body.version,
                fields=fields,
                actor=actor.author,
                actor_session=actor.session,
            )
        return MapView.of(row)

    if not agent:

        @router.delete("/maps/{key}", status_code=204)
        async def delete_map(request: Request, actor: ActorDep, key: str) -> Response:
            async with pool_of(request).connection() as conn:
                if not await maps_db.delete_map(
                    conn, key, actor=actor.author, actor_session=actor.session
                ):
                    raise ApiError(404, "not_found", "not found")
            return Response(status_code=204)

    return router
