"""The walking skeleton, black-box: bootstrap login and rotation land in the audit service."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

NOT_FOUND = "urn:dc:problem:not-found"


def _assert_problem(response: httpx.Response, status: int, problem_type: str | None = None) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == status
    if problem_type is not None:
        assert body["type"] == problem_type


async def test_rotation_event_reached_audit_exactly_once_and_leaks_nothing(
    internal: Callable[[str], httpx.AsyncClient],
    admin_token: str,
    admin_password: str,
    wait_for: Callable[..., Any],
) -> None:
    audit = internal("audit")
    headers = {"Authorization": f"Bearer {admin_token}"}

    async def rotated() -> list[dict[str, Any]] | None:
        response = await audit.get(
            "/internal/v1/events", params={"action": "user.password_rotated"}, headers=headers
        )
        response.raise_for_status()
        # Later password changes (the UI password-change journey, an operator restoring a
        # password) emit the same action; only the bootstrap rotation starts from must_rotate.
        events: list[dict[str, Any]] = [
            event
            for event in response.json()
            if (event.get("before") or {}).get("must_rotate") is True
        ]
        return events or None

    events = await wait_for(rotated, 30.0, description="the bootstrap user.password_rotated event")
    assert len(events) == 1
    event = events[0]
    assert event["actor"]["type"] == "user"
    assert str(event["actor"]["id"]).startswith("local:")
    text = json.dumps(event)
    bootstrap_secret = open("/run/secrets/bootstrap_admin_secret", encoding="utf-8").read().strip()  # noqa: ASYNC230
    assert "$argon2" not in text
    assert bootstrap_secret not in text
    assert admin_password not in text


@pytest.mark.parametrize("path", ["/internal/v1/events", "/healthz", "/.well-known/jwks.json"])
async def test_paths_no_router_serves_are_404_problems_from_the_catch_all(
    gateway: httpx.AsyncClient, path: str
) -> None:
    _assert_problem(await gateway.get(path), 404, NOT_FOUND)


async def test_a_roles_claim_token_is_authenticated_but_authorized_for_nothing(
    internal: Callable[[str], httpx.AsyncClient], non_admin_token: str
) -> None:
    response = await internal("audit").get(
        "/internal/v1/events", headers={"Authorization": f"Bearer {non_admin_token}"}
    )
    _assert_problem(response, 403)
