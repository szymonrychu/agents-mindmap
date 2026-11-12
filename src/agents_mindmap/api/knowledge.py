"""Knowledge capture: `POST /knowledge/notes` is `node_create` with defaults. The project's
`knowledge` map is created lazily and the note is filed under its root unless a parent is
given."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Request

from agents_mindmap.api.actors import Actor, ActorDep, require_writable
from agents_mindmap.api.common import lookup_of, pool_of
from agents_mindmap.api.errors import ApiError
from agents_mindmap.api.nodes import node_view, publish_node
from agents_mindmap.api.schemas import KnowledgeNoteIn, NodeView
from agents_mindmap.db import maps as maps_db
from agents_mindmap.db.keys import fmt_key, parse_key
from agents_mindmap.db.scope import project_of

KNOWLEDGE_SLUG = "knowledge"


async def ensure_knowledge_map(
    conn: psycopg.AsyncConnection, project: str, actor: Actor
) -> maps_db.MapRow:
    """The project's knowledge map, created on first use; concurrent first captures wait on
    an advisory lock and share the one map."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"knowledge:{project}",))
    cur = await conn.execute(
        "SELECT id FROM maps WHERE project = %s AND kind = 'knowledge'", (project,)
    )
    found = await cur.fetchone()
    if found is not None:
        row = await maps_db.get_map(conn, fmt_key("M", found[0]))
        assert row is not None
        return row
    return await maps_db.create_map(
        conn,
        project=project,
        slug=KNOWLEDGE_SLUG,
        title=f"{project} knowledge",
        description="",
        kind="knowledge",
        root_topic=f"{project} knowledge",
        actor=actor.author,
        actor_session=actor.session,
        client_key=None,
    )


def build_router() -> APIRouter:
    router = APIRouter()

    @router.post("/knowledge/notes", response_model=NodeView, status_code=201)
    async def put_note(request: Request, actor: ActorDep, body: KnowledgeNoteIn) -> NodeView:
        readable = await actor.readable(lookup_of(request))
        async with pool_of(request).connection() as conn:
            if body.parent is not None:
                parent_project = await project_of(conn, "node", body.parent)
                if body.project is not None and body.project != parent_project:
                    raise ApiError(422, "parent_invalid", "the parent is in another project")
                require_writable(actor, parent_project)
                cur = await conn.execute(
                    "SELECT map_id FROM nodes WHERE id = %s", (parse_key("N", body.parent),)
                )
                map_key = fmt_key("M", (await cur.fetchone())[0])  # type: ignore[index]
            else:
                project = body.project or actor.home_project
                if project is None:
                    raise ApiError(422, "project_required", "name the project of the note")
                require_writable(actor, project)
                await lookup_of(request).require_active(project)
                map_key = (await ensure_knowledge_map(conn, project, actor)).key
            row = await publish_node(
                conn,
                actor,
                readable,
                map_key=map_key,
                parent=body.parent,
                topic=body.topic,
                notes=body.notes,
                summary=body.summary,
                status="seed",
                client_key=body.client_key,
                growth_areas=[],
                links=body.links,
                edges=body.edges,
            )
            return await node_view(conn, row, readable)

    return router
