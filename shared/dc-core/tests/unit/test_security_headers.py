"""Every service response carries ``nosniff`` and, by default, ``Cache-Control: no-store``.

ZAP rule 10021 found nosniff missing on identity's JSON answers (quick 261120-pn7); the audit's
cache-control follow-up (quick 261120-s0p) makes ``no-store`` the default for JSON and problem
answers. Both are set by the service itself, not the gateway: no security control may live only in
the replaceable gateway. A handler's own ``Cache-Control`` wins, and the service's own
``openapi.json`` (a public document) is left cacheable.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from dc_core.app import create_app
from dc_core.policy import PolicyRouter, public
from dc_core.settings import CoreSettings
from fastapi import FastAPI, Response

pytestmark = pytest.mark.unit


def _cache_router() -> PolicyRouter:
    router = PolicyRouter(prefix="/api/v1/core")

    async def revalidated(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "private, no-cache"
        return {"kind": "revalidated"}

    async def plain() -> dict[str, str]:
        return {"kind": "plain"}

    async def own_sniff(response: Response) -> dict[str, str]:
        response.headers["X-Content-Type-Options"] = "nosniff"
        return {"kind": "own"}

    router.add_api_route("/revalidated", revalidated, methods=["GET"], dependencies=[public()])
    router.add_api_route("/plain", plain, methods=["GET"], dependencies=[public()])
    router.add_api_route("/own-sniff", own_sniff, methods=["GET"], dependencies=[public()])
    return router


def _app() -> FastAPI:
    return create_app(
        service="core",
        settings=CoreSettings(service="core", database_url="postgresql+psycopg://x/y"),
        routers=[_cache_router()],
        version_route=True,
    )


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path)


async def test_success_response_has_nosniff() -> None:
    response = await _get("/api/v1/core/version")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_problem_responses_have_nosniff() -> None:
    missing = await _get("/no/such/route")
    assert missing.status_code == 404
    assert missing.headers["x-content-type-options"] == "nosniff"
    refused = await _get("/a%2fb")
    assert refused.status_code == 400
    assert refused.headers["x-content-type-options"] == "nosniff"
    assert list(refused.headers.get_list("x-content-type-options")) == ["nosniff"]


async def test_a_json_success_response_is_no_store() -> None:
    response = await _get("/api/v1/core/plain")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert list(response.headers.get_list("cache-control")) == ["no-store"]


async def test_problem_responses_are_no_store_once() -> None:
    missing = await _get("/no/such/route")
    assert missing.status_code == 404
    assert list(missing.headers.get_list("cache-control")) == ["no-store"]
    refused = await _get("/a%2fb")
    assert refused.status_code == 400
    assert list(refused.headers.get_list("cache-control")) == ["no-store"]


async def test_a_route_that_sets_its_own_cache_control_keeps_it() -> None:
    version = await _get("/api/v1/core/version")
    assert version.headers["cache-control"] == "public, max-age=60"
    revalidated = await _get("/api/v1/core/revalidated")
    assert revalidated.headers["cache-control"] == "private, no-cache"
    assert list(revalidated.headers.get_list("cache-control")) == ["private, no-cache"]


async def test_the_services_own_openapi_document_is_left_cacheable() -> None:
    response = await _get("/api/v1/core/openapi.json")
    assert response.status_code == 200
    assert "cache-control" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_a_handlers_own_nosniff_is_not_doubled() -> None:
    response = await _get("/api/v1/core/own-sniff")
    assert response.status_code == 200
    assert list(response.headers.get_list("x-content-type-options")) == ["nosniff"]
    assert response.headers["cache-control"] == "no-store"


async def test_a_lifespan_scope_passes_through_the_middlewares_untouched() -> None:
    incoming = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return incoming.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await _app()({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send)
    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
