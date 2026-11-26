"""Gateway hardening at its boundaries (PLAT-03, PLAT-07, D-18, D-24; T-01-57, T-01-59).

Proves, through https://traefik: TLS 1.2+ with the dev CA, CORS answered by the gateway itself,
rate limits and the 2 MiB body cap at their exact thresholds (and under concurrency), that no
internal or traversal path ever reaches a service route, and that every error the gateway
generates for a well-formed request is RFC 9457 problem+json.

Requests that are not valid HTTP/URI syntax (malformed percent-encoding) are refused by Traefik
before routing with its own empty 400. No configuration can render that as problem+json: the
user accepted it as D-24, so those tests assert the status and that no service answered, never
the body.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import select
import socket
import ssl
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

pytestmark = pytest.mark.e2e

PROBLEM_JSON = "application/problem+json"
PROBLEM_PREFIX = "urn:dc:problem:"

# Thresholds owned elsewhere; the tests must follow them, so the sources are named here.
# GATEWAY_BODY_LIMIT: gateway/dynamic/middlewares.yml (body-limit, maxRequestBodyBytes).
GATEWAY_BODY_LIMIT = 2097152
# CONTRACTS_MAX_DOCUMENT_BYTES: the contracts service's DC_MAX_DOCUMENT_BYTES (default 1 MiB).
CONTRACTS_MAX_DOCUMENT_BYTES = 1048576
# Auth burst: gateway/dynamic/middlewares.yml (rl-auth: average 5/s, burst 10).
AUTH_BURST = 10

ALLOWED_ORIGIN = "http://localhost:5173"
FOLDERS = "/api/v1/identity/folders"
REFRESH = "/api/v1/identity/auth/refresh"
CONTRACTS = "/api/v1/contracts/contracts"


def _assert_problem(
    response: httpx.Response, status: int, problem_type: str | None = None
) -> dict[str, Any]:
    assert response.status_code == status, f"{response.request.url}: {response.text[:300]}"
    assert response.headers["content-type"] == PROBLEM_JSON
    body: dict[str, Any] = response.json()
    assert str(body["type"]).startswith(PROBLEM_PREFIX), body
    assert body["status"] == status
    if problem_type is not None:
        assert body["type"] == PROBLEM_PREFIX + problem_type
    return body


# --- raw requests: exact bytes on the request line, no client-side normalisation ------------


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def is_problem(self) -> bool:
        return self.headers.get("content-type") == PROBLEM_JSON

    def problem(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.body)
        return loaded


def _decode_chunked(data: bytes) -> bytes:
    out = bytearray()
    while data:
        size_line, _, rest = data.partition(b"\r\n")
        size = int(size_line.split(b";")[0] or b"0", 16)
        if size == 0:
            break
        out += rest[:size]
        data = rest[size + 2 :]
    return bytes(out)


def _parse_response(data: bytes) -> RawResponse:
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(":") for line in lines[1:])
    }
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _decode_chunked(body)
    return RawResponse(status, headers, body)


def _tls_context(cafile: str | None = None) -> ssl.SSLContext:
    """A verifying client context that refuses anything older than TLS 1.2, like the gateway."""
    context = ssl.create_default_context(cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _connect() -> tuple[ssl.SSLSocket, str]:
    url = urlparse(os.environ["DC_E2E_GATEWAY_URL"])
    host = url.hostname or "traefik"
    context = _tls_context(os.environ["DC_E2E_CA_FILE"])
    raw = socket.create_connection((host, url.port or 443), timeout=15)
    return context.wrap_socket(raw, server_hostname=host), host


def _raw_request_sync(target: bytes, *, method: str = "GET") -> RawResponse:
    """Send ``method target`` over TLS verbatim (HTTP/1.1, connection close) and parse the reply."""
    tls, host = _connect()
    request = (
        method.encode("ascii")
        + b" "
        + target
        + b" HTTP/1.1\r\nHost: "
        + host.encode("ascii")
        + b"\r\nConnection: close\r\nAccept: */*\r\n\r\n"
    )
    with tls:
        tls.sendall(request)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = tls.recv(65536)
            except ssl.SSLError, ConnectionResetError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    return _parse_response(b"".join(chunks))


def _post_reading_while_sending_sync(
    target: str, body: bytes, headers: dict[str, str]
) -> RawResponse:
    """POST ``body`` while reading the reply as it arrives.

    For a body above the gateway cap Traefik answers 413 after the first few hundred KiB and then
    resets the connection while the client is still sending. A client that finishes writing before
    it reads (httpx, curl without --expect100) can lose that response to the reset, so the 413
    proofs read and write concurrently, exactly as a streaming client does.
    """
    tls, host = _connect()
    lines = [f"POST {target} HTTP/1.1", f"Host: {host}", f"Content-Length: {len(body)}"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    lines += ["Connection: close", "", ""]
    pending = "\r\n".join(lines).encode("latin-1") + body
    received = bytearray()
    with tls:
        tls.setblocking(False)
        sent = 0
        deadline = time.monotonic() + 30
        closed = False
        while time.monotonic() < deadline and not closed:
            readable, writable, _ = select.select(
                [tls], [tls] if sent < len(pending) else [], [], 1
            )
            if readable:
                try:
                    chunk = tls.recv(65536)
                except ssl.SSLWantReadError:
                    chunk = None
                except ssl.SSLError, ConnectionResetError:
                    break
                if chunk == b"":
                    closed = True
                elif chunk:
                    received += chunk
            if writable and sent < len(pending):
                try:
                    sent += tls.send(pending[sent : sent + 65536])
                except ssl.SSLWantWriteError:
                    pass
                except ssl.SSLError, ConnectionResetError, BrokenPipeError:
                    sent = len(pending)  # peer reset: stop writing, keep draining what arrived
            if sent >= len(pending) and b"\r\n\r\n" in received and not readable:
                break
    assert received, "no response bytes arrived"
    return _parse_response(bytes(received))


async def _raw(target: bytes, *, method: str = "GET") -> RawResponse:
    return await asyncio.to_thread(_raw_request_sync, target, method=method)


# --- TLS ----------------------------------------------------------------------------------


def test_tls_handshake_with_the_dev_ca_negotiates_tls_1_2_or_newer() -> None:
    url = urlparse(os.environ["DC_E2E_GATEWAY_URL"])
    host = url.hostname or "traefik"
    context = _tls_context(os.environ["DC_E2E_CA_FILE"])
    with socket.create_connection((host, url.port or 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            assert tls.version() in {"TLSv1.2", "TLSv1.3"}
            certificate = tls.getpeercert()
    assert certificate is not None


def test_tls_handshake_works_for_a_client_limited_to_tls_1_2() -> None:
    url = urlparse(os.environ["DC_E2E_GATEWAY_URL"])
    host = url.hostname or "traefik"
    context = _tls_context(os.environ["DC_E2E_CA_FILE"])
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((host, url.port or 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            assert tls.version() == "TLSv1.2"


def test_tls_verification_fails_for_a_client_that_does_not_trust_the_dev_ca() -> None:
    url = urlparse(os.environ["DC_E2E_GATEWAY_URL"])
    host = url.hostname or "traefik"
    context = _tls_context()  # system roots only: the dev CA is not among them
    with (
        socket.create_connection((host, url.port or 443), timeout=10) as raw,
        pytest.raises(ssl.SSLCertVerificationError),
    ):
        context.wrap_socket(raw, server_hostname=host)


# --- CORS ---------------------------------------------------------------------------------


async def test_cors_preflight_is_answered_by_the_gateway_for_the_allowed_origin(
    gateway: httpx.AsyncClient,
) -> None:
    response = await gateway.request(
        "OPTIONS",
        CONTRACTS,
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    # Services have no OPTIONS handler: a preflight that reached one would be a 405 problem.
    assert 200 <= response.status_code < 300, f"{response.status_code} {response.text[:200]}"
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    methods = {
        m.strip().upper() for m in response.headers["access-control-allow-methods"].split(",")
    }
    assert {"GET", "POST", "PATCH", "PUT", "DELETE"} <= methods
    allowed_headers = {
        h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")
    }
    assert "authorization" in allowed_headers


async def test_cors_preflight_for_put_and_delete_is_answered_by_the_gateway(
    gateway: httpx.AsyncClient,
) -> None:
    for method in ("PUT", "DELETE"):
        response = await gateway.request(
            "OPTIONS",
            CONTRACTS,
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert 200 <= response.status_code < 300, f"{response.status_code} {response.text[:200]}"
        assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        methods = {
            m.strip().upper() for m in response.headers["access-control-allow-methods"].split(",")
        }
        assert method in methods


async def test_cors_preflight_may_send_the_conditional_request_headers(
    gateway: httpx.AsyncClient,
) -> None:
    """If-Match (draft writes) and If-None-Match (consumer revalidation) cross the preflight."""
    response = await gateway.request(
        "OPTIONS",
        CONTRACTS,
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization, if-match, if-none-match",
        },
    )
    assert 200 <= response.status_code < 300, f"{response.status_code} {response.text[:200]}"
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    allowed_headers = {
        h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")
    }
    assert {"authorization", "if-match", "if-none-match"} <= allowed_headers


async def test_cors_preflight_from_another_origin_gets_no_allow_origin(
    gateway: httpx.AsyncClient,
) -> None:
    for origin in ("https://evil.example", "http://localhost:5174", "null"):
        response = await gateway.request(
            "OPTIONS",
            CONTRACTS,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert "access-control-allow-origin" not in response.headers, origin


async def test_cors_actual_request_from_the_allowed_origin_carries_allow_origin_and_vary(
    gateway: httpx.AsyncClient, admin_token: str
) -> None:
    response = await gateway.get(
        CONTRACTS,
        headers={"Origin": ALLOWED_ORIGIN, "Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "origin" in response.headers["vary"].lower()


async def test_cors_actual_request_from_another_origin_gets_no_allow_origin(
    gateway: httpx.AsyncClient, admin_token: str
) -> None:
    response = await gateway.get(
        CONTRACTS,
        headers={"Origin": "https://evil.example", "Authorization": f"Bearer {admin_token}"},
    )
    assert "access-control-allow-origin" not in response.headers


async def test_cors_headers_also_land_on_gateway_generated_problems(
    gateway: httpx.AsyncClient,
) -> None:
    """cors is the outermost middleware, so a browser can read a re-rendered error."""
    response = await gateway.get("/internal/no/such/path", headers={"Origin": ALLOWED_ORIGIN})
    _assert_problem(response, 404, "not-found")
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


# --- body cap (boundary) --------------------------------------------------------------------


def _folder_body_of_size(parent_id: str, size: int) -> bytes:
    """A well-formed folder-create JSON body of exactly ``size`` bytes (oversized display_name)."""
    base = json.dumps({"parent_id": parent_id, "slug": "pad", "display_name": ""}).encode()
    body = json.dumps(
        {"parent_id": parent_id, "slug": "pad", "display_name": "a" * (size - len(base))}
    ).encode()
    assert len(body) == size
    return body


async def _post_oversized(path: str, content: bytes, headers: dict[str, str]) -> httpx.Response:
    """POST a body the gateway (or service) rejects, returned as an httpx.Response."""
    reply = await asyncio.to_thread(_post_reading_while_sending_sync, path, content, headers)
    return httpx.Response(
        reply.status,
        headers=reply.headers,
        content=reply.body,
        request=httpx.Request("POST", os.environ["DC_E2E_GATEWAY_URL"] + path),
    )


async def _root_id(gateway: httpx.AsyncClient, headers: dict[str, str]) -> str:
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=headers)
    assert roots.status_code == 200, roots.text
    return str(roots.json()["items"][0]["id"])


async def test_boundary_body_of_exactly_the_gateway_limit_passes_and_one_byte_more_is_413(
    gateway: httpx.AsyncClient, admin_token: str
) -> None:
    headers = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}
    root = await _root_id(gateway, headers)

    at_limit = await gateway.post(
        FOLDERS, content=_folder_body_of_size(root, GATEWAY_BODY_LIMIT), headers=headers
    )
    # Identity answers with its own problem (display_name is capped at 200): it passed the gateway.
    _assert_problem(at_limit, 422, "validation-error")

    over_limit = await _post_oversized(
        FOLDERS, _folder_body_of_size(root, GATEWAY_BODY_LIMIT + 1), headers
    )
    _assert_problem(over_limit, 413, "payload-too-large")


async def test_boundary_contract_above_the_service_cap_is_413_problem_json(
    gateway: httpx.AsyncClient, admin_token: str
) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}
    parent = await _root_id(gateway, headers)
    run = uuid.uuid4().hex[:10]
    for slug in (f"gw-{run}", "payments", "ingest"):
        created = await gateway.post(
            FOLDERS,
            json={"parent_id": parent, "slug": slug, "display_name": slug},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        parent = str(created.json()["id"])
    response = await _post_oversized(
        f"/api/v1/contracts/folders/{parent}/contracts",
        b"#" * (CONTRACTS_MAX_DOCUMENT_BYTES + 1),
        {**headers, "Content-Type": "application/yaml"},
    )
    # The service's own 413, re-rendered by the gateway's errors middleware.
    _assert_problem(response, 413, "payload-too-large")


# --- rate limits ----------------------------------------------------------------------------


def _random_refresh_body() -> dict[str, str]:
    return {"refresh_token": uuid.uuid4().hex}


async def _let_the_auth_bucket_refill() -> None:
    # rl-auth refills 5 tokens per second up to the burst of 10.
    await asyncio.sleep(3.0)


async def test_boundary_requests_within_the_auth_burst_are_served(
    gateway: httpx.AsyncClient,
) -> None:
    await _let_the_auth_bucket_refill()
    for _ in range(AUTH_BURST - 4):
        response = await gateway.post(REFRESH, json=_random_refresh_body())
        _assert_problem(response, 401)  # identity's answer for an unknown refresh token


async def test_a_login_refresh_token_is_rotated_through_the_gateway(
    gateway: httpx.AsyncClient, admin_password: str
) -> None:
    """A real refresh round trip: the coverage gate counts only a 2xx answer (WR-05), and every
    other refresh call in this suite is a rate-limit probe with an unknown token (401 or 429)."""
    await _let_the_auth_bucket_refill()
    login = await gateway.post(
        "/api/v1/identity/auth/login", json={"username": "admin", "password": admin_password}
    )
    assert login.status_code == 200, login.text
    first = login.json()["refresh_token"]
    assert first
    refreshed = await gateway.post(REFRESH, json={"refresh_token": first})
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["access_token"]
    assert body["refresh_token"] and body["refresh_token"] != first


async def test_concurrency_thirty_parallel_auth_requests_are_limited_in_aggregate(
    gateway: httpx.AsyncClient,
) -> None:
    await _let_the_auth_bucket_refill()
    responses = await asyncio.gather(
        *(gateway.post(REFRESH, json=_random_refresh_body()) for _ in range(30))
    )
    statuses = sorted(response.status_code for response in responses)
    assert set(statuses) <= {401, 429}, statuses
    assert 429 in statuses, f"parallelism bypassed the limiter: {statuses}"
    assert 401 in statuses, f"nothing reached identity: {statuses}"
    for response in responses:
        # Every response, limited or not, is problem+json.
        assert response.headers["content-type"] == PROBLEM_JSON
        if response.status_code == 429:
            _assert_problem(response, 429, "too-many-requests")
        else:
            _assert_problem(response, 401)
    await _let_the_auth_bucket_refill()  # leave the limiter usable for whatever runs next


# --- D-18: every gateway-generated error is problem+json ------------------------------------


async def test_problem_json_for_gateway_404_413_429_and_5xx(
    gateway: httpx.AsyncClient, admin_token: str, compose: Any, wait_for: Callable[..., Any]
) -> None:
    headers = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}

    # 404: nothing routes this path (/internal is never served; an unknown SPA path is the UI).
    not_found = await gateway.get("/internal/no/such/path")
    body = _assert_problem(not_found, 404, "not-found")
    assert body["title"]

    # 413: body above the gateway cap.
    root = await _root_id(gateway, headers)
    too_large = await _post_oversized(
        FOLDERS, _folder_body_of_size(root, GATEWAY_BODY_LIMIT + 1), headers
    )
    _assert_problem(too_large, 413, "payload-too-large")

    # The client's own X-Request-ID comes back as request_id on a re-rendered error.
    request_id = f"e2e-{uuid.uuid4().hex[:12]}"
    traced = await _post_oversized(
        FOLDERS,
        _folder_body_of_size(root, GATEWAY_BODY_LIMIT + 1),
        {**headers, "X-Request-ID": request_id},
    )
    assert _assert_problem(traced, 413)["request_id"] == request_id

    # 429: exhaust the credential limiter.
    await _let_the_auth_bucket_refill()
    limited = [
        response
        for response in await asyncio.gather(
            *(gateway.post(REFRESH, json=_random_refresh_body()) for _ in range(40))
        )
        if response.status_code == 429
    ]
    assert limited, "the auth rate limiter never answered 429"
    _assert_problem(limited[0], 429, "too-many-requests")
    await _let_the_auth_bucket_refill()

    # 5xx: an upstream that is down. The container is started again whatever happens.
    compose.stop("validation")
    try:
        upstream_down = await gateway.get("/api/v1/validation/anything")
        assert upstream_down.status_code in {502, 503, 504}, upstream_down.status_code
        _assert_problem(upstream_down, upstream_down.status_code)
    finally:
        compose.start("validation")
        await wait_for(
            lambda: compose.health("validation") == "healthy",
            90.0,
            description="the validation service to be healthy again",
        )


# --- D-UI-08-08: HSTS is outermost on every router, so gateway-made errors carry it too ----

HSTS = "max-age=31536000; includeSubDomains"


async def test_hsts_is_on_api_docs_ui_and_every_gateway_generated_problem(
    gateway: httpx.AsyncClient, admin_token: str
) -> None:
    headers = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}
    root = await _root_id(gateway, headers)

    api = await gateway.get(FOLDERS, params={"limit": 1}, headers=headers)
    assert api.status_code == 200
    docs = await gateway.get(f"{DOCS}/")
    assert docs.status_code == 200
    ui = await gateway.get("/")
    assert ui.status_code == 200
    not_found = await gateway.get("/internal/no/such/path")
    _assert_problem(not_found, 404, "not-found")
    too_large = await _post_oversized(
        FOLDERS, _folder_body_of_size(root, GATEWAY_BODY_LIMIT + 1), headers
    )
    _assert_problem(too_large, 413, "payload-too-large")

    for label, response in {
        "api 200": api,
        "docs index": docs,
        "ui root": ui,
        "catch-all 404": not_found,
        "body-limit 413": too_large,
    }.items():
        assert response.headers.get("strict-transport-security") == HSTS, label


# --- unreachable paths ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/internal/v1/authz/check",
        "/internal/v1/authz/accessible-folders",
        "/internal/v1/events",
        "/healthz",
        "/readyz",
        "/.well-known/jwks.json",
        "/api/v1/identity-evil/x",
        "/api/v1/contracts-evil/contracts",
        "/api/v1/identity",
        "/api/v1/unknown-module/x",
    ],
)
async def test_paths_no_router_serves_are_404_problem_json_from_the_catch_all(
    gateway: httpx.AsyncClient, path: str
) -> None:
    _assert_problem(await gateway.get(path), 404, "not-found")
    _assert_problem(await gateway.post(path, json={}), 404, "not-found")


# --- UI-04: the web UI is served by its own container behind the gateway (D-UI-08-06) --------


@pytest.mark.parametrize("path", ["/", "/signin", "/contracts/abc"])
async def test_ui_serves_the_single_page_app_for_get_on_every_spa_path(
    gateway: httpx.AsyncClient, path: str
) -> None:
    response = await gateway.get(path)
    assert response.status_code == 200, f"{path}: {response.text[:300]}"
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="root"' in response.text


async def test_ui_assets_have_their_own_bucket_while_the_api_still_limits(
    gateway: httpx.AsyncClient,
) -> None:
    """A cold page load is dozens of static files at once: the ui router's rl-ui (300/s, burst
    600) serves 150 rapid requests, while a router on rl-default (50/s, burst 100) still
    answers 429 to the same volume (gateway/dynamic/middlewares.yml)."""
    index = await gateway.get("/")
    assert index.status_code == 200
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.text)
    assert assets, index.text[:300]
    paths = ["/", "/config.json", *assets]
    ui = await asyncio.gather(*(gateway.get(paths[i % len(paths)]) for i in range(150)))
    assert {response.status_code for response in ui} == {200}

    limited = await asyncio.gather(*(gateway.get("/internal/rl-probe") for _ in range(200)))
    statuses = {response.status_code for response in limited}
    assert 429 in statuses, f"rl-default no longer limits: {statuses}"
    assert statuses <= {404, 429}, statuses
    await asyncio.sleep(3.0)  # let rl-default refill for whatever runs next


async def test_ui_head_on_the_root_is_200(gateway: httpx.AsyncClient) -> None:
    response = await gateway.head("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_ui_runtime_config_is_public_json_with_the_compose_values(
    gateway: httpx.AsyncClient,
) -> None:
    response = await gateway.get("/config.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    config: dict[str, Any] = response.json()
    assert config["localLoginEnabled"] is True
    assert config["entraEnabled"] is True
    assert config["apiBase"] == "/api/v1"
    assert not [key for key in config if "secret" in key.lower()]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/signin"),
        ("PUT", "/"),
        ("GET", "/internal/v1/events"),
        ("GET", "/api"),
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("GET", "/.well-known/jwks.json"),
    ],
)
async def test_ui_router_never_answers_a_non_get_or_a_reserved_path(
    gateway: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await gateway.request(method, path)
    _assert_problem(response, 404, "not-found")


# The compose ui service enables Entra against the dev stand-in, so the authority's origin joins
# connect-src, frame-src and form-action (D-UI-08-07).
UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;"
    " font-src 'self'; connect-src 'self' https://localhost:8443;"
    " frame-src 'self' https://localhost:8443; worker-src 'self' blob:;"
    " base-uri 'none'; form-action 'self' https://localhost:8443; object-src 'none';"
    " frame-ancestors 'none'"
)


async def test_ui_headers_are_on_every_location_404s_included(
    gateway: httpx.AsyncClient,
) -> None:
    """nginx owns the UI security headers (D-UI-08-07); each location must carry the whole set."""
    index = await gateway.get("/")
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.text)
    assert assets, index.text
    asset = assets[0]
    missing = "/assets/missing-0000.js"

    responses = {
        "/": index,
        "/signin": await gateway.get("/signin"),
        asset: await gateway.get(asset),
        "/config.json": await gateway.get("/config.json"),
        "/healthz.txt": await gateway.get("/healthz.txt"),
        missing: await gateway.get(missing),
    }
    for path, response in responses.items():
        headers = response.headers
        assert headers["content-security-policy"] == UI_CSP, path
        assert headers["x-content-type-options"] == "nosniff", path
        assert headers["referrer-policy"] == "no-referrer", path
        assert headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()", path
        assert headers["cross-origin-opener-policy"] == "same-origin", path
        assert headers["strict-transport-security"] == HSTS, path

    assert responses[asset].status_code == 200
    assert responses[asset].headers["cache-control"] == "public, max-age=31536000, immutable"
    # A missing hashed asset is a real 404 and must never be cached for a year (A-8).
    assert responses[missing].status_code == 404
    assert "immutable" not in responses[missing].headers.get("cache-control", "")
    for path in ("/", "/signin", "/config.json", "/healthz.txt"):
        assert responses[path].headers["cache-control"] == "no-store", path


async def test_ui_entra_config_is_published_and_the_csp_allows_the_authority_origin(
    gateway: httpx.AsyncClient,
) -> None:
    """Plan 08-12: the running UI knows the compose stand-in (D-UI-08-05, D-UI-08-07)."""
    config: dict[str, Any] = (await gateway.get("/config.json")).json()
    assert config["entraEnabled"] is True
    assert config["entraClientId"] == "ui-alice"
    assert config["entraAuthority"] == "https://localhost:8443/entra"
    assert config["entraProtocol"] == "oidc"
    # The entrypoint publishes the scopes as one space-separated string; the SPA splits it.
    assert config["entraScopes"] == "openid profile offline_access"
    root = await gateway.get("/")
    directives = root.headers["content-security-policy"].split(";")
    csp = {name: value for name, _, value in (part.strip().partition(" ") for part in directives)}
    assert "https://localhost:8443" in csp["connect-src"].split()


async def test_ui_router_leaves_an_api_route_with_its_own_service(
    gateway: httpx.AsyncClient,
) -> None:
    response = await gateway.get(MERGED_OPENAPI)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json()["info"]["title"] == "Contractum API"


async def test_a_raw_dot_segment_traversal_into_internal_is_a_404_problem() -> None:
    """Sanitized to /internal/v1/authz/check, which no router serves: the catch-all answers."""
    for target in (
        b"/api/v1/identity/../../internal/v1/authz/check",
        b"/api/v1/contracts/./../../internal/v1/events",
        b"/api/v1/identity/folders/../../../../internal/v1/events",
    ):
        reply = await _raw(target)
        assert reply.status == 404, (target, reply.status, reply.body[:200])
        assert reply.is_problem, (target, reply.headers)
        assert reply.problem()["type"] == PROBLEM_PREFIX + "not-found"


@pytest.mark.parametrize(
    "target",
    [
        b"/api/v1/identity%2F..%2Finternal/v1/authz/check",
        b"/api/v1/identity/%2e%2e/%2e%2e/internal/v1/events",
        b"/api/v1/identity%5C..%5Cinternal/v1/events",
        b"/api/v1/identity/folders%00",
        b"/api/v1/identity/folders%3B/x",
        b"/api/v1/contracts/contracts%2F..%2F..%2Finternal/v1/events",
    ],
)
async def test_encoded_traversal_never_reaches_a_service_route(target: bytes) -> None:
    """The service guard answers 400 invalid-path or the catch-all answers 404; never 2xx/401."""
    reply = await _raw(target)
    assert reply.status in {400, 404}, (target, reply.status, reply.body[:200])
    if reply.status == 404:
        assert reply.is_problem, (target, reply.headers)
        assert reply.problem()["type"] == PROBLEM_PREFIX + "not-found"
    elif reply.is_problem:
        assert reply.problem()["type"] == PROBLEM_PREFIX + "invalid-path"


@pytest.mark.parametrize(
    "target", [b"/api/v1/identity/%zz", b"/api/v1/contracts/contracts/%", b"/%zz"]
)
async def test_malformed_percent_encoding_is_refused_before_routing(target: bytes) -> None:
    """D-24: not valid URI syntax. Traefik refuses it with its own 400 before any router runs;
    the body is deliberately not asserted (no configuration can make it problem+json)."""
    reply = await _raw(target)
    assert reply.status == 400, (target, reply.status)


# --- PLAT-06: the merged OpenAPI document and Swagger UI (D-176..D-178) ---------------------

MERGED_OPENAPI = "/api/v1/openapi.json"


async def test_openapi_merged_document_is_served_through_the_gateway(
    gateway: httpx.AsyncClient,
) -> None:
    response = await gateway.get(MERGED_OPENAPI)
    assert response.status_code == 200, response.text[:300]
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"
    doc: dict[str, Any] = response.json()
    assert doc["openapi"] == "3.1.0"
    assert doc["servers"] == [{"url": "/"}]
    assert doc["info"] == {
        "title": "Contractum API",
        "description": "Contractum: data contract governance",
        "version": "v1",
    }
    assert doc["components"]["securitySchemes"] == {
        "bearerAuth": {"type": "http", "scheme": "bearer"}
    }
    paths: list[str] = list(doc["paths"])
    # Audit has no public route until its query API lands; the other three always do.
    for module in ("identity", "contracts", "validation"):
        assert any(p.startswith(f"/api/v1/{module}/") for p in paths), module
    assert all(
        p.startswith(
            tuple(f"/api/v1/{m}/" for m in ("identity", "contracts", "validation", "audit"))
        )
        for p in paths
    )
    assert not [p for p in paths if "/internal" in p]


async def test_openapi_merged_document_is_read_only(gateway: httpx.AsyncClient) -> None:
    _assert_problem(await gateway.post(MERGED_OPENAPI, json={}), 405, "method-not-allowed")


DOCS = "/api/v1/docs"


def _assert_docs_headers(response: httpx.Response) -> None:
    csp = response.headers["content-security-policy"]
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
    ):
        assert directive in csp, csp
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_docs_bare_path_redirects_permanently_to_the_slash_form(
    gateway: httpx.AsyncClient,
) -> None:
    response = await gateway.get(DOCS, follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == DOCS + "/"


async def test_docs_index_is_the_vendored_swagger_ui_with_a_same_origin_csp(
    gateway: httpx.AsyncClient,
) -> None:
    response = await gateway.get(DOCS + "/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    _assert_docs_headers(response)
    assert "swagger-ui-bundle.js" in response.text
    assert "<script>" not in response.text  # no inline script: script-src 'self' holds


async def test_every_component_answers_its_version_through_the_gateway(
    gateway: httpx.AsyncClient,
) -> None:
    # The UI's "Versions" dialog reads these: one shape, public, the same platform version.
    versions = set()
    for module in ("identity", "contracts", "validation", "audit", "docs"):
        response = await gateway.get(f"/api/v1/{module}/version")
        assert response.status_code == 200, (module, response.text)
        body = response.json()
        assert set(body) == {"service", "version", "commit", "built_at"}, module
        assert response.headers["cache-control"] == "public, max-age=60", module
        versions.add(body["version"])
    assert len(versions) == 1, versions


async def test_docs_assets_and_the_initializer_are_served_same_origin_only(
    gateway: httpx.AsyncClient,
) -> None:
    for asset in (
        "swagger-ui-bundle.js",
        "swagger-ui-standalone-preset.js",
        "swagger-ui.css",
        "index.css",
        "favicon-32x32.png",
    ):
        response = await gateway.get(f"{DOCS}/{asset}")
        assert response.status_code == 200, asset
        _assert_docs_headers(response)
    initializer = await gateway.get(f"{DOCS}/swagger-initializer.js")
    assert initializer.status_code == 200
    assert initializer.headers["content-type"].startswith("text/javascript")
    text = initializer.text
    assert MERGED_OPENAPI in text
    assert 'validatorUrl: "none"' in text
    assert "http://" not in text
    assert "https://" not in text
    assert "petstore" not in text
    for module in ("identity", "contracts", "validation", "audit"):
        assert f"/api/v1/{module}/openapi.json" in text, module


@pytest.mark.parametrize(
    "path", [f"{DOCS}/package.json", f"{DOCS}/nope.js", f"{DOCS}/swagger-ui.js.map"]
)
async def test_docs_files_outside_the_allow_list_are_404_problem_json(
    gateway: httpx.AsyncClient, path: str
) -> None:
    _assert_problem(await gateway.get(path), 404, "not-found")


async def test_docs_dot_segments_never_reach_a_file_outside_the_allow_list() -> None:
    for target in (b"/api/v1/docs/../server.py", b"/api/v1/docs/%2e%2e/server.py"):
        reply = await _raw(target)
        assert reply.status in {400, 404}, (target, reply.status, reply.body[:200])
        assert b"merge_specs" not in reply.body
        assert b"ThreadingHTTPServer" not in reply.body


async def test_docs_lookalike_path_stays_on_the_catch_all(gateway: httpx.AsyncClient) -> None:
    response = await gateway.get("/api/v1/docsevil")
    _assert_problem(response, 404, "not-found")
    assert "content-security-policy" not in response.headers  # not answered by api-docs
