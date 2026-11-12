"""The 0001 schema: checksum guard, extension handling, constraints and the embedding config."""

import hashlib
import re
import uuid
from importlib import resources

import psycopg
import pytest
from agents_common.db.migrate import MigrationError, run_migrations
from conftest import PACKAGE, make_settings
from psycopg import errors, sql

from agents_mindmap.settings import EMBED_DIM, EMBED_MODEL

FILE = "0001_mindmap.sql"


def migration_text() -> str:
    return (resources.files(PACKAGE) / "migrations" / FILE).read_text()


def seed(conn: psycopg.Connection) -> tuple[int, int]:
    """One map with a root node; returns (map_id, node_id)."""
    map_id = conn.execute(
        "INSERT INTO maps (project, slug, title, status, created_by) "
        "VALUES ('p1', %s, 't', 'active', 'user:a@b.c') RETURNING id",
        (f"m-{uuid.uuid4().hex[:8]}",),
    ).fetchone()[0]
    node_id = conn.execute(
        "INSERT INTO nodes (map_id, project, topic, status, created_by, updated_by) "
        "VALUES (%s, 'p1', 'root', 'seed', 'user:a@b.c', 'user:a@b.c') RETURNING id",
        (map_id,),
    ).fetchone()[0]
    return map_id, node_id


def add_node(conn: psycopg.Connection, map_id: int, project: str = "p1") -> int:
    return conn.execute(
        "INSERT INTO nodes (map_id, project, topic, status, created_by, updated_by) "
        "VALUES (%s, %s, 'n', 'seed', 'user:a@b.c', 'user:a@b.c') RETURNING id",
        (map_id, project),
    ).fetchone()[0]


def test_checksum_stable(empty_db_url):
    assert run_migrations(empty_db_url, PACKAGE) == [FILE]
    assert run_migrations(empty_db_url, PACKAGE) == []
    with psycopg.connect(empty_db_url) as conn:
        recorded = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE package = %s AND name = %s",
            (PACKAGE, FILE),
        ).fetchone()[0]
        assert recorded == hashlib.sha256(migration_text().encode()).hexdigest()
        conn.execute("UPDATE schema_migrations SET checksum = 'changed'")
        conn.commit()
    with pytest.raises(MigrationError):
        run_migrations(empty_db_url, PACKAGE)


def test_pg_trgm_created_by_app_role(pg_url, empty_db_url):
    role = f"app_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'pw' NOSUPERUSER").format(sql.Identifier(role))
        )
        name = f"o_{uuid.uuid4().hex}"
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(role)
            )
        )
    host = pg_url.split("@", 1)[1].rsplit("/", 1)[0]
    url = f"postgresql://{role}:pw@{host}/{name}"
    assert run_migrations(url, PACKAGE) == [FILE]
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'").fetchone()


def test_vector_guard_raises(bare_db_url):
    with pytest.raises(psycopg.Error, match="vector extension missing"):
        run_migrations(bare_db_url, PACKAGE)


def test_node_project_must_match_map(db_url):
    with psycopg.connect(db_url, autocommit=True) as conn:
        map_id, _ = seed(conn)
        with pytest.raises(errors.ForeignKeyViolation):
            add_node(conn, map_id, project="other")


def test_self_edge_refused(db_url):
    with psycopg.connect(db_url, autocommit=True) as conn:
        _, node = seed(conn)
        with pytest.raises(errors.CheckViolation):
            conn.execute(
                "INSERT INTO edges (src_node_id, src_project, dst_node_id, dst_project, kind,"
                " created_by) VALUES (%s, 'p1', %s, 'p1', 'relates_to', 'user:a@b.c')",
                (node, node),
            )


def test_link_unique_with_null_node(db_url):
    with psycopg.connect(db_url, autocommit=True) as conn:
        map_id, _ = seed(conn)
        insert = (
            "INSERT INTO links (map_id, project, node_id, ref_key, relation, created_by) "
            "VALUES (%s, 'p1', NULL, 'T-1', 'researched_by', 'user:a@b.c')"
        )
        conn.execute(insert, (map_id,))
        with pytest.raises(errors.UniqueViolation):
            conn.execute(insert, (map_id,))


def test_single_knowledge_map_per_project(db_url):
    with psycopg.connect(db_url, autocommit=True) as conn:
        insert = (
            "INSERT INTO maps (project, slug, kind, title, status, created_by) "
            "VALUES ('p1', %s, 'knowledge', 'k', 'active', 'user:a@b.c')"
        )
        conn.execute(insert, ("knowledge",))
        with pytest.raises(errors.UniqueViolation):
            conn.execute(insert, ("knowledge-2",))


@pytest.mark.parametrize("ref", ["X-1", "T-0", "T-01", "t-1", "T-", "S-1x"])
def test_key_grammar_checks(db_url, ref):
    with psycopg.connect(db_url, autocommit=True) as conn:
        map_id, _ = seed(conn)
        with pytest.raises(errors.CheckViolation):
            conn.execute(
                "INSERT INTO links (map_id, project, ref_key, relation, created_by) "
                "VALUES (%s, 'p1', %s, 'cites', 'user:a@b.c')",
                (map_id, ref),
            )


def test_sql_dim_matches_constant(db_url):
    text = migration_text()
    assert re.findall(r"vector\((\d+)\)", text) == [str(EMBED_DIM)]
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT model, dim FROM embedding_config").fetchall() == [
            (EMBED_MODEL, EMBED_DIM)
        ]
        typmod = conn.execute(
            "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'node_chunks'::regclass"
            " AND attname = 'embedding'"
        ).fetchone()[0]
        assert typmod == EMBED_DIM


async def test_startup_refuses_embedding_mismatch(db_url):
    from agents_mindmap.runtime import EmbeddingConfigError, open_pool, verify_embedding_config

    settings = make_settings(db_url, embed_dim=768)
    pool = await open_pool(settings)
    try:
        with pytest.raises(EmbeddingConfigError, match="MINDMAP_EMBED_DIM"):
            await verify_embedding_config(pool, settings)
        await verify_embedding_config(pool, make_settings(db_url))
        with pytest.raises(EmbeddingConfigError, match="MINDMAP_EMBEDDER_MODEL"):
            await verify_embedding_config(pool, make_settings(db_url, embedder_model="other/model"))
    finally:
        await pool.close()
