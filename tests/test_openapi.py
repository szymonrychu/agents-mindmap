"""`python -m agents_mindmap.api.openapi [--service]` writes a valid OpenAPI 3 document."""

import json
import subprocess
import sys

import pytest

from agents_mindmap.api.openapi import ERROR_SCHEMAS, document


@pytest.mark.parametrize(
    ("args", "title"), [([], "agents-mindmap"), (["--service"], "agents-mindmap-service")]
)
def test_openapi_module_writes_docs(args, title):
    proc = subprocess.run(
        [sys.executable, "-m", "agents_mindmap.api.openapi", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    doc = json.loads(proc.stdout)
    assert doc["openapi"].startswith("3.")
    assert doc["info"]["title"] == title
    assert isinstance(doc["paths"], dict)
    for name in ERROR_SCHEMAS:
        assert name in doc["components"]["schemas"]
    assert proc.stdout.endswith("}\n")


def test_probes_are_not_documented():
    for service in (False, True):
        assert not {"/healthz", "/readyz"} & set(document(service)["paths"])


def test_export_is_deterministic():
    first = subprocess.run(
        [sys.executable, "-m", "agents_mindmap.api.openapi"], capture_output=True, check=True
    ).stdout
    second = subprocess.run(
        [sys.executable, "-m", "agents_mindmap.api.openapi"], capture_output=True, check=True
    ).stdout
    assert first == second
