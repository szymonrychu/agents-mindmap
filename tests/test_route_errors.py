"""Secrets-model checklist: every route of every listener lists its statuses in ROUTE_ERRORS,
and the export documents exactly those."""

import pytest
from conftest import make_settings

from agents_mindmap.api.app import PREFIX, create_delegate_app, create_service_app, create_web_app
from agents_mindmap.api.openapi import (
    ROUTE_ERRORS,
    SERVICE_ERRORS,
    SERVICE_GUARD,
    WEB_ERRORS,
    WEB_GUARD,
    document,
)


def routes_of(app) -> set[tuple[str, str]]:
    """Every (method, path under the prefix) the app serves, probes excluded; read from the
    app's own OpenAPI because FastAPI includes routers lazily."""
    return {
        (method, path.removeprefix(PREFIX))
        for path, methods in app.openapi()["paths"].items()
        if path.startswith(PREFIX)
        for method in methods
    }


APPS = {
    "web": (False, lambda: create_web_app(make_settings(), None)),
    "delegate": (False, lambda: create_delegate_app(make_settings(), None)),
    "service": (True, lambda: create_service_app(make_settings(), None)),
}


@pytest.mark.parametrize("name", list(APPS))
def test_route_errors_listed(name: str):
    service, build = APPS[name]
    app_routes = routes_of(build())
    listed = set(ROUTE_ERRORS[service])
    assert app_routes - listed == set(), "route without ROUTE_ERRORS entry"
    assert listed - app_routes == set(), "ROUTE_ERRORS entry without a route"


@pytest.mark.parametrize("service", [False, True])
def test_documented_statuses_are_the_listed_ones(service: bool):
    guard = SERVICE_GUARD if service else WEB_GUARD
    errors = SERVICE_ERRORS if service else WEB_ERRORS
    for path, methods in document(service)["paths"].items():
        for method, operation in methods.items():
            statuses = {int(s) for s in operation["responses"] if not s.startswith("2")}
            expected = {*guard, *ROUTE_ERRORS[service][(method, path.removeprefix(PREFIX))]}
            assert statuses == expected, (method, path)
            assert statuses <= set(errors)


def test_web_and_delegate_serve_the_same_routes():
    assert routes_of(APPS["web"][1]()) == routes_of(APPS["delegate"][1]())
