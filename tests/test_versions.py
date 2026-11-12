"""Node version history routes."""

from test_maps_api import MAP, make_map


async def setup(client):
    created = await make_map(client)
    return created["key"], created["root"]


async def test_versions_ordered_with_author_summary(web_client):
    _, root = await setup(web_client)
    await web_client.patch(f"/nodes/{root}", json={"version": 1, "notes": "a", "summary": "first"})
    await web_client.patch(f"/nodes/{root}", json={"version": 2, "topic": "t", "summary": "second"})
    versions = (await web_client.get(f"/nodes/{root}/versions")).json()
    assert [(v["version"], v["author"], v["summary"]) for v in versions] == [
        (1, "user:alice@example.test", "created"),
        (2, "user:alice@example.test", "first"),
        (3, "user:alice@example.test", "second"),
    ]
    assert "notes" not in versions[0]
    full = (await web_client.get(f"/nodes/{root}/versions/2")).json()
    assert (full["notes"], full["topic"]) == ("a", MAP["title"])
    assert (await web_client.get(f"/nodes/{root}/versions/9")).status_code == 404
    assert (await web_client.get("/nodes/N-999/versions")).status_code == 404
