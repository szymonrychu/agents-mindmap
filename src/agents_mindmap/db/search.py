"""Search over nodes and growth areas (spec 3.3): lexical, semantic and hybrid.

Up to three ranked lists feed a reciprocal rank fusion (k = 60): full text
(`websearch_to_tsquery` on the `simple` config, which never raises on user input; nodes and
growth areas, a growth area hit is reported as its node), fuzzy topic match (pg_trgm) and chunk
similarity (pgvector cosine over `node_chunks`, the top CANDIDATES chunks, best chunk per node).
Snippets are `ts_headline` output with plain `[[` `]]` markers, else the best chunk's text, else
the topic; they are text, never markup. The query vector is computed by the caller, outside any
transaction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import psycopg

from agents_mindmap.db import guard
from agents_mindmap.db.chunks import vector_literal
from agents_mindmap.db.keys import fmt_key, parse_key

MAX_LIMIT = 50
CANDIDATES = 50
RRF_K = 60
HEADLINE = "StartSel=[[, StopSel=]], MaxWords=30, MinWords=10, MaxFragments=1, HighlightAll=false"

EF_SEARCH = 80
SNIPPET_CHARS = 300

Signal = Literal["fts", "trgm", "semantic"]
Mode = Literal["hybrid", "lexical", "semantic"]

_FILTER = """
  AND (%(projects)s::text[] IS NULL OR {alias}.project = ANY(%(projects)s))
  AND (%(map)s::bigint IS NULL OR {alias}.map_id = %(map)s)
  AND (%(status)s::text IS NULL OR {alias}.status = %(status)s)
"""

NODE_FTS_SQL = f"""
SELECT id, map_id, project, topic, rank,
       ts_headline('simple', topic || E'\\n' || notes, tq, '{HEADLINE}')
FROM (
    SELECT n.id, n.map_id, n.project, n.topic, n.notes, q.tq, ts_rank_cd(n.tsv, q.tq) AS rank
    FROM nodes n, (SELECT websearch_to_tsquery('simple', %(q)s) AS tq) q
    WHERE n.tsv @@ q.tq {_FILTER.format(alias="n")}
    ORDER BY rank DESC, n.id LIMIT {CANDIDATES}
) s
"""

GROWTH_FTS_SQL = f"""
SELECT id, map_id, project, topic, rank, ts_headline('simple', text, tq, '{HEADLINE}')
FROM (
    SELECT n.id, n.map_id, n.project, n.topic, g.text, q.tq, ts_rank_cd(g.tsv, q.tq) AS rank
    FROM growth_areas g JOIN nodes n ON n.id = g.node_id,
         (SELECT websearch_to_tsquery('simple', %(q)s) AS tq) q
    WHERE g.tsv @@ q.tq {_FILTER.format(alias="n")}
    ORDER BY rank DESC, g.id LIMIT {CANDIDATES}
) s
"""

TRGM_SQL = f"""
SELECT n.id, n.map_id, n.project, n.topic, similarity(n.topic, %(q)s) AS sim
FROM nodes n WHERE n.topic %% %(q)s {_FILTER.format(alias="n")}
ORDER BY sim DESC, n.id LIMIT {CANDIDATES}
"""

# The inner ORDER BY is the distance alone, so the HNSW index serves it (ef_search 80); the
# filters apply to the candidates it walks. The outer sort breaks distance ties on the keys.
SEMANTIC_SQL = (
    """
SELECT id, map_id, project, topic, text FROM (
    SELECT n.id, n.map_id, n.project, n.topic, c.text, c.ord,
           c.embedding <=> %(v)s::vector AS dist
    FROM node_chunks c JOIN nodes n ON n.id = c.node_id
    WHERE (%(projects)s::text[] IS NULL OR c.project = ANY(%(projects)s))
      AND (%(map)s::bigint IS NULL OR n.map_id = %(map)s)
      AND (%(status)s::text IS NULL OR n.status = %(status)s)
    ORDER BY c.embedding <=> %(v)s::vector
    LIMIT """
    + str(CANDIDATES)
    + """
) s ORDER BY dist, id, ord
"""
)

PENDING_SQL = (
    "SELECT count(*) FROM nodes WHERE (%(projects)s::text[] IS NULL OR project = ANY(%(projects)s))"
    " AND embedded_rev < content_rev"
)


def _as_list(projects: Sequence[str] | None) -> list[str] | None:
    return None if projects is None else list(projects)


@dataclass(frozen=True)
class Hit:
    node_key: str
    map_key: str
    project: str
    topic: str
    snippet: str
    signals: list[Signal]
    score: float


async def pending_embedding(conn: psycopg.AsyncConnection, projects: Sequence[str] | None) -> int:
    """Readable nodes whose embedding lags their content (`projects` None: unscoped)."""
    async with guard.read_guard(conn):
        cur = await conn.execute(PENDING_SQL, {"projects": _as_list(projects)})
        return (await cur.fetchone())[0]


def _params(q: str, projects, map_key, status) -> dict:
    return {
        "q": q,
        "projects": _as_list(projects),
        "map": None if map_key is None else parse_key("M", map_key),
        "status": status,
    }


# One ranked list: (node id, map id, project, topic, snippet or None), best first, one per node.
Ranked = list[tuple[int, int, str, str, str | None]]


async def _lexical_lists(conn: psycopg.AsyncConnection, params: dict) -> tuple[Ranked, Ranked]:
    fts: dict[int, tuple[float, tuple]] = {}
    for sql in (NODE_FTS_SQL, GROWTH_FTS_SQL):
        cur = await conn.execute(sql, params)
        for row in await cur.fetchall():
            if row[0] not in fts or row[4] > fts[row[0]][0]:
                fts[row[0]] = (row[4], row)
    ranked_fts = [
        (r[0], r[1], r[2], r[3], r[5])
        for _, r in sorted(fts.values(), key=lambda t: (-t[0], t[1][0]))
    ]
    cur = await conn.execute(TRGM_SQL, params)
    trgm = [(r[0], r[1], r[2], r[3], None) for r in await cur.fetchall()]
    return ranked_fts, trgm


async def _semantic_list(
    conn: psycopg.AsyncConnection, params: dict, vector: Sequence[float]
) -> Ranked:
    await conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(EF_SEARCH),))
    cur = await conn.execute(SEMANTIC_SQL, {**params, "v": vector_literal(vector)})
    best: Ranked = []
    seen: set[int] = set()
    for node_id, map_id, project, topic, text in await cur.fetchall():
        if node_id not in seen:
            seen.add(node_id)
            best.append((node_id, map_id, project, topic, text[:SNIPPET_CHARS]))
    return best


def _fuse(lists: list[tuple[Signal, Ranked]], limit: int) -> list[Hit]:
    """Reciprocal rank fusion; the snippet is the first list's (fts headline before chunk text),
    the topic when no list carries one. Ties break on the node id: deterministic."""
    scores: dict[int, float] = {}
    signals: dict[int, list[Signal]] = {}
    rows: dict[int, tuple] = {}
    snippets: dict[int, str] = {}
    for signal, ranked in lists:
        for rank, row in enumerate(ranked):
            node_id = row[0]
            scores[node_id] = scores.get(node_id, 0.0) + 1 / (RRF_K + rank + 1)
            signals.setdefault(node_id, []).append(signal)
            rows.setdefault(node_id, row)
            if row[4] is not None:
                snippets.setdefault(node_id, row[4])
    order = sorted(scores, key=lambda i: (-scores[i], i))[: max(1, min(limit, MAX_LIMIT))]
    return [
        Hit(
            node_key=fmt_key("N", i),
            map_key=fmt_key("M", rows[i][1]),
            project=rows[i][2],
            topic=rows[i][3],
            snippet=snippets.get(i, rows[i][3]),
            signals=signals[i],
            score=round(scores[i], 6),
        )
        for i in order
    ]


async def lexical_search(
    conn: psycopg.AsyncConnection,
    q: str,
    *,
    projects: Sequence[str] | None,
    map_key: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[Hit]:
    """Full text and fuzzy hits for `q` in `projects` (None: unscoped, the user), best first,
    at most `limit` (capped at MAX_LIMIT)."""
    return await hybrid_search(
        conn, q, None, projects=projects, map_key=map_key, status=status, limit=limit
    )


async def semantic_search(
    conn: psycopg.AsyncConnection,
    vector: Sequence[float],
    *,
    projects: Sequence[str] | None,
    map_key: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[Hit]:
    """Nodes whose chunks are nearest to `vector` (cosine), best chunk per node."""
    async with guard.read_guard(conn):
        ranked = await _semantic_list(conn, _params("", projects, map_key, status), vector)
    return _fuse([("semantic", ranked)], limit)


async def hybrid_search(
    conn: psycopg.AsyncConnection,
    q: str,
    vector: Sequence[float] | None,
    *,
    projects: Sequence[str] | None,
    map_key: str | None = None,
    status: str | None = None,
    limit: int = 20,
    mode: Mode = "hybrid",
) -> list[Hit]:
    """`mode` hybrid fuses full text, fuzzy and (with `vector`) chunk similarity; lexical
    uses the first two; semantic only the chunks, and falls back to lexical without a vector
    (the embedder is down: search never fails because of it)."""
    params = _params(q, projects, map_key, status)
    lists: list[tuple[Signal, Ranked]] = []
    async with guard.read_guard(conn):
        if mode != "semantic" or vector is None:
            fts, trgm = await _lexical_lists(conn, params)
            lists += [("fts", fts), ("trgm", trgm)]
        if mode != "lexical" and vector is not None:
            lists.append(("semantic", await _semantic_list(conn, params, vector)))
    return _fuse(lists, limit)
