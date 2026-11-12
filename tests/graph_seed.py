"""Direct SQL seeding of maps, nodes and edges for traversal and search tests (the API is
too slow for hundreds of rows)."""

from __future__ import annotations

import psycopg

AUTHOR = "user:seed"


def seed_map(db_url: str, project: str, slug: str, topics: list[str], *, status: str = "active"):
    """A map with one root (the first topic) and the other topics as its children; returns
    (map id, [node ids])."""
    with psycopg.connect(db_url) as conn:
        map_id = conn.execute(
            "INSERT INTO maps (project, slug, kind, title, description, status, created_by)"
            " VALUES (%s, %s, 'research', %s, '', 'active', %s) RETURNING id",
            (project, slug, slug, AUTHOR),
        ).fetchone()[0]
        ids: list[int] = []
        for i, topic in enumerate(topics):
            ids.append(
                conn.execute(
                    "INSERT INTO nodes (map_id, project, parent_id, topic, status, created_by,"
                    " updated_by) VALUES (%s, %s, %s, %s, 'seed', %s, %s) RETURNING id",
                    (map_id, project, ids[0] if i else None, topic, AUTHOR, AUTHOR),
                ).fetchone()[0]
            )
        conn.execute("UPDATE maps SET root_node_id = %s WHERE id = %s", (ids[0], map_id))
        conn.commit()
    return map_id, ids


def seed_edges(db_url: str, pairs: list[tuple[int, int]], kind: str = "relates_to") -> None:
    with psycopg.connect(db_url) as conn:
        for src, dst in pairs:
            conn.execute(
                "INSERT INTO edges (src_node_id, src_project, dst_node_id, dst_project, kind,"
                " created_by) SELECT s.id, s.project, d.id, d.project, %s, %s"
                " FROM nodes s, nodes d WHERE s.id = %s AND d.id = %s",
                (kind, AUTHOR, src, dst),
            )
        conn.commit()


def set_text(db_url: str, node_id: int, *, notes: str | None = None) -> None:
    with psycopg.connect(db_url) as conn:
        if notes is not None:
            conn.execute("UPDATE nodes SET notes = %s WHERE id = %s", (notes, node_id))
        conn.commit()


def add_growth(db_url: str, node_id: int, text: str, status: str = "open") -> None:
    with psycopg.connect(db_url) as conn:
        conn.execute(
            "INSERT INTO growth_areas (node_id, text, status, created_by) VALUES (%s,%s,%s,%s)",
            (node_id, text, status, AUTHOR),
        )
        conn.commit()


def key(prefix: str, id_: int) -> str:
    return f"{prefix}-{id_}"
