"""``GET /api/v1/<service>/version``: the public build identity of a service (opt-in)."""

from __future__ import annotations

import httpx
import pytest
from dc_core.app import (
    BUILD_COMMIT_ENV,
    BUILD_TIME_ENV,
    BUILD_VERSION_ENV,
    create_app,
    service_version,
)
from dc_core.settings import CoreSettings

pytestmark = pytest.mark.unit


async def _get(app, path: str) -> httpx.Response:  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path)


async def test_version_route_is_public_cacheable_and_reports_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BUILD_VERSION_ENV, "1.4.0-rc.2")
    monkeypatch.setenv(BUILD_COMMIT_ENV, "abc1234def56")
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
    assert body["commit"] == "abc1234def56"
    assert body["built_at"] == "2026-11-15T20:00:00Z"
    # The platform version of the image, never a package version.
    assert body["version"] == "1.4.0-rc.2"
    doc = app.openapi()
    assert doc["info"]["version"] == "1.4.0-rc.2"
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


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_build_args_read_dev(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    for name in (BUILD_VERSION_ENV, BUILD_COMMIT_ENV, BUILD_TIME_ENV):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    # An installed workspace package (dc-identity 0.1.0) never leaks in as the version.
    info = service_version("identity")
    assert (info.version, info.commit, info.built_at) == ("dev", "dev", "dev")
    app = create_app(
        service="core",
        settings=CoreSettings(service="core", database_url="postgresql+psycopg://x/y"),
        routers=[],
    )
    assert app.openapi()["info"]["version"] == "dev"
