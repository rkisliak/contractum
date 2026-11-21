"""``GET /api/v1/<module>/version`` through the gateway: public, cacheable build identity.

Every routed service answers without a token with its package version, the git commit and build
time baked into the image (``"dev"`` for a local build), so ``test_zz_operation_coverage.py``
counts the four operations as exercised. Mail is not routed and has no version route.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.e2e

SERVICES = ("identity", "contracts", "validation", "audit")


@pytest.mark.parametrize("service", SERVICES)
async def test_each_routed_service_reports_its_build_without_a_token(
    gateway: httpx.AsyncClient, service: str
) -> None:
    response = await gateway.get(f"/api/v1/{service}/version")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60"
    body = response.json()
    assert body["service"] == service
    assert set(body) == {"service", "version", "commit", "built_at"}
    assert all(isinstance(body[key], str) and body[key] for key in body)


async def test_mail_has_no_public_version_route(gateway: httpx.AsyncClient) -> None:
    response = await gateway.get("/api/v1/mail/version")
    assert response.status_code == 404
