"""GET /internal/v1/events: admin-only read of ingested audit events (T-01-22)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx
from dc_audit.ingest import handle_payload
from dc_audit.main import create_audit_app
from dc_audit.settings import AuditSettings
from dc_auth.issuers import AuthSettings
from dc_auth.testing import FakeIssuer
from dc_core.events import encode_envelope

pytestmark = pytest.mark.integration

PDP = "http://identity:8000/internal/v1/authz/check"
EVENTS = "/internal/v1/events"


def _event(aggregate_id: str, *, service: str = "probe", action: str = "widget.created") -> dict:  # type: ignore[type-arg]
    return {
        "event_id": str(uuid.uuid4()),
        "occurred_at": "2026-11-09T12:00:00+00:00",
        "service": service,
        "aggregate_type": "widget",
        "aggregate_id": aggregate_id,
        "aggregate_version": None,
        "scope_path": [],
        "action": action,
        "actor": {"type": "user", "id": "fake:admin", "display": "Admin", "via": "jwt"},
        "request_id": "req-1",
        "before": None,
        "after": {"id": aggregate_id},
        "schema_version": 1,
    }


@pytest.fixture
def app(audit_env: Any, fake_issuer: FakeIssuer):  # type: ignore[no-untyped-def]
    settings = AuditSettings(database_url=audit_env.db_url)  # type: ignore[arg-type]
    auth = AuthSettings(
        issuers=[fake_issuer.issuer_config()], identity_internal_url="http://identity:8000"
    )
    return create_audit_app(settings, auth)


@pytest.fixture
async def seeded(audit_env: Any) -> dict[str, Any]:
    events = [
        _event("w-1"),
        _event("w-1", action="widget.updated"),
        _event("w-2"),
        _event("u-1", service="identity", action="user.created"),
    ]
    for event in events:
        assert await handle_payload(audit_env.session_factory, encode_envelope(event))
    return {"events": events}


def _auth(fake_issuer: FakeIssuer) -> dict[str, str]:
    return {"Authorization": f"Bearer {fake_issuer.mint('admin')}"}


async def test_allowed_principal_reads_events_filtered_by_aggregate_service_and_action(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, seeded: dict[str, Any]
) -> None:
    with respx.mock() as router:
        route = router.post(PDP).respond(json={"outcome": "allow", "folder": None})
        async with asgi_client(app) as client:
            by_aggregate = await client.get(
                EVENTS, params={"aggregate_id": "w-1"}, headers=_auth(fake_issuer)
            )
            by_service = await client.get(
                EVENTS, params={"service": "identity"}, headers=_auth(fake_issuer)
            )
            by_action = await client.get(
                EVENTS, params={"action": "widget.updated"}, headers=_auth(fake_issuer)
            )
            everything = await client.get(EVENTS, params={"limit": 2}, headers=_auth(fake_issuer))

    assert by_aggregate.status_code == 200
    body = by_aggregate.json()
    assert [e["action"] for e in body] == ["widget.created", "widget.updated"]  # ordered by id
    first = body[0]
    assert first["event_id"] == seeded["events"][0]["event_id"]
    assert first["service"] == "probe"
    assert first["aggregate_type"] == "widget"
    assert first["actor"]["id"] == "fake:admin"
    assert first["before"] is None
    assert first["after"] == {"id": "w-1"}
    assert first["changes"] == [{"op": "add", "path": "/id", "value": "w-1"}]
    assert {"received_at", "occurred_at", "scope_path", "request_id", "aggregate_version"} <= set(
        first
    )

    assert [e["aggregate_id"] for e in by_service.json()] == ["u-1"]
    assert [e["aggregate_id"] for e in by_action.json()] == ["w-1"]
    assert len(everything.json()) == 2

    request = route.calls.last.request
    assert json.loads(request.content)["permission"] == "audit:read"
    assert request.headers["authorization"].startswith("Bearer ")


@pytest.mark.parametrize("limit", [0, 201, -1, "x"])
async def test_limit_is_bounded(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, limit: Any
) -> None:
    with respx.mock() as router:
        router.post(PDP).respond(json={"outcome": "allow", "folder": None})
        async with asgi_client(app) as client:
            response = await client.get(EVENTS, params={"limit": limit}, headers=_auth(fake_issuer))
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_forbidden_principal_gets_403_problem(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, seeded: dict[str, Any]
) -> None:
    with respx.mock() as router:
        router.post(PDP).respond(json={"outcome": "forbidden", "folder": None})
        async with asgi_client(app) as client:
            response = await client.get(EVENTS, headers=_auth(fake_issuer))
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:dc:problem:forbidden"
    assert "w-1" not in response.text  # no event data leaks into the denial


async def test_missing_or_invalid_token_gets_401_and_never_asks_identity(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer
) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(PDP).respond(json={"outcome": "allow", "folder": None})
        async with asgi_client(app) as client:
            anonymous = await client.get(EVENTS)
            garbage = await client.get(EVENTS, headers={"Authorization": "Bearer not-a-jwt"})
            expired = await client.get(
                EVENTS,
                headers={"Authorization": f"Bearer {fake_issuer.mint('admin', exp_delta_s=-60)}"},
            )
    assert [anonymous.status_code, garbage.status_code, expired.status_code] == [401, 401, 401]
    assert anonymous.headers["www-authenticate"].startswith("Bearer")
    assert anonymous.headers["content-type"].startswith("application/problem+json")
    assert not route.called


async def test_unreachable_identity_fails_closed_with_503(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, seeded: dict[str, Any]
) -> None:
    with respx.mock() as router:
        router.post(PDP).mock(side_effect=httpx.ConnectError("identity is down"))
        async with asgi_client(app) as client:
            response = await client.get(EVENTS, headers=_auth(fake_issuer))
    assert response.status_code == 503
    assert response.json()["type"] == "urn:dc:problem:service-unavailable"
    assert "w-1" not in response.text


async def test_events_are_not_part_of_the_public_openapi(app: Any, asgi_client: Any) -> None:
    async with asgi_client(app) as client:
        response = await client.get("/api/v1/audit/openapi.json")
    assert response.status_code == 200
    assert not [p for p in response.json()["paths"] if "/internal" in p]
