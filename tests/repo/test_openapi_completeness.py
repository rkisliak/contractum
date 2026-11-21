"""Every endpoint of every service is documented (PLAT-06, D-179, T-7-05, T-7-06).

The expectations come from the routes themselves (``APIRoute`` and ``find_policy``), so there is
no hand-kept route list: a new route is checked the moment it exists.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from dc_core.policy import find_policy
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

pytestmark = pytest.mark.unit

PROBLEM = "application/problem+json"
PROBLEM_REF = "#/components/schemas/Problem"
SERVICES = ("identity", "contracts", "validation", "audit", "mail")
METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _routes(app: FastAPI) -> list[tuple[str, str, bool]]:
    """(METHOD, path, public) for every APIRoute included in the schema."""
    out: list[tuple[str, str, bool]] = []
    for rc in iter_route_contexts(app.routes):
        if not isinstance(rc.original_route, APIRoute) or not rc.include_in_schema:
            continue
        meta = find_policy(rc.dependant)
        assert meta is not None, rc.path
        for method in sorted(rc.methods or ()):
            out.append((method, rc.path_format or "", meta.public))
    return out


def _operations(doc: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method.upper(), path, op)
        for path, item in doc["paths"].items()
        for method, op in item.items()
        if method in METHODS
    ]


@pytest.fixture(scope="module")
def documents(service_apps: dict[str, FastAPI]) -> dict[str, dict[str, Any]]:
    assert set(service_apps) == set(SERVICES)
    return {name: app.openapi() for name, app in service_apps.items()}


@pytest.mark.parametrize("service", SERVICES)
def test_spec_is_served_under_the_module_prefix(
    service: str, service_apps: dict[str, FastAPI]
) -> None:
    assert service_apps[service].openapi_url == f"/api/v1/{service}/openapi.json"


@pytest.mark.parametrize("service", SERVICES)
def test_every_schema_route_is_documented_and_no_internal_or_health_path_is(
    service: str, service_apps: dict[str, FastAPI], documents: dict[str, dict[str, Any]]
) -> None:
    doc = documents[service]
    documented = {(m, p) for m, p, _ in _operations(doc)}
    expected = {(m, p) for m, p, _ in _routes(service_apps[service])}
    assert expected - documented == set()
    assert documented - expected == set()
    assert not [p for p in doc["paths"] if "/internal" in p]
    assert "/healthz" not in doc["paths"]
    assert "/readyz" not in doc["paths"]


def test_operation_ids_are_unique_across_all_services(
    documents: dict[str, dict[str, Any]],
) -> None:
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for service, doc in documents.items():
        for method, path, op in _operations(doc):
            oid = op.get("operationId")
            assert oid, f"{service} {method} {path} has no operationId"
            if oid in seen:
                duplicates.append(f"{oid}: {seen[oid]} and {service}")
            seen[oid] = service
    assert duplicates == []
    assert len(seen) > 100  # the walk is not vacuous


@pytest.mark.parametrize("service", SERVICES)
def test_every_operation_has_a_summary_a_tag_and_its_policy_security(
    service: str, service_apps: dict[str, FastAPI], documents: dict[str, dict[str, Any]]
) -> None:
    public = {(m, p): pub for m, p, pub in _routes(service_apps[service])}
    for method, path, op in _operations(documents[service]):
        label = f"{service} {method} {path}"
        assert op.get("summary"), label
        assert op.get("tags"), label
        assert "security" in op, label
        if public[(method, path)]:
            assert op["security"] == [], label
        else:
            assert op["security"] == [{"bearerAuth": []}], label


@pytest.mark.parametrize("service", SERVICES)
def test_every_error_response_is_a_problem_and_guarded_routes_declare_401(
    service: str, service_apps: dict[str, FastAPI], documents: dict[str, dict[str, Any]]
) -> None:
    public = {(m, p): pub for m, p, pub in _routes(service_apps[service])}
    for method, path, op in _operations(documents[service]):
        label = f"{service} {method} {path}"
        responses = op["responses"]
        assert "default" not in responses, label
        for status in ("400", "429"):
            assert status in responses, f"{label} lacks {status}"
        if not public[(method, path)]:
            for status in ("401", "403", "503"):
                assert status in responses, f"{label} lacks {status}"
        if "{" in path:
            assert "404" in responses, f"{label} lacks 404"
        if op.get("parameters") or "requestBody" in op:
            assert "422" in responses, f"{label} lacks 422"
        if "requestBody" in op:
            assert "413" in responses, f"{label} lacks 413"
        for status, response in responses.items():
            if status[0] in "45":
                assert response.get("description"), f"{label} {status}"
                assert response.get("content") == {PROBLEM: {"schema": {"$ref": PROBLEM_REF}}}, (
                    f"{label} {status}"
                )


@pytest.mark.parametrize("service", SERVICES)
def test_fastapi_validation_schemas_are_gone(
    service: str, documents: dict[str, dict[str, Any]]
) -> None:
    text = json.dumps(documents[service])
    assert "HTTPValidationError" not in text
    assert '"#/components/schemas/ValidationError"' not in text
    assert "ValidationError" not in documents[service]["components"]["schemas"]


def test_shared_components_are_byte_identical_in_every_document(
    documents: dict[str, dict[str, Any]],
) -> None:
    def canon(body: Any) -> str:
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    problems = {canon(doc["components"]["schemas"]["Problem"]) for doc in documents.values()}
    schemes = {
        canon(doc["components"]["securitySchemes"]["bearerAuth"]) for doc in documents.values()
    }
    assert len(problems) == 1
    assert schemes == {canon({"type": "http", "scheme": "bearer"})}


@pytest.mark.parametrize("service", ["audit", "mail"])
def test_services_without_public_routes_produce_a_valid_empty_document(
    service: str, service_apps: dict[str, FastAPI], documents: dict[str, dict[str, Any]]
) -> None:
    # Edge: audit (until AUD-05 adds its public query) and mail expose only internal routes.
    if [r for r in _routes(service_apps[service])]:
        pytest.skip(f"{service} now has public routes; covered by the generic tests")
    doc = documents[service]
    assert doc["paths"] == {}
    assert "Problem" in doc["components"]["schemas"]
    assert "bearerAuth" in doc["components"]["securitySchemes"]
