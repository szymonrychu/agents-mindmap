"""Graph traversal from a node and the whole-map graph (spec 3.4).

The read scope filter sits inside the recursive term on both edge directions, so a path can
never cross an unreadable project. A walk is followed by exactly two queries (the node rows,
the edges among the result nodes); there are no per-node queries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg.rows import class_row

from agents_mindmap.db import guard
from agents_mindmap.db.edges import EDGE_COLUMNS, EdgeRow
from agents_mindmap.db.keys import fmt_key

DEFAULT_DEPTH = 2
MAX_DEPTH = 4
MAX_NODES = 200
DEFAULT_GRAPH_DEPTH = 10
MAX_GRAPH_DEPTH = 20
MAX_GRAPH_NODES = 500

Follow = Literal["out", "in", "both"]


@dataclass(frozen=True)
class NodeStub:
    key: str
    map_key: str
    project: str
    topic: str
    parent_key: str | None
    status: str
    open_growth_areas: int
    embedding_pending: bool
    depth: int
    stub: bool


@dataclass(frozen=True)
class Subgraph:
    nodes: list[NodeStub]
    edges: list[EdgeRow]
    truncated: bool


WALK_SQL = """
WITH RECURSIVE walk(node_id, depth) AS (
    SELECT %(start)s::bigint, 0
  UNION ALL
    SELECT nb.node_id, w.depth + 1
    FROM walk w
    JOIN LATERAL (
        SELECT e.dst_node_id AS node_id FROM edges e
         WHERE e.src_node_id = w.node_id AND e.kind = ANY(%(kinds)s)
           AND e.dst_project = ANY(%(readable)s) AND %(out)s
        UNION ALL
        SELECT e.src_node_id FROM edges e
         WHERE e.dst_node_id = w.node_id AND e.kind = ANY(%(kinds)s)
           AND e.src_project = ANY(%(readable)s) AND %(inc)s
        UNION ALL
        SELECT n.id FROM nodes n
         WHERE n.parent_id = w.node_id AND n.project = ANY(%(readable)s) AND %(tree)s
    ) nb ON true
    WHERE w.depth < %(depth)s
) CYCLE node_id SET is_cycle USING path
SELECT node_id, min(depth) AS depth FROM walk WHERE NOT is_cycle
GROUP BY node_id ORDER BY min(depth), node_id LIMIT %(limit)s
"""

NODES_SQL = """
SELECT n.id, n.map_id, n.project, n.topic, n.parent_id, n.status,
       (SELECT count(*) FROM growth_areas g WHERE g.node_id = n.id AND g.status = 'open'),
       n.embedded_rev < n.content_rev
FROM nodes n WHERE n.id = ANY(%s)
"""

EDGES_SQL = (
    f"SELECT {EDGE_COLUMNS} FROM edges WHERE src_node_id = ANY(%s) AND dst_node_id = ANY(%s)"
    " AND kind = ANY(%s) ORDER BY id"
)


async def _stubs(
    conn: psycopg.AsyncConnection,
    depths: dict[int, int],
    *,
    home_map: int | None = None,
    home_node: int | None = None,
    far_ends: frozenset[int] = frozenset(),
) -> list[NodeStub]:
    """The node rows with their depth. `stub` is true for nodes of another map than the home
    map (the map of `home_node` when `home_map` is not given) and for the `far_ends`."""
    cur = await conn.execute(NODES_SQL, (list(depths),))
    rows = await cur.fetchall()
    if home_map is None:
        home_map = next(r[1] for r in rows if r[0] == home_node)
    stubs = [
        NodeStub(
            key=fmt_key("N", node_id),
            map_key=fmt_key("M", map_id),
            project=project,
            topic=topic,
            parent_key=None if parent_id is None else fmt_key("N", parent_id),
            status=status,
            open_growth_areas=open_growth,
            embedding_pending=pending,
            depth=depths[node_id],
            stub=map_id != home_map or node_id in far_ends,
        )
        for node_id, map_id, project, topic, parent_id, status, open_growth, pending in rows
    ]
    return sorted(stubs, key=lambda s: (s.depth, int(s.key[2:])))


async def traverse(
    conn: psycopg.AsyncConnection,
    start_id: int,
    *,
    depth: int,
    kinds: list[str],
    follow: Follow,
    tree: bool,
    readable: Sequence[str],
) -> Subgraph:
    """Nodes reachable from `start_id` within `depth` (capped at MAX_DEPTH) hops over edges of
    `kinds` in the `follow` direction and, with `tree`, down the parent tree; at most MAX_NODES
    nodes (nearest first, `truncated` set when more exist). Only readable projects are
    entered. `stub` marks nodes outside the start node's map."""
    params = {
        "start": start_id,
        "kinds": kinds,
        "readable": list(readable),
        "out": follow in ("out", "both"),
        "inc": follow in ("in", "both"),
        "tree": tree,
        "depth": max(0, min(depth, MAX_DEPTH)),
        "limit": MAX_NODES + 1,
    }
    async with guard.read_guard(conn):
        cur = await conn.execute(WALK_SQL, params)
        found = await cur.fetchall()
        truncated = len(found) > MAX_NODES
        depths = {node_id: d for node_id, d in found[:MAX_NODES]}
        nodes = await _stubs(conn, depths, home_node=start_id)
        async with conn.cursor(row_factory=class_row(EdgeRow)) as edge_cur:
            await edge_cur.execute(EDGES_SQL, (list(depths), list(depths), kinds))
            edges = await edge_cur.fetchall()
    return Subgraph(nodes=nodes, edges=edges, truncated=truncated)


TREE_SQL = """
WITH RECURSIVE tree(node_id, depth) AS (
    SELECT n.id, 0 FROM nodes n WHERE n.map_id = %(map)s AND n.parent_id IS NULL
  UNION ALL
    SELECT c.id, t.depth + 1 FROM tree t JOIN nodes c ON c.parent_id = t.node_id
     WHERE t.depth < %(depth)s AND c.map_id = %(map)s
)
SELECT node_id, depth FROM tree ORDER BY depth, node_id LIMIT %(limit)s
"""

LEAVING_SQL = (
    f"SELECT {EDGE_COLUMNS} FROM edges WHERE (src_node_id = ANY(%(ids)s) OR dst_node_id ="
    " ANY(%(ids)s)) AND src_project = ANY(%(readable)s) AND dst_project = ANY(%(readable)s)"
    " ORDER BY id"
)


async def map_graph(
    conn: psycopg.AsyncConnection,
    map_id: int,
    *,
    depth: int,
    readable: Sequence[str],
) -> Subgraph:
    """The map's parent tree down to `depth` (capped at MAX_GRAPH_DEPTH, at most
    MAX_GRAPH_NODES nodes) plus every readable edge touching it. Far ends of edges that leave
    the tree are stubs (own key, topic, map and project), one level below their near end."""
    async with guard.read_guard(conn):
        cur = await conn.execute(
            TREE_SQL,
            {
                "map": map_id,
                "depth": max(0, min(depth, MAX_GRAPH_DEPTH)),
                "limit": MAX_GRAPH_NODES + 1,
            },
        )
        found = await cur.fetchall()
        truncated = len(found) > MAX_GRAPH_NODES
        depths = {node_id: d for node_id, d in found[:MAX_GRAPH_NODES]}
        async with conn.cursor(row_factory=class_row(EdgeRow)) as edge_cur:
            await edge_cur.execute(LEAVING_SQL, {"ids": list(depths), "readable": list(readable)})
            edges = await edge_cur.fetchall()
        far: dict[int, int] = {}
        for e in edges:
            for near, other in ((e.src_node_id, e.dst_node_id), (e.dst_node_id, e.src_node_id)):
                if near in depths and other not in depths:
                    far[other] = min(far.get(other, depths[near] + 1), depths[near] + 1)
        nodes = await _stubs(conn, {**depths, **far}, home_map=map_id, far_ends=frozenset(far))
    return Subgraph(nodes=nodes, edges=edges, truncated=truncated)
