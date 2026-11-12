"""Lexical search over nodes and growth areas (spec 3.3).

Two ranked lists feed a reciprocal rank fusion (k = 60): full text (`websearch_to_tsquery`
on the `simple` config, which never raises on user input; nodes and growth areas, a growth
area hit is reported as its node) and fuzzy topic match (pg_trgm). Snippets are `ts_headline`
output with plain `[[` `]]` markers, or the topic for a fuzzy-only hit; they are text, never
markup. Semantic search joins in a later task; `Hit.signals` already names the signal."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import psycopg

from agents_mindmap.db import guard
from agents_mindmap.db.keys import fmt_key, parse_key

MAX_LIMIT = 50
CANDIDATES = 50
RRF_K = 60
HEADLINE = "StartSel=[[, StopSel=]], MaxWords=30, MinWords=10, MaxFragments=1, HighlightAll=false"

Signal = Literal["fts", "trgm", "semantic"]

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


async def lexical_search(
    conn: psycopg.AsyncConnection,
    q: str,
    *,
    projects: Sequence[str] | None,
    map_key: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[Hit]:
    """Hits for `q` in `projects` (None: unscoped, the user), best first, at most `limit`
    (capped at MAX_LIMIT)."""
    params = {
        "q": q,
        "projects": _as_list(projects),
        "map": None if map_key is None else parse_key("M", map_key),
        "status": status,
    }
    async with guard.read_guard(conn):
        fts: dict[int, tuple[float, tuple]] = {}
        for sql in (NODE_FTS_SQL, GROWTH_FTS_SQL):
            cur = await conn.execute(sql, params)
            for row in await cur.fetchall():
                if row[0] not in fts or row[4] > fts[row[0]][0]:
                    fts[row[0]] = (row[4], row)
        cur = await conn.execute(TRGM_SQL, params)
        trgm = await cur.fetchall()
    ranked_fts = sorted(fts.values(), key=lambda t: (-t[0], t[1][0]))
    scores: dict[int, float] = {}
    signals: dict[int, list[Signal]] = {}
    rows: dict[int, tuple] = {}
    for rank, (_, row) in enumerate(ranked_fts):
        scores[row[0]] = scores.get(row[0], 0.0) + 1 / (RRF_K + rank + 1)
        signals.setdefault(row[0], []).append("fts")
        rows[row[0]] = row
    for rank, row in enumerate(trgm):
        scores[row[0]] = scores.get(row[0], 0.0) + 1 / (RRF_K + rank + 1)
        signals.setdefault(row[0], []).append("trgm")
        rows.setdefault(row[0], (*row, row[3]))
    order = sorted(scores, key=lambda i: (-scores[i], i))[: max(1, min(limit, MAX_LIMIT))]
    return [
        Hit(
            node_key=fmt_key("N", i),
            map_key=fmt_key("M", rows[i][1]),
            project=rows[i][2],
            topic=rows[i][3],
            snippet=rows[i][5],
            signals=signals[i],
            score=round(scores[i], 6),
        )
        for i in order
    ]
