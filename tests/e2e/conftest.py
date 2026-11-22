"""Fixtures shared by every end-to-end test (runs in a container on the compose networks).

Module-level imports are limited to the standard library, httpx and pytest: every file or
environment read happens inside a fixture, so host-side collection (`uv run pytest -m unit`)
never fails. The e2e tests run on one session-scoped event loop because the HTTP clients are
session-scoped; the runner passes ``-o asyncio_default_test_loop_scope=session`` (scripts/e2e.sh
and the image's default command), so no test needs a per-test marker.

Fixtures: ``gateway``, ``internal``, ``operation_registry``, ``admin_password``, ``admin_token``,
``users``, ``non_admin_token``, ``entra_token``, ``fake_graph``, ``mailpit``, ``webhook_sink``,
``compose`` and the ``wait_for`` helper.

``operation_registry`` (D-181 part 3) is the set of ``(method, path)`` of every request sent through
the ``gateway`` and ``internal`` clients, filled by an httpx request event hook. It keeps nothing
else: no header, token, query string or body. ``test_zz_operation_coverage.py`` (collected last)
matches it to the merged OpenAPI document.
``/dev-out`` is the host's ``./.dev`` (shared with ``scripts/skeleton-smoke.sh``);
``/run/secrets`` holds the CA certificate and the bootstrap secret.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import secrets
import ssl
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from docker.models.containers import Container

ADMIN_USERNAME = "admin"
LOGIN_PATH = "/api/v1/identity/auth/login"
ROTATE_PATH = "/api/v1/identity/auth/password"
ADMIN_TOKEN_MAX_AGE_S = 600.0  # access tokens live 15 minutes; refresh well before that
DEV_OUT = Path("/dev-out")
SECRETS = Path("/run/secrets")
# The Entra tenant, API audience and personas seeded by infra/mock-oidc/config.json and
# infra/fake-graph/directory.json (Plan 04-07). A persona's value is its Entra object id.
ENTRA_TENANT = "11111111-2222-4333-8444-555555555555"
ENTRA_API_AUDIENCE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ENTRA_DATA_ENG_GROUP = "9a0f0000-0000-4000-8000-0000000000a1"
# The browser-visible origin of the compose Entra stand-in (gateway dev-only router). Sent as
# Host and X-Forwarded-Proto on in-network token calls so the mock's `iss` matches the one entry
# every service trusts.
ENTRA_ISSUER_HEADERS = {"Host": "localhost:8443", "X-Forwarded-Proto": "https"}
ENTRA_PERSONAS = {
    "alice": "a11ce000-0000-4000-8000-000000000001",
    "bob": "b0b00000-0000-4000-8000-000000000002",
    "carol": "ca201000-0000-4000-8000-000000000003",
    "pipeline": "5e2b1ce0-0000-4000-8000-000000000004",
    "alice-idtoken": "a11ce000-0000-4000-8000-000000000001",
    "alice-wrong-aud": "a11ce000-0000-4000-8000-000000000001",
}


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set; e2e tests run through scripts/e2e.sh")
    return value


async def wait_for[T](
    predicate: Callable[[], T | Awaitable[T]],
    timeout_s: float = 30.0,
    *,
    interval_s: float = 0.5,
    description: str = "condition",
) -> T:
    """Poll ``predicate`` (sync or async) until it returns a truthy value; raise on timeout."""
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while True:
        try:
            result = predicate()
            if inspect.isawaitable(result):
                result = await result
            if result:
                return result  # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001 - keep polling, report the last one on timeout
            last_error = exc
        if time.monotonic() >= deadline:
            detail = f" (last error: {last_error!r})" if last_error else ""
            raise AssertionError(f"timed out after {timeout_s}s waiting for {description}{detail}")
        await asyncio.sleep(interval_s)


@pytest.fixture(name="wait_for")
def wait_for_fixture() -> Callable[..., Awaitable[Any]]:
    return wait_for


OperationRegistry = set[tuple[str, str]]


@pytest.fixture(scope="session")
def operation_registry() -> OperationRegistry:
    """``(method, path)`` of every request the ``gateway`` and ``internal`` clients sent that was
    answered below 400 (2xx or 3xx): the coverage evidence of D-181(3) (WR-05)."""
    return set()


@pytest.fixture(scope="session")
def operation_refusals() -> OperationRegistry:
    """``(method, path)`` of every request answered 400 or above: reported, never coverage."""
    return set()


def _recorder(
    registry: OperationRegistry, refusals: OperationRegistry
) -> Callable[[httpx.Response], Awaitable[None]]:
    async def record(response: httpx.Response) -> None:
        # WR-05: a response hook, so the status is known. A 401, a 404 probe, a 422 or a 503
        # proves only that the route refuses; an operation counts as covered once some e2e test
        # got a 2xx or 3xx from it. Only the method and the decoded path are kept: never headers,
        # tokens, the query or the body.
        request = response.request
        target = registry if response.status_code < 400 else refusals
        target.add((request.method, request.url.path))

    return record


@pytest_asyncio.fixture(scope="session")
async def gateway(
    operation_registry: OperationRegistry, operation_refusals: OperationRegistry
) -> AsyncIterator[httpx.AsyncClient]:
    """Client for the public entry point, verifying TLS with the dev CA."""
    async with httpx.AsyncClient(
        base_url=_env("DC_E2E_GATEWAY_URL"),
        verify=ssl.create_default_context(cafile=_env("DC_E2E_CA_FILE")),
        timeout=httpx.Timeout(15.0),
        event_hooks={"response": [_recorder(operation_registry, operation_refusals)]},
    ) as client:
        yield client


@pytest_asyncio.fixture(scope="session")
async def internal(
    operation_registry: OperationRegistry, operation_refusals: OperationRegistry
) -> AsyncIterator[Callable[[str], httpx.AsyncClient]]:
    """``internal("audit")`` is a client for ``http://audit:8000`` (no gateway in between)."""
    clients: dict[str, httpx.AsyncClient] = {}

    def factory(service: str) -> httpx.AsyncClient:
        if service not in clients:
            clients[service] = httpx.AsyncClient(
                base_url=f"http://{service}:8000",
                timeout=httpx.Timeout(15.0),
                event_hooks={"response": [_recorder(operation_registry, operation_refusals)]},
            )
        return clients[service]

    yield factory
    for client in clients.values():
        await client.aclose()


async def _login(gateway: httpx.AsyncClient, password: str) -> httpx.Response:
    return await gateway.post(LOGIN_PATH, json={"username": ADMIN_USERNAME, "password": password})


@pytest_asyncio.fixture(scope="session")
async def admin_password(gateway: httpx.AsyncClient) -> str:
    """The admin's rotated password, bootstrapping and rotating when none works yet.

    Shared with ``scripts/skeleton-smoke.sh`` through ``.dev/admin_password``.
    """
    stored = DEV_OUT / "admin_password"
    if stored.is_file():
        candidate = stored.read_text(encoding="utf-8").strip()
        if candidate and (await _login(gateway, candidate)).status_code == 200:
            return candidate
        stored.unlink()
    secret = (SECRETS / "bootstrap_admin_secret").read_text(encoding="utf-8").strip()
    restricted = await _login(gateway, secret)
    restricted.raise_for_status()
    new_password = secrets.token_urlsafe(24)
    rotated = await gateway.post(
        ROTATE_PATH,
        json={"new_password": new_password},
        headers={"Authorization": f"Bearer {restricted.json()['access_token']}"},
    )
    rotated.raise_for_status()
    stored.write_text(new_password, encoding="utf-8")
    stored.chmod(0o600)
    return new_password


class _AdminTokens:
    def __init__(self, gateway: httpx.AsyncClient, password: str) -> None:
        self._gateway = gateway
        self._password = password
        self._token: str | None = None
        self._obtained = 0.0

    async def get(self) -> str:
        if self._token is None or time.monotonic() - self._obtained > ADMIN_TOKEN_MAX_AGE_S:
            response = await _login(self._gateway, self._password)
            response.raise_for_status()
            self._token = str(response.json()["access_token"])
            self._obtained = time.monotonic()
        return self._token


@pytest_asyncio.fixture(scope="session")
async def _admin_tokens(gateway: httpx.AsyncClient, admin_password: str) -> _AdminTokens:
    return _AdminTokens(gateway, admin_password)


@pytest_asyncio.fixture
async def admin_token(_admin_tokens: _AdminTokens) -> str:
    """A valid admin access token; one login is shared across the session and renewed after
    ten minutes (the gateway rate-limits ``/auth/``)."""
    return await _admin_tokens.get()


@pytest_asyncio.fixture
async def ensure_webhook_hosts(
    gateway: httpx.AsyncClient, admin_token: str
) -> Callable[..., Awaitable[None]]:
    """``await ensure_webhook_hosts("webhook-sink")``: the webhook allow-list is mandatory (quick
    261120-s0p), so a journey that points a card at a host lists it first, as the Global admin.

    Idempotent and additive: it keeps every entry and every other stored notification setting, and
    writes (with the section's ETag) only when a host is missing.
    """

    async def _ensure(*hosts: str) -> None:
        headers = {"Authorization": f"Bearer {admin_token}"}
        listed = await gateway.get("/api/v1/identity/settings", headers=headers)
        assert listed.status_code == 200, listed.text
        section = next(s for s in listed.json()["sections"] if s["section"] == "notifications")
        values: dict[str, Any] = dict(section["values"])
        current = list(values.get("webhook_allowed_hosts") or [])
        missing = [host for host in hosts if host not in current]
        if not missing:
            return
        values["webhook_allowed_hosts"] = [*current, *missing]
        saved = await gateway.put(
            "/api/v1/identity/settings/notifications",
            json=values,
            headers={**headers, "If-Match": str(section["etag"])},
        )
        assert saved.status_code == 200, saved.text

    return _ensure


@dataclass(frozen=True)
class E2EUser:
    """A local user created through the gateway; ``principal_id`` is ``local:<uuid>``."""

    id: str
    principal_id: str
    username: str
    password: str


class _Users:
    """Creates local users as the Global admin and logs each in at most once per session.

    The gateway rate-limits ``/api/v1/identity/auth/`` (average 5/s, burst 10), so tokens are
    cached per user and renewed only after ``ADMIN_TOKEN_MAX_AGE_S``.
    """

    def __init__(self, gateway: httpx.AsyncClient, admin_tokens: _AdminTokens) -> None:
        self._gateway = gateway
        self._admin_tokens = admin_tokens
        self._tokens: dict[str, tuple[str, float]] = {}

    async def create(self, username_prefix: str) -> E2EUser:
        username = f"{username_prefix}-{uuid.uuid4().hex[:8]}"
        password = secrets.token_urlsafe(24)
        admin = await self._admin_tokens.get()
        response = await self._gateway.post(
            "/api/v1/identity/users",
            json={"username": username, "password": password, "display_name": username},
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        return E2EUser(
            id=str(body["id"]),
            principal_id=str(body["principal_id"]),
            username=username,
            password=password,
        )

    async def token(self, user: E2EUser) -> str:
        cached = self._tokens.get(user.username)
        if cached is not None and time.monotonic() - cached[1] <= ADMIN_TOKEN_MAX_AGE_S:
            return cached[0]
        for attempt in range(5):
            response = await self._gateway.post(
                LOGIN_PATH, json={"username": user.username, "password": user.password}
            )
            if response.status_code == 429 and attempt < 4:
                await asyncio.sleep(1.0)
                continue
            assert response.status_code == 200, response.text
            token = str(response.json()["access_token"])
            self._tokens[user.username] = (token, time.monotonic())
            return token
        raise AssertionError("unreachable")  # pragma: no cover


@pytest_asyncio.fixture(scope="session")
async def users(gateway: httpx.AsyncClient, _admin_tokens: _AdminTokens) -> _Users:
    """``await users.create("dev")`` -> ``E2EUser``; ``await users.token(user)`` -> access token."""
    return _Users(gateway, _admin_tokens)


@pytest_asyncio.fixture(scope="session")
async def non_admin_token() -> str:
    """A token from the mock OIDC issuer for a principal with no role assignments.

    The mock deliberately puts ``roles: [global_admin]`` and ``groups: [admins]`` in the token:
    authorization never reads claims, so they must grant nothing.
    """
    url = _env("DC_E2E_MOCK_OIDC_URL").rstrip("/") + "/token"
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:

        async def fetch() -> str | None:
            response = await client.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": "e2e",
                    "client_secret": "e2e",
                },
            )
            return str(response.json()["access_token"]) if response.status_code == 200 else None

        return await wait_for(fetch, 60.0, description="the mock OIDC token endpoint")


@pytest_asyncio.fixture(scope="session")
async def entra_token() -> Callable[[str], Awaitable[str]]:
    """``await entra_token("alice")`` -> a token from the Entra-shaped mock issuer for a persona.

    The mock selects the persona's claims by the ``scope`` form parameter (Plan 04-07's spike), so
    both ``client_id`` and ``scope`` carry the persona name. A fresh token is minted per call.

    The mock derives ``iss`` from ``Host`` and ``X-Forwarded-Proto`` only, so the in-network call
    sends the browser-visible origin the platform trusts (``https://localhost:8443/entra``, Plan
    08-12); without the two headers ``iss`` would be ``http://mock-oidc:8080/entra`` and identity
    would refuse the token.
    """
    url = _env("DC_E2E_ENTRA_OIDC_URL").rstrip("/") + "/token"

    async def get(persona: str) -> str:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:

            async def fetch() -> str | None:
                response = await client.post(
                    url,
                    headers=ENTRA_ISSUER_HEADERS,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": persona,
                        "client_secret": persona,
                        "scope": persona,
                    },
                )
                return str(response.json()["access_token"]) if response.status_code == 200 else None

            return await wait_for(fetch, 60.0, description=f"the Entra mock token for {persona}")

    return get


class FakeGraph:
    """The test mutators of the fake Microsoft Graph (``DC_E2E_FAKE_GRAPH_URL``)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def reset(self) -> None:
        response = await self._client.post("/_fake/reset")
        assert response.status_code in (200, 204), response.text

    async def set_principal(self, oid: str, **fields: Any) -> None:
        response = await self._client.post(f"/_fake/principals/{oid}", json=fields)
        assert response.status_code in (200, 201, 204), response.text


@pytest_asyncio.fixture
async def fake_graph() -> AsyncIterator[FakeGraph]:
    """Mutators for the fake Graph; the directory is reset on teardown so a failed run cannot
    leave a disabled persona behind for the other e2e tests (T-04-48)."""
    async with httpx.AsyncClient(
        base_url=_env("DC_E2E_FAKE_GRAPH_URL"), timeout=httpx.Timeout(10.0)
    ) as client:
        graph = FakeGraph(client)
        try:
            yield graph
        finally:
            await graph.reset()


class Mailpit:
    """The compose mail catcher's HTTP API (``DC_E2E_MAILPIT_URL``, Mailpit v1.34.0).

    Endpoints confirmed by the Plan 04.1-17 spike: ``GET /api/v1/messages`` (a ``messages`` list),
    ``GET /api/v1/search?query=`` (same shape) and ``DELETE /api/v1/messages`` (``ok``).
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def messages(self) -> list[dict[str, Any]]:
        response = await self._client.get("/api/v1/messages", params={"limit": 200})
        assert response.status_code == 200, response.text
        found: list[dict[str, Any]] = response.json()["messages"]
        return found

    async def search(self, query: str) -> list[dict[str, Any]]:
        response = await self._client.get("/api/v1/search", params={"query": query, "limit": 200})
        assert response.status_code == 200, response.text
        found: list[dict[str, Any]] = response.json()["messages"]
        return found

    async def clear(self) -> None:
        response = await self._client.delete("/api/v1/messages")
        assert response.status_code == 200, response.text


@pytest_asyncio.fixture
async def mailpit() -> AsyncIterator[Mailpit]:
    """The mail catcher, emptied before and after the test."""
    async with httpx.AsyncClient(
        base_url=_env("DC_E2E_MAILPIT_URL"), timeout=httpx.Timeout(10.0)
    ) as client:
        catcher = Mailpit(client)
        await catcher.clear()
        try:
            yield catcher
        finally:
            await catcher.clear()


class WebhookSink:
    """The compose webhook receiver's test mutators (``DC_E2E_WEBHOOK_SINK_URL``)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def requests(self) -> list[dict[str, Any]]:
        response = await self._client.get("/_sink/requests")
        assert response.status_code == 200, response.text
        found: list[dict[str, Any]] = response.json()
        return found

    async def fail(self, count: int, status: int = 500) -> None:
        response = await self._client.post("/_sink/fail", json={"count": count, "status": status})
        assert response.status_code == 200, response.text

    async def reset(self) -> None:
        response = await self._client.post("/_sink/reset")
        assert response.status_code == 200, response.text


@pytest_asyncio.fixture
async def webhook_sink() -> AsyncIterator[WebhookSink]:
    """The webhook receiver, reset before and after the test (a queued failure never leaks)."""
    async with httpx.AsyncClient(
        base_url=_env("DC_E2E_WEBHOOK_SINK_URL"), timeout=httpx.Timeout(10.0)
    ) as client:
        sink = WebhookSink(client)
        await sink.reset()
        try:
            yield sink
        finally:
            await sink.reset()


class Compose:
    """Containers of this compose project, found by their compose labels."""

    def __init__(self, client: Any, project: str) -> None:
        self._client = client
        self.project = project

    def containers(self, service: str | None = None, *, all: bool = True) -> list[Container]:  # noqa: A002
        # oneoff=False: services of the stack, not `compose run` containers (including this one)
        labels = [f"com.docker.compose.project={self.project}", "com.docker.compose.oneoff=False"]
        if service is not None:
            labels.append(f"com.docker.compose.service={service}")
        found: list[Container] = self._client.containers.list(all=all, filters={"label": labels})
        return found

    def container(self, service: str) -> Container:
        found = self.containers(service)
        if len(found) != 1:
            raise AssertionError(f"expected one container for service {service}, got {len(found)}")
        return found[0]

    def health(self, service: str) -> str | None:
        container = self.container(service)
        container.reload()
        state: dict[str, Any] = container.attrs["State"]
        health = state.get("Health")
        return None if health is None else str(health["Status"])

    def stop(self, service: str, timeout_s: int = 10) -> None:
        self.container(service).stop(timeout=timeout_s)

    def start(self, service: str) -> None:
        self.container(service).start()


@pytest.fixture(scope="session")
def compose() -> Any:
    import docker  # imported here so host-side collection never needs the docker SDK

    client = docker.from_env()
    try:
        yield Compose(client, _env("DC_E2E_COMPOSE_PROJECT"))
    finally:
        client.close()
