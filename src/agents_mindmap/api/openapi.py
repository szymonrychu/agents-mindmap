"""Deterministic OpenAPI export per listener.

`python -m agents_mindmap.api.openapi > openapi.json` documents the web listener (users; the
delegate listener serves the same routes with 401 instead of 403 as its gate error);
`--service` documents the service listener (the orchestrator relay). FastAPI documents a
validation error as its own `HTTPValidationError`; this service answers every error with
`{"error": {"code", "message"}}` (see `errors.py`), so the export replaces the generated error
responses with that shape and the statuses each route really returns (`ROUTE_ERRORS`; a route
missing there fails the export). The probes (`/healthz`, `/readyz`) are not documented.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from agents_mindmap.api.app import PREFIX, create_service_app, create_web_app
from agents_mindmap.settings import Settings

ERROR_REF = {"$ref": "#/components/schemas/ErrorResponse"}

ERROR_SCHEMAS: dict[str, Any] = {
    "ErrorBody": {
        "type": "object",
        "title": "ErrorBody",
        "required": ["code", "message"],
        "additionalProperties": True,
        "properties": {
            "code": {"type": "string", "title": "Code"},
            "message": {"type": "string", "title": "Message"},
        },
    },
    "ErrorResponse": {
        "type": "object",
        "title": "ErrorResponse",
        "required": ["error"],
        "properties": {"error": {"$ref": "#/components/schemas/ErrorBody"}},
    },
}

_COMMON = {
    404: "unknown key (not_found)",
    409: "conflict (version_conflict with current_version, slug_taken)",
    422: (
        "invalid request (invalid_request, project_unknown, project_archived, summary_required,"
        " parent_cycle, parent_invalid, root_immovable)"
    ),
    503: "the orchestrator cannot validate the project (upstream_unavailable)",
}
WEB_ERRORS = {
    403: "source address is not allowed on the web listener (forbidden)",
    **_COMMON,
}
SERVICE_ERRORS = {
    401: "no or an unknown bearer token (unauthorized)",
    **_COMMON,
}

WEB_GUARD = (403,)
SERVICE_GUARD = (401,)

# (method, path under PREFIX) -> the error statuses besides the gate's, per listener.
USER_ROUTE_ERRORS: dict[tuple[str, str], tuple[int, ...]] = {
    ("get", "/projects"): (503,),
    ("get", "/maps"): (422, 503),
    ("post", "/maps"): (409, 422, 503),
    ("get", "/maps/{key}"): (404,),
    ("patch", "/maps/{key}"): (404, 409, 422),
    ("delete", "/maps/{key}"): (404,),
    ("post", "/maps/{key}/nodes"): (404, 422),
    ("get", "/nodes/{key}"): (404,),
    ("patch", "/nodes/{key}"): (404, 409, 422),
    ("delete", "/nodes/{key}"): (404, 422),
    ("get", "/nodes/{key}/versions"): (404,),
    ("get", "/nodes/{key}/versions/{v}"): (404, 422),
}
ROUTE_ERRORS: dict[bool, dict[tuple[str, str], tuple[int, ...]]] = {
    False: USER_ROUTE_ERRORS,
    True: {},
}


def document(service: bool = False) -> dict[str, Any]:
    """The OpenAPI document of the web listener, or of the service listener with `service`."""
    settings = Settings(
        database_url="postgresql://openapi/export",
        orchestrator_url="http://orchestrator.invalid",
        orchestrator_token="openapi-export-orchestrator",
        public_base_url="https://lab.example.test",
        relay_token="openapi-export-relay",
    )
    if service:
        app = create_service_app(settings, None)
        guard, errors = SERVICE_GUARD, SERVICE_ERRORS
    else:
        app = create_web_app(settings, None)
        guard, errors = WEB_GUARD, WEB_ERRORS
    route_errors = ROUTE_ERRORS[service]
    doc = app.openapi()
    doc["paths"] = {p: m for p, m in doc.get("paths", {}).items() if p.startswith(PREFIX)}
    for path, methods in doc["paths"].items():
        for method, operation in methods.items():
            responses = operation["responses"]
            for status in [s for s in responses if not s.startswith("2")]:
                del responses[status]
            statuses = (*guard, *route_errors[(method, path.removeprefix(PREFIX))])
            for status in sorted(statuses):
                responses[str(status)] = {
                    "description": errors[status],
                    "content": {"application/json": {"schema": ERROR_REF}},
                }
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    schemas.update(ERROR_SCHEMAS)
    return doc


def export(service: bool = False) -> str:
    """The listener's OpenAPI document as JSON with sorted keys and a trailing newline."""
    return json.dumps(document(service), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(export(service="--service" in sys.argv[1:]), end="")
