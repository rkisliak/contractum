"""RFC 9457 problem matrix and the encoded-path guard (PLAT-07, T-01-06, T-01-61)."""

from __future__ import annotations

import json
import uuid

import pytest
from dc_core.app import create_app
from dc_core.problems import ProblemError, problem_response
from dc_core.settings import CoreSettings
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

pytestmark = pytest.mark.unit


class Body(BaseModel):
    name: str


def _app():  # type: ignore[no-untyped-def]
    router = APIRouter()

    @router.get("/api/v1/probe/status/{code}")
    async def status(code: int) -> None:
        raise HTTPException(status_code=code)

    @router.get("/api/v1/probe/problem")
    async def problem() -> None:
        raise ProblemError(409, "conflict", "Conflict", "already there", headers={"X-Extra": "1"})

    @router.post("/api/v1/probe/body")
    async def body(payload: Body) -> dict[str, str]:
        return {"name": payload.name}

    @router.get("/api/v1/probe/boom")
    async def boom() -> None:
        raise RuntimeError("database password is hunter2")

    @router.get("/api/v1/probe/nonstandard")
    async def nonstandard() -> None:
        raise HTTPException(status_code=499, detail={"reason": "client closed"})

    @router.get("/api/v1/probe/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    return create_app(
        service="probe",
        settings=CoreSettings(service="probe", database_url="postgresql+psycopg://x/y"),
        routers=[router],
    )


def _assert_problem(response, status: int, slug: str) -> dict:  # type: ignore[no-untyped-def,type-arg]
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == f"urn:dc:problem:{slug}"
    assert body["status"] == status
    assert "title" in body
    return body  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("code", "slug"),
    [
        (401, "unauthenticated"),
        (403, "forbidden"),
        (404, "not-found"),
        (409, "conflict"),
        (413, "payload-too-large"),
        (415, "unsupported-media-type"),
    ],
)
async def test_status_matrix_is_problem_json(asgi_client, code: int, slug: str) -> None:  # type: ignore[no-untyped-def]
    async with asgi_client(_app()) as client:
        _assert_problem(await client.get(f"/api/v1/probe/status/{code}"), code, slug)


async def test_unknown_route_is_404_problem(asgi_client) -> None:  # type: ignore[no-untyped-def]
    async with asgi_client(_app()) as client:
        _assert_problem(await client.get("/api/v1/probe/nope"), 404, "not-found")


async def test_wrong_method_is_405_problem(asgi_client) -> None:  # type: ignore[no-untyped-def]
    async with asgi_client(_app()) as client:
        response = await client.post("/api/v1/probe/ping")
        _assert_problem(response, 405, "method-not-allowed")
        assert "allow" in response.headers


async def test_problem_error_carries_headers_and_request_id(asgi_client) -> None:  # type: ignore[no-untyped-def]
    async with asgi_client(_app()) as client:
        response = await client.get("/api/v1/probe/problem")
        body = _assert_problem(response, 409, "conflict")
        assert body["detail"] == "already there"
        assert response.headers["x-extra"] == "1"
        assert body["request_id"] == response.headers["x-request-id"]


async def test_validation_error_has_pointers_and_never_echoes_input(asgi_client) -> None:  # type: ignore[no-untyped-def]
    async with asgi_client(_app()) as client:
        # A number is not a string. Its text holds a ".", which is not a hex digit, so it never
        # turns up by chance in the random request id.
        response = await client.post(
            "/api/v1/probe/body", json={"name": 9876.54321, "extra": "S3CR3T"}
        )
        body = _assert_problem(response, 422, "validation-error")
    assert body["errors"][0]["pointer"] == "/body/name"
    assert "detail" in body["errors"][0]
    assert "9876.54321" not in response.text
    assert "S3CR3T" not in response.text


async def test_unhandled_exception_is_generic_500_with_request_id(asgi_client) -> None:  # type: ignore[no-untyped-def]
    async with asgi_client(_app(), raise_app_exceptions=False) as client:
        request_id = uuid.uuid4().hex
        response = await client.get("/api/v1/probe/boom", headers={"X-Request-ID": request_id})
        body = _assert_problem(response, 500, "internal-error")
        assert body["detail"] == "Internal server error"
        assert "hunter2" not in response.text
        # The 500 response is rendered outside CorrelationIdMiddleware, so no response header.
        assert body["request_id"] == request_id


@pytest.mark.parametrize(
    "target",
    [
        "/api/v1/probe%2Fwidgets",
        "/api/v1/probe/widgets%2f..%2fx",
        "/api/v1/probe/widgets%2F..%2Fx",
        "/api/v1/probe%5Cx",
        "/api/v1/probe%5cx",
        "/api/v1/probe/%00",
        "/api/v1/probe;%3Bx",
        "/api/v1/probe/%3bx",
        "/healthz%2F",
    ],
)
async def test_encoded_path_guard_rejects_before_auth_and_routing(asgi_client, target: str) -> None:  # type: ignore[no-untyped-def]
    async with asgi_client(_app()) as client:
        request = client.build_request("GET", target)
        # Guard against a vacuous test: the client must not have re-encoded the target.
        assert request.url.raw_path.decode().split("?")[0].lower() == target.lower()
        response = await client.send(request)
        _assert_problem(response, 400, "invalid-path")
    assert response.json()["request_id"]
    assert "widgets" not in response.text
    assert "%" not in response.text


async def test_encoded_path_guard_ignores_query_string(asgi_client) -> None:  # type: ignore[no-untyped-def]
    async with asgi_client(_app()) as client:
        response = await client.get("/api/v1/probe/ping?path=a%2Fb")
    assert response.status_code == 200


async def test_a_nonstandard_status_gets_a_generic_title_and_no_structured_detail(
    asgi_client,
) -> None:  # type: ignore[no-untyped-def]
    async with asgi_client(_app()) as client:
        response = await client.get("/api/v1/probe/nonstandard")
    body = _assert_problem(response, 499, "bad-request")
    assert body["title"] == "Error"
    assert "detail" not in body
    assert "client closed" not in response.text


def test_a_problem_rendered_outside_a_request_has_no_request_id_or_detail() -> None:
    response = problem_response(503, "service-unavailable", "Service Unavailable")
    assert response.status_code == 503
    assert json.loads(response.body) == {
        "type": "urn:dc:problem:service-unavailable",
        "title": "Service Unavailable",
        "status": 503,
    }
