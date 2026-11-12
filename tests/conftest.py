"""Shared fixtures: one pgvector Postgres container per session and the common settings."""

import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import sql

from agents_mindmap.settings import Settings

PACKAGE = "agents_mindmap"
# pgvector is a superuser extension; this image ships it, as the CNPG standard image does live.
PG_IMAGE = "pgvector/pgvector:0.8.7-pg16"
RELAY_TOKEN = "relay-fixture"
DELEGATE_TOKEN = "delegate-fixture"
ORCH_TOKEN = "orch-fixture"
TASKBOARD_TOKEN = "taskboard-fixture"


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """Admin URL of one pgvector container for the whole session; overrides the agents_common
    plugin fixture (postgres:18-alpine has no pgvector)."""
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(PG_IMAGE, driver=None) as container:
        yield (
            f"postgresql://{container.username}:{container.password}"
            f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
            f"/{container.dbname}"
        )


@pytest.fixture
def db_url(migrated_db: Callable[[str], str]) -> str:
    """A fresh database cloned from the migrated template."""
    return migrated_db(PACKAGE)


@pytest.fixture
def empty_db_url(pg_url: str) -> Iterator[str]:
    """A fresh database with no schema at all."""
    name = f"e_{uuid.uuid4().hex}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        yield f"{pg_url.rsplit('/', 1)[0]}/{name}"
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def make_settings(database_url: str = "postgresql://u:p@db.invalid/mindmap", **extra) -> Settings:
    values = {
        "database_url": database_url,
        "orchestrator_url": "http://orchestrator.invalid",
        "orchestrator_token": ORCH_TOKEN,
        "public_base_url": "https://lab.example.test",
        "relay_token": RELAY_TOKEN,
        "delegate_tokens": [DELEGATE_TOKEN],
        "taskboard_url": "http://taskboard.invalid",
        "taskboard_token": TASKBOARD_TOKEN,
    }
    return Settings(**{**values, **extra})


@pytest.fixture
def settings() -> Settings:
    """Settings with an unreachable database: for tests that never touch Postgres."""
    return make_settings()
