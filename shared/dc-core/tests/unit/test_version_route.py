"""``GET /api/v1/<service>/version``: the public build identity of a service (opt-in)."""

from __future__ import annotations

import httpx
import pytest
from dc_core.app import BUILD_COMMIT_ENV, BUILD_TIME_ENV, create_app, service_version
from dc_core.settings import CoreSettings

pytestmark = pytest.mark.unit


async def _get(app, path: str) -> httpx.Response:  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path)


async def test_version_route_is_public_cacheable_and_reports_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BUILD_COMMIT_ENV, "abc1234")
    monkeypatch.setenv(BUILD_TIME_ENV, "2026-11-15T20:00:00Z")
    app = create_app(
        service="core",
        settings=CoreSettings(service="core", database_url="postgresql+psycopg://x/y"),
        routers=[],
        version_route=True,
    )
    response = await _get(app, "/api/v1/core/version")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60"
    body = response.json()
    assert body["service"] == "core"
    assert body["commit"] == "abc1234"
    assert body["built_at"] == "2026-11-15T20:00:00Z"
    # dc-core is installed in the workspace, so the version is a real one.
    assert body["version"] != ""
    doc = app.openapi()
    operation = doc["paths"]["/api/v1/core/version"]["get"]
    assert operation["security"] == []
    assert "ServiceVersionOut" in doc["components"]["schemas"]


async def test_version_route_is_opt_in() -> None:
    app = create_app(
        service="core",
        settings=CoreSettings(service="core", database_url="postgresql+psycopg://x/y"),
        routers=[],
    )
    assert (await _get(app, "/api/v1/core/version")).status_code == 404


def test_unknown_package_and_unset_build_args_read_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BUILD_COMMIT_ENV, raising=False)
    monkeypatch.delenv(BUILD_TIME_ENV, raising=False)
    info = service_version("no-such-service")
    assert (info.version, info.commit, info.built_at) == ("dev", "dev", "dev")
