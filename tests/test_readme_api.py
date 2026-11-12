"""The README API section lists every route of both OpenAPI documents, and nothing else."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
PREFIX = "/mindmap/api/v1"
TOKEN = re.compile(r"`(GET|POST|PATCH|PUT|DELETE) (/[^`\s]*)`")


def section(heading: str) -> str:
    match = re.search(rf"^### {re.escape(heading)}\n(.*?)(?=^#{{2,3}} |\Z)", README, re.M | re.S)
    assert match, f"README has no '### {heading}' section"
    return match.group(1)


def documented(name: str) -> set[tuple[str, str]]:
    doc = json.loads((ROOT / name).read_text())
    return {
        (method.upper(), path.removeprefix(PREFIX))
        for path, methods in doc["paths"].items()
        for method in methods
    }


@pytest.mark.parametrize(
    ("heading", "name"),
    [
        ("Web and delegate routes", "openapi.json"),
        ("Service routes", "openapi-service.json"),
    ],
)
def test_readme_api(heading: str, name: str):
    listed = set(TOKEN.findall(section(heading)))
    expected = documented(name)
    assert expected - listed == set(), f"missing from the README: {sorted(expected - listed)}"
    assert listed - expected == set(), f"not in {name}: {sorted(listed - expected)}"


def test_readme_documents_the_feed():
    text = section("Web and delegate routes")
    assert "`GET /events`" in text and "`GET /stream`" in text
    assert "mindmap_changed" in README
