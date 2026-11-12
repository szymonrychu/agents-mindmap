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
    409: (
        "conflict (version_conflict with the row version, slug_taken, edge_exists, link_exists,"
        " invalid_transition)"
    ),
    422: (
        "invalid request (invalid_request, project_unknown, project_archived, project_required,"
        " summary_required, parent_cycle, parent_invalid, root_immovable, self_edge,"
        " task_required, grown_node_invalid, node_invalid)"
    ),
    503: (
        "the orchestrator cannot validate the project (upstream_unavailable) or a read query"
        " timed out (query_timeout)"
    ),
}
WEB_ERRORS = {
    403: "source address is not allowed on the web listener (forbidden)",
    **_COMMON,
}
SERVICE_ERRORS = {
    400: "missing or malformed session or project scope headers (actor_required)",
    401: "no or an unknown bearer token (unauthorized)",
    403: "outside the caller's project scope (project_forbidden, withdraw_forbidden)",
    **_COMMON,
}

WEB_GUARD = (403,)
SERVICE_GUARD = (400, 401)

# (method, path under PREFIX) -> the error statuses besides the gate's, per listener.
USER_ROUTE_ERRORS: dict[tuple[str, str], tuple[int, ...]] = {
    ("get", "/projects"): (503,),
    ("get", "/maps"): (422, 503),
    ("post", "/maps"): (409, 422, 503),
    ("get", "/maps/{key}"): (404,),
    ("patch", "/maps/{key}"): (404, 409, 422),
    ("delete", "/maps/{key}"): (404,),
    ("post", "/maps/{key}/nodes"): (404, 409, 422, 503),
    ("get", "/nodes/{key}"): (404, 503),
    ("patch", "/nodes/{key}"): (404, 409, 422, 503),
    ("delete", "/nodes/{key}"): (404, 422),
    ("get", "/nodes/{key}/versions"): (404,),
    ("get", "/nodes/{key}/versions/{v}"): (404, 422),
    ("post", "/nodes/{key}/growth-areas"): (404, 422),
    ("patch", "/growth-areas/{key}"): (404, 409, 422),
    ("delete", "/growth-areas/{key}"): (404,),
    ("post", "/edges"): (404, 409, 422, 503),
    ("delete", "/edges/{key}"): (404,),
    ("get", "/links"): (422, 503),
    ("post", "/maps/{key}/links"): (404, 409, 422),
    ("post", "/nodes/{key}/links"): (404, 409, 422),
    ("delete", "/links/{link_id}"): (404, 422),
    ("get", "/directions"): (422, 503),
    ("post", "/maps/{key}/directions"): (404, 422),
    ("patch", "/directions/{key}"): (404, 409, 422),
    ("post", "/knowledge/notes"): (404, 409, 422, 503),
    ("get", "/nodes/{key}/traverse"): (404, 422, 503),
    ("get", "/maps/{key}/graph"): (404, 503),
    ("get", "/search"): (422, 503),
}
HARD_DELETES = {
    ("delete", "/maps/{key}"),
    ("delete", "/nodes/{key}"),
    ("delete", "/growth-areas/{key}"),
}
# Agents archive instead of deleting; every agent route can fail on scope (403) and on the
# orchestrator's active-project list (503).
SERVICE_ROUTE_ERRORS: dict[tuple[str, str], tuple[int, ...]] = {
    route: tuple(sorted({*statuses, 403, 503}))
    for route, statuses in USER_ROUTE_ERRORS.items()
    if route not in HARD_DELETES
}
ROUTE_ERRORS: dict[bool, dict[tuple[str, str], tuple[int, ...]]] = {
    False: USER_ROUTE_ERRORS,
    True: SERVICE_ROUTE_ERRORS,
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
