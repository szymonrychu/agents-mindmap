"""node_chunks: the embedder's claim, its guarded store and the re-embed reset.

The embedder claims pending nodes (`embedded_rev < content_rev`) with FOR UPDATE SKIP LOCKED in
a short transaction that ends before any model call; the store runs in a new transaction and is
guarded on the node's `content_rev`, so a result built from text that changed meanwhile is
dropped and the node stays pending. Chunks of a node are replaced together with `embedded_rev`
and a silent `node_embedded` event."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import psycopg

from agents_mindmap.db.events import append_event
from agents_mindmap.db.keys import fmt_key

EMBEDDER_ACTOR = "system:embedder"

CLAIM_SQL = """
SELECT id, project, map_id, content_rev, topic, notes FROM nodes
WHERE embedded_rev < content_rev
ORDER BY id LIMIT %s
FOR UPDATE SKIP LOCKED
"""

# Every growth area counts, whatever its status: a status change does not bump content_rev, so
# filtering on it would leave chunk 0 out of step with the node until its next text change.
GROWTH_SQL = """
SELECT node_id, text FROM growth_areas WHERE node_id = ANY(%s) ORDER BY node_id, position, id
"""

# Status-guarded: only while the content is still the one the vectors were built from.
MARK_SQL = """
UPDATE nodes SET embedded_rev = content_rev
WHERE id = %s AND content_rev = %s AND embedded_rev < content_rev
RETURNING project, map_id
"""

INSERT_SQL = """
INSERT INTO node_chunks (node_id, ord, project, content_rev, text, embedding)
VALUES (%s, %s, %s, %s, %s, %s::vector)
"""


@dataclass(frozen=True)
class PendingNode:
    id: int
    project: str
    map_id: int
    content_rev: int
    topic: str
    notes: str
    growth: list[str]


def vector_literal(vector: Sequence[float]) -> str:
    """The pgvector text form `[x,y,...]` (no adapter registration on pool connections)."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


async def claim_pending(conn: psycopg.AsyncConnection, limit: int) -> list[PendingNode]:
    """Read up to `limit` pending nodes and their growth texts, oldest id first. The rows are
    locked only for the length of this short transaction, which commits before returning."""
    async with conn.transaction():
        cur = await conn.execute(CLAIM_SQL, (limit,))
        rows = await cur.fetchall()
        if not rows:
            return []
        cur = await conn.execute(GROWTH_SQL, ([r[0] for r in rows],))
        growth: dict[int, list[str]] = {}
        for node_id, text in await cur.fetchall():
            growth.setdefault(node_id, []).append(text)
    return [PendingNode(*row, growth=growth.get(row[0], [])) for row in rows]


async def store_chunks(
    conn: psycopg.AsyncConnection,
    node: PendingNode,
    texts: Sequence[str],
    vectors: Sequence[Sequence[float]],
) -> bool:
    """Replace the node's chunks if its content is still `node.content_rev`; False when it
    moved on (or the node is gone): nothing is written and the node stays pending."""
    async with conn.transaction():
        cur = await conn.execute(MARK_SQL, (node.id, node.content_rev))
        row = await cur.fetchone()
        if row is None:
            return False
        project, map_id = row
        await conn.execute("DELETE FROM node_chunks WHERE node_id = %s", (node.id,))
        async with conn.cursor() as c:
            await c.executemany(
                INSERT_SQL,
                [
                    (node.id, i, project, node.content_rev, text, vector_literal(vector))
                    for i, (text, vector) in enumerate(zip(texts, vectors, strict=True))
                ],
            )
        await append_event(
            conn,
            project=project,
            map_id=map_id,
            node_id=node.id,
            kind="node_embedded",
            actor=EMBEDDER_ACTOR,
            actor_session=None,
            detail={"node": fmt_key("N", node.id), "content_rev": node.content_rev},
        )
    return True


async def pending_count(conn: psycopg.AsyncConnection) -> int:
    """Nodes whose chunks lag their content, across every project."""
    cur = await conn.execute("SELECT count(*) FROM nodes WHERE embedded_rev < content_rev")
    return (await cur.fetchone())[0]


async def reset_all(conn: psycopg.AsyncConnection) -> int:
    """`mindmap reembed`: mark every node pending (`embedded_rev = 0`); the stored chunks keep
    serving search until the embedder replaces them. Returns the number of nodes."""
    async with conn.transaction():
        cur = await conn.execute("UPDATE nodes SET embedded_rev = 0")
        return cur.rowcount
