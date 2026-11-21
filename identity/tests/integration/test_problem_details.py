"""RFC 9457 problem matrix on identity and the OpenAPI surface (PLAT-07)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

pytestmark = pytest.mark.integration


def _is_problem(resp: Any, status: int) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["status"] == status
    assert body["type"].startswith("urn:dc:problem:")
    return body


async def test_unknown_identity_path_is_a_404_problem(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        body = _is_problem(await client.get("/api/v1/identity/nope"), 404)
    assert body["type"] == "urn:dc:problem:not-found"


async def test_wrong_method_on_login_is_a_405_problem(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        body = _is_problem(await client.get("/api/v1/identity/auth/login"), 405)
    assert body["type"] == "urn:dc:problem:method-not-allowed"


async def test_malformed_json_on_login_is_a_422_problem_before_authentication(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    # Documented exception A14 (accepted in D-21): Pydantic-bodied routes validate first.
    env = make_identity()
    async with asgi_client(env.app) as client:
        resp = await client.post(
            "/api/v1/identity/auth/login",
            content=b'{"username": "admin", ',
            headers={"Content-Type": "application/json"},
        )
        body = _is_problem(resp, 422)
        extra = await client.post(
            "/api/v1/identity/auth/login", json={"username": "a", "password": "b", "x": 1}
        )
        _is_problem(extra, 422)
    assert body["type"] == "urn:dc:problem:validation-error"


async def test_password_route_without_a_token_is_a_401_problem(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        resp = await client.post(
            "/api/v1/identity/auth/password", json={"new_password": "irrelevant password"}
        )
    body = _is_problem(resp, 401)
    assert body["type"] == "urn:dc:problem:unauthenticated"
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_openapi_documents_public_routes_and_never_the_internal_api(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        resp = await client.get("/api/v1/identity/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/api/v1/identity/auth/login" in paths
    assert "/api/v1/identity/auth/password" in paths
    assert not [p for p in paths if "/internal" in p]
    # JWKS is a service-level well-known document that the gateway never routes (D-17).
    assert "/.well-known/jwks.json" not in paths
