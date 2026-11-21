"""Settings-route helpers that never touch the database: the secret-expiry cache, the connection
test's fixed failure code, the test rate limiter and the about probe of a peer service."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import respx
from dc_auth.principal import Principal
from dc_core.http import make_client
from dc_identity.api import settings as settings_api
from dc_identity.platform_settings import StoredSection

pytestmark = pytest.mark.unit

TENANT_A = "4f1c2d3e-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
TENANT_B = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
APP_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class _Store:
    def __init__(self, default_tenant: str | None) -> None:
        value = {} if default_tenant is None else {"default_tenant_id": default_tenant}
        self._section = StoredSection("entra", value, 1, None, None)

    async def section(self, name: str) -> StoredSection:
        assert name == "entra"
        return self._section


class _Directory:
    """Graph stand-in: tenant A always fails, tenant B knows one secret end date."""

    def __init__(self, end: datetime) -> None:
        self.end = end
        self.asked: list[str] = []

    async def application_secret_expiries(self, tenant_id: str, app_id: str) -> list[datetime]:
        self.asked.append(tenant_id)
        if tenant_id == TENANT_A:
            raise RuntimeError("graph exploded")
        return [self.end]

    async def check_connection(self, tenant_id: str) -> str:
        self.asked.append(tenant_id)
        raise RuntimeError("graph exploded")


def _request(
    directory: Any,
    *,
    default_tenant: str | None = TENANT_A,
    tenants: tuple[str, ...] = (TENANT_A, TENANT_B),
    **state: Any,
) -> Any:
    app_state = SimpleNamespace(
        settings=SimpleNamespace(graph_client_id=APP_ID),
        entra_tenant_ids=tenants,
        platform_settings=_Store(default_tenant),
        directory=directory,
        test_limiter=settings_api.TestRateLimiter(),
        **state,
    )
    return SimpleNamespace(app=SimpleNamespace(state=app_state), headers={})


async def test_a_failing_tenant_is_skipped_and_the_known_expiry_is_cached() -> None:
    end = datetime.now(UTC) + timedelta(days=40, hours=1)
    directory = _Directory(end)
    cache = settings_api.SecretExpiryCache()
    first = await cache.get(_request(directory))
    second = await cache.get(_request(directory))
    assert first.status == "known"
    assert first.expires_at == end
    assert first.days_left == 40
    assert second is first
    assert directory.asked == [TENANT_A, TENANT_B]  # default tenant first, then asked once only


async def test_a_graph_failure_during_the_connection_test_is_the_fixed_graph_error_code(
    make_principal: Callable[..., Principal],
) -> None:
    directory = _Directory(datetime.now(UTC))
    out = await settings_api.entra_test(
        _request(directory, default_tenant=None), make_principal("local:admin")
    )
    assert out.result == "graph-error"
    assert out.tenant_id == TENANT_A
    assert directory.asked == [TENANT_A]


async def test_without_an_entra_tenant_the_connection_test_is_not_configured(
    make_principal: Callable[..., Principal],
) -> None:
    directory = _Directory(datetime.now(UTC))
    out = await settings_api.entra_test(_request(directory, tenants=()), make_principal("local:a"))
    assert out.result == "not-configured"
    assert out.tenant_id is None
    assert directory.asked == []


def test_the_rate_limiter_counts_per_key_and_forgets_hits_outside_the_window() -> None:
    limiter = settings_api.TestRateLimiter(limit=2, window_s=60.0)
    assert limiter.check("a") is None
    assert limiter.check("a") is None
    wait = limiter.check("a")
    assert wait is not None
    assert 1 <= wait <= 61
    assert limiter.check("b") is None
    expired = settings_api.TestRateLimiter(limit=1, window_s=0.0)
    assert expired.check("a") is None
    assert expired.check("a") is None  # the earlier hit already left the zero-length window


async def test_the_service_defaults_are_served_from_a_live_cache_without_asking_anyone() -> None:
    cached = {"contracts": {"default_odcs_api_version": "v3.1.0"}}
    request = _request(None, service_defaults_cache=(time.monotonic() + 30, cached))
    assert await settings_api._service_defaults(request) is cached


async def test_a_peer_about_that_is_not_200_reads_as_unreachable() -> None:
    with respx.mock() as router:
        router.get("http://audit.test/internal/v1/about").respond(503, json={})
        async with make_client(timeout_s=1.0) as http:
            out = await settings_api._ask(http, "audit", "http://audit.test/", "bearer")
    assert out.service == "audit"
    assert out.status == "unreachable"
    assert out.version is None
    assert out.setting_defaults == {}
