"""Actor scope: users and agents read every active project, agents write only their own."""

import psycopg
import pytest
from test_maps_api import MAP, _insert_inactive, make_map

TABLES = (
    "maps",
    "nodes",
    "node_versions",
    "growth_areas",
    "links",
    "directions",
    "edges",
    "events",
)

# name -> (X-Orch-Projects, may write project p1)
AGENTS = {
    "home": ("p1", True),
    "grant": ("p2,p1", True),
    "superleader": ("*", True),
    "other": ("p2", False),
}


def counts(db_url):
    with psycopg.connect(db_url) as conn:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


async def seed(web_client):
    """Maps in p1 and p2, a node and a direction in p1, a node in p2."""
    m1 = await make_map(web_client)
    m2 = await make_map(web_client, project="p2", slug="two", title="two")
    n1 = (await web_client.post(f"/maps/{m1['key']}/nodes", json={"topic": "n1"})).json()
    n2 = (await web_client.post(f"/maps/{m2['key']}/nodes", json={"topic": "n2"})).json()
    d1 = (await web_client.post(f"/maps/{m1['key']}/directions", json={"text": "grow it"})).json()
    return m1, m2, n1, n2, d1


@pytest.mark.parametrize("name", AGENTS)
async def test_scope_matrix_reads(web_client, agent, db_url, name):
    m1, m2, n1, n2, _ = await seed(web_client)
    old_map, old_node = _insert_inactive(db_url)
    client = agent(AGENTS[name][0])
    listed = {m["key"] for m in (await client.get("/maps")).json()}
    assert listed == {m1["key"], m2["key"]}
    for path in (
        f"/maps/{m1['key']}",
        f"/maps/{m2['key']}",
        f"/nodes/{n1['key']}",
        f"/nodes/{n2['key']}",
        f"/nodes/{n2['key']}/versions",
        f"/nodes/{n2['key']}/versions/1",
    ):
        assert (await client.get(path)).status_code == 200, path
    for path in (f"/maps/{old_map}", f"/nodes/{old_node}", f"/nodes/{old_node}/versions"):
        assert (await client.get(path)).status_code == 404, path
    assert {p["project"] for p in (await client.get("/projects")).json()} == {"p1", "p2"}


async def test_scope_matrix_reads_user(web_client, db_url):
    """The user lists active projects only; by key the user is unscoped (spec 3.5)."""
    m1, m2, *_ = await seed(web_client)
    old_map, _ = _insert_inactive(db_url)
    assert {m["key"] for m in (await web_client.get("/maps")).json()} == {m1["key"], m2["key"]}
    assert (await web_client.get(f"/maps/{old_map}")).status_code == 200


@pytest.mark.parametrize("name", AGENTS)
async def test_scope_matrix_writes(web_client, agent, db_url, name):
    scope, may_write = AGENTS[name]
    m1, _, n1, n2, d1 = await seed(web_client)
    g1 = (await web_client.post(f"/nodes/{n1['key']}/growth-areas", json={"text": "g"})).json()
    client = agent(scope)
    k = m1["key"]
    writes = [
        ("patch", f"/maps/{k}", {"version": 1, "title": "x"}),
        ("post", f"/maps/{k}/nodes", {"topic": "new"}),
        ("patch", f"/nodes/{n1['key']}", {"version": 1, "status": "growing"}),
        ("post", f"/nodes/{n1['key']}/growth-areas", {"text": "more"}),
        ("patch", f"/growth-areas/{g1['key']}", {"version": 1, "text": "changed"}),
        ("post", f"/maps/{k}/links", {"ref_key": "T-1"}),
        ("post", f"/nodes/{n1['key']}/links", {"ref_key": "T-2"}),
        ("post", f"/maps/{k}/directions", {"text": "d"}),
        ("patch", f"/directions/{d1['key']}", {"version": 1, "action": "acknowledge"}),
        ("patch", f"/directions/{d1['key']}", {"version": 2, "action": "resolve"}),
        ("post", "/edges", {"src": n1["key"], "dst": n2["key"]}),
        ("post", "/knowledge/notes", {"project": "p1", "topic": "t", "notes": "n", "summary": "s"}),
        ("post", "/maps", {**MAP, "slug": "agent-made"}),
    ]
    for method, path, body in writes:
        before = counts(db_url)
        response = await getattr(client, method)(path, json=body)
        if may_write:
            assert response.status_code in (200, 201), (path, response.text)
        else:
            assert response.status_code == 403, (path, response.status_code)
            assert response.json()["error"]["code"] == "project_forbidden"
            assert counts(db_url) == before, path
            assert "p1" not in response.text.replace("project_forbidden", "")


async def test_agent_cannot_hard_delete(web_client, agent):
    (
        m1,
        *_,
    ) = await seed(web_client)
    client = agent("*")
    for path in (f"/maps/{m1['key']}", f"/nodes/{m1['root']}", "/growth-areas/G-1"):
        assert (await client.delete(path)).status_code == 405, path


async def test_agent_author_and_session(web_client, agent, db_url):
    client = agent("p1", session="sess-1", slug="alpha")
    created = (await client.post("/maps", json=MAP)).json()
    assert created["created_by"] == "session:alpha"
    with psycopg.connect(db_url) as conn:
        row = conn.execute("SELECT actor, actor_session FROM events").fetchone()
    assert row == ("session:alpha", "sess-1")
    anonymous = agent("p1", session="sess-2", slug=None)
    other = (await anonymous.post("/maps", json={**MAP, "slug": "two"})).json()
    assert other["created_by"] == "session:sess-2"


@pytest.mark.parametrize(
    "headers",
    [
        {"projects": None},
        {"projects": ""},
        {"projects": "P1"},
        {"projects": "p1,p1"},
        {"projects": ",".join(f"p{i}" for i in range(11))},
        {"session": ""},
    ],
)
async def test_service_rejects_missing_projects_header(agent, headers):
    client = agent(**headers)
    response = await client.get("/maps")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "actor_required"


async def test_agent_create_map_in_unknown_project_422(agent):
    client = agent("*")
    response = await client.post("/maps", json={**MAP, "project": "nope"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "project_unknown"
