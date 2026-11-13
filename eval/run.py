"""Golden-set evaluation of mindmap search: recall@10 and MRR per mode over eval/golden.yaml.

CI runs the harness with the tests' fake embedder (tests/test_golden_eval.py). On demand, run
it against a live embeddings server; it seeds a scratch pgvector Postgres (a throwaway
testcontainers one unless --database-url names an empty scratch database), embeds every note
through the real provider and checks hybrid search against the `live` floors:

    kubectl -n agents port-forward svc/agents-embeddings 8089:80 &
    uv run python eval/run.py --embedder-url http://127.0.0.1:8089

Prints one JSON object (per mode: recall_at_10, mrr) and exits 1 below the floors."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import psycopg
import yaml

from agents_mindmap import embedder as emb
from agents_mindmap.db.keys import parse_key
from agents_mindmap.db.search import hybrid_search
from agents_mindmap.embeddings import EmbeddingProvider, OpenAIShapedProvider
from agents_mindmap.settings import EMBED_MODEL

GOLDEN = Path(__file__).resolve().parent / "golden.yaml"
PROJECT = "eval"
AUTHOR = "user:eval"
TOP = 10
PG_IMAGE = "pgvector/pgvector:0.8.7-pg16"


@dataclass(frozen=True)
class Query:
    q: str
    lang: str
    relevant: frozenset[str]


@dataclass(frozen=True)
class Golden:
    nodes: list[dict]
    queries: list[Query]
    floors: dict[str, dict[str, float]]


@dataclass(frozen=True)
class Score:
    recall_at_10: float
    mrr: float


def load_golden(path: Path = GOLDEN) -> Golden:
    data = yaml.safe_load(path.read_text())
    queries = [Query(q["q"], q["lang"], frozenset(q["relevant"])) for q in data["queries"]]
    labels = {n["id"] for n in data["nodes"]}
    for query in queries:
        unknown = query.relevant - labels
        if unknown:
            raise ValueError(f"golden query {query.q!r} names unknown notes {sorted(unknown)}")
    return Golden(data["nodes"], queries, data["floors"])


def seed(db_url: str, golden: Golden) -> dict[str, int]:
    """One map in project `eval`, the first note its root and the others its children; returns
    {note label: node id}."""
    with psycopg.connect(db_url) as conn:
        map_id = conn.execute(
            "INSERT INTO maps (project, slug, kind, title, description, status, created_by)"
            " VALUES (%s, 'golden', 'research', 'golden', '', 'active', %s) RETURNING id",
            (PROJECT, AUTHOR),
        ).fetchone()[0]
        ids: dict[str, int] = {}
        root = None
        for node in golden.nodes:
            node_id = conn.execute(
                "INSERT INTO nodes (map_id, project, parent_id, topic, notes, status,"
                " created_by, updated_by) VALUES (%s, %s, %s, %s, %s, 'seed', %s, %s)"
                " RETURNING id",
                (map_id, PROJECT, root, node["topic"], node["notes"], AUTHOR, AUTHOR),
            ).fetchone()[0]
            root = root or node_id
            ids[node["id"]] = node_id
        conn.execute("UPDATE maps SET root_node_id = %s WHERE id = %s", (root, map_id))
        conn.commit()
    return ids


async def embed_all(pool, provider: EmbeddingProvider) -> int:
    total = 0
    while stored := await emb.embed_once(pool, provider):
        total += stored
    return total


def score(rankings: list[list[str]], relevant: list[set[str] | frozenset[str]]) -> Score:
    """Mean recall@10 and mean reciprocal rank of the first relevant hit."""
    recalls, ranks = [], []
    for ranked, rel in zip(rankings, relevant, strict=True):
        recalls.append(len(rel & set(ranked[:TOP])) / len(rel))
        ranks.append(next((1 / (i + 1) for i, label in enumerate(ranked) if label in rel), 0.0))
    return Score(round(sum(recalls) / len(recalls), 4), round(sum(ranks) / len(ranks), 4))


async def evaluate(
    pool, golden: Golden, ids: dict[str, int], provider: EmbeddingProvider | None, *, mode: str
) -> Score:
    labels = {node_id: label for label, node_id in ids.items()}
    rankings = []
    for query in golden.queries:
        vector = None
        if provider is not None and mode != "lexical":
            (vector,) = await provider.embed([query.q], query=True)
        async with pool.connection() as conn:
            hits = await hybrid_search(conn, query.q, vector, projects=None, limit=TOP, mode=mode)
        rankings.append([labels[parse_key("N", h.node_key)] for h in hits])
    return score(rankings, [q.relevant for q in golden.queries])


async def _live(db_url: str, embedder_url: str, model: str) -> dict:
    from agents_common.db import run_migrations

    from agents_mindmap.runtime import open_pool
    from agents_mindmap.settings import Settings

    run_migrations(db_url, "agents_mindmap")
    golden = load_golden()
    ids = seed(db_url, golden)
    settings = Settings(
        database_url=db_url,
        orchestrator_url="http://orchestrator.invalid",
        public_base_url="http://eval.invalid",
    )
    pool = await open_pool(settings, min_size=1, max_size=2)
    provider = OpenAIShapedProvider(embedder_url, model, timeout=300)
    try:
        await embed_all(pool, provider)
        result = {
            mode: (await evaluate(pool, golden, ids, provider, mode=mode)).__dict__
            for mode in ("lexical", "semantic", "hybrid")
        }
    finally:
        await provider.aclose()
        await pool.close()
    floors = golden.floors["live"]
    result["floors"] = floors
    result["pass"] = all(result["hybrid"][k] >= v for k, v in floors.items())
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--embedder-url", required=True, help="OpenAI-shaped embeddings base url")
    parser.add_argument("--model", default=EMBED_MODEL)
    parser.add_argument(
        "--database-url", help="an EMPTY scratch database with pgvector; default: a container"
    )
    args = parser.parse_args(argv)
    with ExitStack() as stack:
        db_url = args.database_url
        if db_url is None:
            from testcontainers.community.postgres import PostgresContainer

            pg = stack.enter_context(PostgresContainer(PG_IMAGE, driver=None))
            db_url = (
                f"postgresql://{pg.username}:{pg.password}"
                f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
            )
            with psycopg.connect(db_url, autocommit=True) as admin:
                admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        result = asyncio.run(_live(db_url, args.embedder_url, args.model))
    print(json.dumps(result, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
