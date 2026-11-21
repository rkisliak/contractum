"""Every routed service answers its public ``GET /api/v1/<module>/version`` without a token."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.unit

ROUTED = ("identity", "contracts", "validation", "audit")


@pytest.mark.parametrize("service", ROUTED)
async def test_the_version_route_is_public_and_cacheable(
    service_apps: dict[str, FastAPI], service: str
) -> None:
    transport = httpx.ASGITransport(app=service_apps[service])
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
        response = await client.get(f"/api/v1/{service}/version")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["service"] == service
    assert body["version"] and body["commit"] and body["built_at"]
    assert response.headers["cache-control"] == "public, max-age=60"
