"""Map routes on the web and delegate listeners."""

import httpx
from conftest import DELEGATE_TOKEN, ORCH_TOKEN, make_settings

from agents_mindmap.api.app import PREFIX, create_delegate_app

MAP = {"project": "p1", "slug": "pgvector", "title": "pgvector research"}


async def make_map(client, **extra):
    response = await client.post("/maps", json={**MAP, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def events(db_url, kind=None):
    import psycopg

    with psycopg.connect(db_url) as conn:
        rows = conn.execute("SELECT kind, actor, detail FROM events ORDER BY id").fetchall()
    return [r for r in rows if kind is None or r[0] == kind]


async def test_create_map_creates_root_and_version(web_client, db_url):
    created = await make_map(web_client, root_topic="pgvector")
    assert created["key"].startswith("M-")
    assert created["created_by"] == "user:alice@example.test"
    root = (await web_client.get(f"/nodes/{created['root']}")).json()
    assert root["topic"] == "pgvector"
    assert root["parent"] is None
    assert root["current_version"] == 1
    versions = (await web_client.get(f"/nodes/{created['root']}/versions")).json()
    assert [(v["version"], v["summary"]) for v in versions] == [(1, "created")]
    assert [e[0] for e in events(db_url)] == ["map_created"]


async def test_create_map_unknown_project_422(web_client):
    response = await web_client.post("/maps", json={**MAP, "project": "nope"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "project_unknown"
    archived = await web_client.post("/maps", json={**MAP, "project": "old"})
    assert archived.json()["error"]["code"] == "project_archived"


async def test_slug_taken_409(web_client):
    await make_map(web_client)
    again = await web_client.post("/maps", json=MAP)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "slug_taken"


async def test_idempotent_create_client_key(web_client, db_url):
    first = await make_map(web_client, client_key="k1")
    second = await make_map(web_client, client_key="k1", slug="other")
    assert second["key"] == first["key"]
    listed = (await web_client.get("/maps")).json()
    assert len(listed) == 1
    assert len(events(db_url, "map_created")) == 1


async def test_list_and_filter_maps(web_client):
    await make_map(web_client)
    await make_map(web_client, project="p2", slug="other", title="other")
    assert len((await web_client.get("/maps")).json()) == 2
    only = (await web_client.get("/maps", params={"project": "p2"})).json()
    assert [m["project"] for m in only] == ["p2"]
    assert (await web_client.get("/maps", params={"status": "archived"})).json() == []
    assert (await web_client.get("/maps", params={"kind": "knowledge"})).json() == []
    assert (await web_client.get("/maps", params={"status": "bogus"})).status_code == 422


async def test_projects_counts(web_client):
    created = await make_map(web_client)
    await web_client.post(f"/maps/{created['key']}/nodes", json={"topic": "child"})
    assert (await web_client.get("/projects")).json() == [{"project": "p1", "maps": 1, "nodes": 2}]


async def test_patch_map_guarded(web_client):
    created = await make_map(web_client)
    key = created["key"]
    patched = await web_client.patch(
        f"/maps/{key}", json={"version": 1, "status": "archived", "title": "renamed"}
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "archived"
    assert patched.json()["version"] == 2
    stale = await web_client.patch(f"/maps/{key}", json={"version": 1, "title": "x"})
    assert stale.status_code == 409
    assert stale.json()["error"]["version"] == 2
    assert (await web_client.patch("/maps/M-999", json={"version": 1})).status_code == 404


async def test_get_unknown_and_malformed_keys_404(web_client):
    for key in ("M-999", "X-1", "M-0", "M-1abc", "M-99999999999999999999999"):
        response = await web_client.get(f"/maps/{key}")
        assert response.status_code == 404, key
        assert response.json() == {"error": {"code": "not_found", "message": "not found"}}


async def test_delete_map_cascades(web_client, db_url):
    import psycopg

    created = await make_map(web_client)
    node = (await web_client.post(f"/maps/{created['key']}/nodes", json={"topic": "c"})).json()
    assert (await web_client.delete(f"/maps/{created['key']}")).status_code == 204
    assert (await web_client.get(f"/maps/{created['key']}")).status_code == 404
    assert (await web_client.get(f"/nodes/{node['key']}")).status_code == 404
    with psycopg.connect(db_url) as conn:
        for table in ("nodes", "node_versions"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert (await web_client.delete(f"/maps/{created['key']}")).status_code == 404
    assert [e[0] for e in events(db_url)][-1] == "map_deleted"


async def test_unknown_field_rejected_without_echo(web_client):
    response = await web_client.post("/maps", json={**MAP, "CANARY-FIELD": 1})
    assert response.status_code == 422
    assert "CANARY" not in response.text


async def test_delegate_serves_same_routes(db_url, pool):
    from conftest import ProjectLookup, fake_orchestrator

    app = create_delegate_app(make_settings(db_url), pool)
    app.state.projects = ProjectLookup("http://o.invalid", "t", transport=fake_orchestrator())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test" + PREFIX,
        headers={"Authorization": f"Bearer {DELEGATE_TOKEN}"},
    ) as client:
        created = await client.post("/maps", json=MAP)
        assert created.status_code == 201
        assert created.json()["created_by"] == "user:unknown"
        assert (await client.get("/maps")).status_code == 200
    assert ORCH_TOKEN not in created.text


def _insert_inactive(db_url):
    """A map with a root node and a second version in the archived project `old`."""
    import psycopg

    with psycopg.connect(db_url) as conn:
        map_id = conn.execute(
            "INSERT INTO maps (project, slug, kind, title, description, status, created_by)"
            " VALUES ('old', 's', 'knowledge', 't', '', 'active', 'user:x') RETURNING id"
        ).fetchone()[0]
        node_id = conn.execute(
            "INSERT INTO nodes (map_id, project, topic, status, created_by, updated_by)"
            " VALUES (%s, 'old', 'r', 'seed', 'user:x', 'user:x') RETURNING id",
            (map_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO node_versions (node_id, version, topic, notes, author, summary)"
            " VALUES (%s, 1, 'r', '', 'user:x', 'created')",
            (node_id,),
        )
        conn.commit()
    return f"M-{map_id}", f"N-{node_id}"


async def test_user_key_reads_are_unscoped(web_client, db_url):
    """Spec 3.5: the user (web, delegate) is unscoped, so key reads see archived projects too."""
    map_key, node_key = _insert_inactive(db_url)
    for path in (
        f"/maps/{map_key}",
        f"/nodes/{node_key}",
        f"/nodes/{node_key}/versions",
        f"/nodes/{node_key}/versions/1",
    ):
        response = await web_client.get(path)
        assert response.status_code == 200, path


async def test_delegate_key_reads_are_unscoped(db_url, pool):
    map_key, node_key = _insert_inactive(db_url)
    from conftest import ProjectLookup, fake_orchestrator

    app = create_delegate_app(make_settings(db_url), pool)
    app.state.projects = ProjectLookup("http://o.invalid", "t", transport=fake_orchestrator())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test" + PREFIX,
        headers={"Authorization": f"Bearer {DELEGATE_TOKEN}"},
    ) as client:
        for path in (f"/maps/{map_key}", f"/nodes/{node_key}", f"/nodes/{node_key}/versions/1"):
            assert (await client.get(path)).status_code == 200, path


async def test_user_identity_header_order_and_fallback(web_client):
    async def author(headers, slug):
        response = await web_client.post(
            "/maps", json={**MAP, "slug": slug}, headers={"X-Forwarded-Email": "", **headers}
        )
        assert response.status_code == 201, response.text
        return response.json()["created_by"]

    both = {"X-Forwarded-Email": "a@x.test", "X-Forwarded-User": "bob"}
    assert await author(both, "a") == "user:a@x.test"
    assert await author({"X-Forwarded-User": "bob"}, "b") == "user:bob"
    assert await author({}, "c") == "user:unknown"
    assert await author({"X-Forwarded-User": "bad\x01name"}, "d") == "user:unknown"
    assert await author({"X-Forwarded-User": "x" * 201}, "e") == "user:unknown"


async def test_two_knowledge_maps_in_one_project(web_client):
    first = await make_map(web_client, kind="knowledge", slug="kb-one")
    second = await make_map(web_client, kind="knowledge", slug="kb-two")
    assert first["key"] != second["key"]
    listed = (await web_client.get("/maps", params={"kind": "knowledge"})).json()
    assert len(listed) == 2


async def test_duplicate_slug_still_conflicts(web_client):
    await make_map(web_client, kind="knowledge", slug="kb-one")
    again = await web_client.post("/maps", json={**MAP, "kind": "knowledge", "slug": "kb-one"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "slug_taken"
