"""Shared fixtures: one pgvector Postgres container per session and the common settings."""

import uuid
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from agents_mindmap.api.app import PREFIX, create_web_app
from agents_mindmap.projects import ProjectLookup
from agents_mindmap.runtime import open_pool
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
        base = (
            f"postgresql://{container.username}:{container.password}"
            f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
        )
        # CNPG creates vector at bootstrap (postInitApplicationSQL); here every new database
        # inherits it from template1, and `bare_db_url` clones template0 to get none.
        with psycopg.connect(f"{base}/template1", autocommit=True) as admin:
            admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        yield f"{base}/{container.dbname}"


@pytest.fixture
def db_url(migrated_db: Callable[[str], str]) -> str:
    """A fresh database cloned from the migrated template."""
    return migrated_db(PACKAGE)


@pytest.fixture
def empty_db_url(pg_url: str) -> Iterator[str]:
    """A fresh database with no schema at all (the vector extension, as CNPG bootstraps it)."""
    yield from _fresh_db(pg_url, "template1")


@pytest.fixture
def bare_db_url(pg_url: str) -> Iterator[str]:
    """A fresh database cloned from template0: not even the vector extension."""
    yield from _fresh_db(pg_url, "template0")


def _fresh_db(pg_url: str, template: str) -> Iterator[str]:
    name = f"e_{uuid.uuid4().hex}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(template)
            )
        )
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


ACTIVE_PROJECTS = ("p1", "p2")
WEB_HEADERS = {"X-Forwarded-Email": "alice@example.test"}


def fake_orchestrator() -> httpx.MockTransport:
    """Projects p1 and p2 are active, `old` is archived, everything else is unknown."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/projects":
            return httpx.Response(
                200, json=[{"slug": s, "archived": False} for s in ACTIVE_PROJECTS]
            )
        slug = path.rsplit("/", 1)[-1]
        if slug in ACTIVE_PROJECTS:
            return httpx.Response(200, json={"slug": slug, "archived": False})
        if slug == "old":
            return httpx.Response(200, json={"slug": slug, "archived": True})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    return httpx.MockTransport(handler)


@pytest.fixture
async def pool(db_url: str) -> AsyncIterator[AsyncConnectionPool]:
    opened = await open_pool(make_settings(db_url))
    yield opened
    await opened.close()


@pytest.fixture
async def web_client(db_url: str, pool: AsyncConnectionPool) -> AsyncIterator[httpx.AsyncClient]:
    """The web app against a migrated database and a fake orchestrator; as the proxy's user."""
    app = create_web_app(make_settings(db_url), pool)
    app.state.projects = ProjectLookup(
        "http://orchestrator.invalid", "t", transport=fake_orchestrator()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://test" + PREFIX,
        headers=WEB_HEADERS,
    ) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


@pytest.fixture
async def agent(db_url: str, pool: AsyncConnectionPool):
    """Factory of service-listener clients: `agent("p1,p2", session="s1", slug="alpha")` acts
    with that project scope (`*` for a superleader). The service app shares the web app's
    database and a fake orchestrator."""
    from agents_mindmap.api.app import create_service_app

    app = create_service_app(make_settings(db_url), pool)
    app.state.projects = ProjectLookup(
        "http://orchestrator.invalid", "t", transport=fake_orchestrator()
    )
    clients: list[httpx.AsyncClient] = []

    def make(projects: str | None = "p1", *, session: str = "s1", slug: str | None = "alpha"):
        headers = {"Authorization": f"Bearer {RELAY_TOKEN}"}
        if session:
            headers["X-Orch-Session"] = session
        if slug:
            headers["X-Orch-Slug"] = slug
        if projects is not None:
            headers["X-Orch-Projects"] = projects
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("10.0.0.1", 1)),
            base_url="http://test" + PREFIX,
            headers=headers,
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()
