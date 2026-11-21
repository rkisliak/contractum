"""The OpenAPI post-processor installed by create_app (PLAT-06, D-177, D-179)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from dc_core.app import create_app
from dc_core.openapi import PROBLEM_SCHEMA
from dc_core.policy import PolicyMeta, PolicyRouter, mark_policy, public
from dc_core.settings import CoreSettings
from fastapi import Depends, FastAPI
from pydantic import BaseModel

pytestmark = pytest.mark.unit

PROBLEM = "application/problem+json"
REF = {"$ref": "#/components/schemas/Problem"}


def guard(permission: str = "probe:read") -> Any:
    async def _policy() -> None:
        return None

    mark_policy(_policy, PolicyMeta(permission))
    return Depends(_policy)


class ProbeThingIn(BaseModel):
    name: str


def _app(*, with_routes: bool = True) -> FastAPI:
    router = PolicyRouter()
    if with_routes:

        @router.get("/api/v1/probe/ping", dependencies=[public()])
        async def ping() -> dict[str, str]:
            return {"ok": "yes"}

        @router.get("/api/v1/probe/search", dependencies=[public()])
        async def search(q: str = "") -> dict[str, str]:
            return {"q": q}

        @router.get(
            "/api/v1/probe/things/{thing_id}",
            dependencies=[guard()],
            responses={409: {"description": "The thing is archived"}},
        )
        async def get_thing(thing_id: uuid.UUID) -> dict[str, str]:
            return {"id": str(thing_id)}

        @router.post("/api/v1/probe/things", dependencies=[guard("probe:write")], tags=["things"])
        async def create_thing(body: ProbeThingIn) -> dict[str, str]:
            return {"name": body.name}

    internal = PolicyRouter()

    @internal.get("/internal/v1/probe/secret", dependencies=[guard()])
    async def secret() -> dict[str, str]:
        return {}

    return create_app(
        service="probe",
        settings=CoreSettings(service="probe", database_url="postgresql+psycopg://x/y"),
        routers=[router],
        internal_routers=[internal],
    )


async def _served(app: FastAPI) -> dict[str, Any]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:
        response = await client.get("/api/v1/probe/openapi.json")
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


def _assert_problem(op: dict[str, Any], status: str) -> None:
    assert status in op["responses"], (status, sorted(op["responses"]))
    assert op["responses"][status]["content"] == {PROBLEM: {"schema": REF}}
    assert op["responses"][status]["description"]


async def test_public_route_has_empty_security_and_guarded_route_bearer() -> None:
    doc = await _served(_app())
    ping = doc["paths"]["/api/v1/probe/ping"]["get"]
    thing = doc["paths"]["/api/v1/probe/things/{thing_id}"]["get"]
    assert ping["security"] == []
    assert thing["security"] == [{"bearerAuth": []}]
    assert ping["tags"] == ["probe"]
    assert thing["tags"] == ["probe"]


async def test_route_declared_tags_are_kept() -> None:
    doc = await _served(_app())
    assert doc["paths"]["/api/v1/probe/things"]["post"]["tags"] == ["things"]


async def test_guarded_route_documents_every_common_problem_status() -> None:
    doc = await _served(_app())
    thing = doc["paths"]["/api/v1/probe/things/{thing_id}"]["get"]
    for status in ("400", "401", "403", "404", "422", "429", "503"):
        _assert_problem(thing, status)
    assert "413" not in thing["responses"]  # no request body


async def test_public_route_documents_only_what_a_public_route_can_produce() -> None:
    doc = await _served(_app())
    ping = doc["paths"]["/api/v1/probe/ping"]["get"]
    for status in ("400", "429"):
        _assert_problem(ping, status)
    for status in ("401", "403", "404", "413", "422", "503"):
        assert status not in ping["responses"], status
    search = doc["paths"]["/api/v1/probe/search"]["get"]
    _assert_problem(search, "422")  # it has a query parameter
    assert "401" not in search["responses"]


async def test_body_route_documents_413_and_422() -> None:
    doc = await _served(_app())
    create = doc["paths"]["/api/v1/probe/things"]["post"]
    for status in ("400", "401", "403", "413", "422", "429", "503"):
        _assert_problem(create, status)
    assert "404" not in create["responses"]  # no path parameter


async def test_shared_components_and_no_fastapi_validation_schemas() -> None:
    doc = await _served(_app())
    components = doc["components"]
    assert components["securitySchemes"]["bearerAuth"] == {"type": "http", "scheme": "bearer"}
    schemas = components["schemas"]
    assert schemas["Problem"] == PROBLEM_SCHEMA
    assert schemas["Problem"]["additionalProperties"] is True
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas
    assert "HTTPValidationError" not in json.dumps(doc)


async def test_route_declared_status_keeps_its_description_and_gains_problem_content() -> None:
    doc = await _served(_app())
    thing = doc["paths"]["/api/v1/probe/things/{thing_id}"]["get"]
    assert thing["responses"]["409"]["description"] == "The thing is archived"
    _assert_problem(thing, "409")
    assert "default" not in thing["responses"]


async def test_internal_and_health_routes_stay_out_of_the_document() -> None:
    doc = await _served(_app())
    assert not [p for p in doc["paths"] if "/internal" in p]
    assert "/healthz" not in doc["paths"]
    assert "/readyz" not in doc["paths"]


def test_openapi_is_built_once_and_cached() -> None:
    app = _app()
    first = app.openapi()
    assert app.openapi() is first
    assert app.openapi_schema is first


def test_service_without_public_routes_has_empty_paths_and_shared_components() -> None:
    doc = _app(with_routes=False).openapi()
    assert doc["paths"] == {}
    assert doc["components"]["schemas"] == {"Problem": PROBLEM_SCHEMA}
    assert doc["components"]["securitySchemes"] == {
        "bearerAuth": {"type": "http", "scheme": "bearer"}
    }


class ValidationError(BaseModel):
    """A service model that happens to share a name with FastAPI's validation schema."""

    field: str


def _single_router_app(router: PolicyRouter) -> FastAPI:
    return create_app(
        service="probe",
        settings=CoreSettings(service="probe", database_url="postgresql+psycopg://x/y"),
        routers=[router],
    )


def test_range_and_default_responses_are_handled_by_their_key() -> None:
    router = PolicyRouter()

    @router.get(
        "/api/v1/probe/ranges",
        dependencies=[public()],
        responses={
            "4XX": {"description": "Any client error"},
            "default": {"description": "Anything else"},
        },
    )
    async def ranges() -> dict[str, str]:
        return {}

    op = _single_router_app(router).openapi()["paths"]["/api/v1/probe/ranges"]["get"]
    _assert_problem(op, "4XX")
    assert op["responses"]["4XX"]["description"] == "Any client error"
    assert "content" not in op["responses"]["default"]
    assert list(op["responses"])[-2:] == ["4XX", "default"]  # non-numeric keys sort last


def test_a_hidden_method_next_to_a_documented_one_stays_undocumented() -> None:
    router = PolicyRouter()

    @router.get("/api/v1/probe/shared", dependencies=[public()])
    async def visible() -> dict[str, str]:
        return {}

    @router.post("/api/v1/probe/shared", dependencies=[guard()], include_in_schema=False)
    async def hidden() -> dict[str, str]:
        return {}

    item = _single_router_app(router).openapi()["paths"]["/api/v1/probe/shared"]
    assert list(item) == ["get"]
    assert item["get"]["security"] == []


def test_an_operation_no_route_claims_gets_the_guarded_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dc_core.openapi as openapi_module

    app = _app()
    monkeypatch.setattr(openapi_module, "iter_route_contexts", lambda routes: iter(()))
    ping = app.openapi()["paths"]["/api/v1/probe/ping"]["get"]
    assert ping["security"] == [{"bearerAuth": []}]
    for status in ("401", "403", "503"):
        _assert_problem(ping, status)


def test_a_service_model_named_like_fastapis_validation_schema_is_kept() -> None:
    router = PolicyRouter()

    @router.get("/api/v1/probe/checks", dependencies=[public()])
    async def checks() -> ValidationError:
        return ValidationError(field="x")

    doc = _single_router_app(router).openapi()
    assert doc["components"]["schemas"]["ValidationError"]["properties"]["field"]["type"] == (
        "string"
    )
    ok = doc["paths"]["/api/v1/probe/checks"]["get"]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ValidationError"
    }
