"""The gateway problem responder renders RFC 9457 bodies for gateway-generated errors (D-18)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.unit

RESPONDER = Path(__file__).resolve().parents[2] / "gateway" / "problem-responder" / "responder.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gateway_problem_responder", RESPONDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_load().app)
    return httpx.AsyncClient(transport=transport, base_url="http://responder")


KNOWN = [
    (404, "not-found", "Not found"),
    (413, "payload-too-large", "Request body too large"),
    (429, "too-many-requests", "Too many requests"),
    (500, "internal-error", "Internal server error"),
    (502, "bad-gateway", "Bad gateway"),
    (503, "service-unavailable", "Service unavailable"),
    (504, "gateway-timeout", "Gateway timeout"),
]


def _assert_problem(response: httpx.Response, status: int, slug: str) -> dict[str, Any]:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    body: dict[str, Any] = response.json()
    assert body["type"] == f"urn:dc:problem:{slug}"
    assert isinstance(body["title"], str) and body["title"]
    assert body["status"] == status and isinstance(body["status"], int)
    assert isinstance(body["detail"], str) and body["detail"]
    return body


@pytest.mark.parametrize(("status", "slug", "title"), KNOWN)
async def test_known_statuses_render_their_slug_and_title(
    client: httpx.AsyncClient, status: int, slug: str, title: str
) -> None:
    body = await client.get(f"/{status}")
    problem = _assert_problem(body, status, slug)
    assert problem["title"] == title


@pytest.mark.parametrize("status", [400, 418, 451, 599])
async def test_other_error_statuses_render_gateway_error(
    client: httpx.AsyncClient, status: int
) -> None:
    problem = _assert_problem(await client.get(f"/{status}"), status, "gateway-error")
    assert problem["title"] == "Gateway error"


@pytest.mark.parametrize("path", ["/foo", "/4040", "/399", "/600", "/", "/40", "/404/extra"])
async def test_anything_else_is_a_404_not_found(client: httpx.AsyncClient, path: str) -> None:
    _assert_problem(await client.get(path), 404, "not-found")


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def test_any_http_method_works(client: httpx.AsyncClient, method: str) -> None:
    _assert_problem(await client.request(method, "/503"), 503, "service-unavailable")


async def test_healthz_is_a_plain_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_a_well_formed_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/429", headers={"X-Request-ID": "abc-123"})
    assert _assert_problem(response, 429, "too-many-requests")["request_id"] == "abc-123"


@pytest.mark.parametrize(
    "value", ["has space", "bad/char", "semi;colon", "x" * 129, "", "tab\there", "<script>"]
)
async def test_a_malformed_request_id_is_ignored(client: httpx.AsyncClient, value: str) -> None:
    response = await client.get("/500", headers={"X-Request-ID": value})
    assert "request_id" not in _assert_problem(response, 500, "internal-error")


async def test_the_longest_allowed_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    value = "a" * 128
    response = await client.get("/500", headers={"X-Request-ID": value})
    assert _assert_problem(response, 500, "internal-error")["request_id"] == value


async def test_the_body_never_reflects_path_query_or_other_headers(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/503?secret-query=zzz-query-value",
        headers={
            "Authorization": "Bearer zzz-token-value",
            "Cookie": "session=zzz-cookie-value",
            "X-Forwarded-For": "zzz-forwarded-value",
            "X-Request-ID": "rid-1",
        },
    )
    text = response.text
    for forbidden in ("zzz-", "secret-query", "Bearer", "/503", "session="):
        assert forbidden not in text
    assert set(json.loads(text)) == {"type", "title", "status", "detail", "request_id"}


async def test_unrouted_path_body_does_not_echo_the_path(client: httpx.AsyncClient) -> None:
    response = await client.get("/internal/v1/events?token=zzz-secret")
    _assert_problem(response, 404, "not-found")
    assert "internal" not in response.text and "zzz-secret" not in response.text


def test_responder_depends_on_the_standard_library_only() -> None:
    text = RESPONDER.read_text(encoding="utf-8")
    assert "fastapi" not in text
    assert "import dc_" not in text and "from dc_" not in text
