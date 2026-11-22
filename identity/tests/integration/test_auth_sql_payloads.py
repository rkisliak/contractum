"""SQL injection payloads at the credential routes answer exactly like any wrong credential.

The nightly OWASP ZAP scan sends boolean SQL injection probes (``ZAP AND 1=1 -- ``,
``ZAP OR 1=1 -- `` and their quoted forms) as the login password, the login username and the
refresh token. Here the same payloads, plus a few classic ones, go through the real routes on
Postgres: every answer must equal the answer to a plain wrong credential (status, problem type,
body), no session may start and nothing but the login throttle's own failure counters may change.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/identity/auth/login"
REFRESH = "/api/v1/identity/auth/refresh"
PASSWORD = "/api/v1/identity/auth/password"
ADMIN_PASSWORD = "an admin password of 30 chars!"  # noqa: S105

# ZAP's boolean probes (scan rule 40018) and a few classic injections.
PAYLOADS = [
    "ZAP AND 1=1 -- ",
    "ZAP AND 1=2 -- ",
    "ZAP OR 1=1 -- ",
    "ZAP' AND '1'='1' -- ",
    "ZAP' AND '1'='2' -- ",
    "ZAP' OR '1'='1' -- ",
    'ZAP" AND "1"="1" -- ',
    'ZAP" OR "1"="1" -- ',
    "' OR '1'='1",
    "' OR 1=1 --",
    "admin' --",
    "admin'/*",
    "1; DROP TABLE users; --",
    "' UNION SELECT username, password_hash FROM users --",
    "'; SELECT pg_sleep(5); --",
]

# The tables a successful injection would read or change; the login throttle is left out on
# purpose, because counting failed attempts is its job.
TABLES = ("users", "refresh_tokens", "outbox")


def _shape(response: Any) -> tuple[int, str, str]:
    """Status, content type and the problem body without its per-request id."""
    body = {k: v for k, v in response.json().items() if k != "request_id"}
    return (
        response.status_code,
        response.headers["content-type"],
        json.dumps(body, sort_keys=True),
    )


def _snapshot(sql: Callable[..., list[dict[str, Any]]], db_url: str) -> dict[str, Any]:
    return {
        table: sql(db_url, f"SELECT * FROM {table} ORDER BY 1")  # noqa: S608 - fixed names
        for table in TABLES
    }


@pytest.fixture
async def auth_env(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    bootstrap_token: Callable[[Any, Any], Awaitable[str]],
) -> AsyncIterator[tuple[Any, Any]]:
    """Identity with the admin's password set and a throttle that never locks during the test."""
    env = make_identity(throttle_account_threshold=10_000, throttle_ip_threshold=10_000)
    async with asgi_client(env.app) as client:
        restricted = await bootstrap_token(client, env)
        rotated = await client.post(
            PASSWORD,
            json={"new_password": ADMIN_PASSWORD},
            headers={"Authorization": f"Bearer {restricted}"},
        )
        assert rotated.status_code == 204, rotated.text
        yield env, client


async def test_injection_payloads_as_login_password_or_username_are_a_plain_401(
    auth_env: tuple[Any, Any], sql: Callable[..., list[dict[str, Any]]]
) -> None:
    env, client = auth_env
    before = _snapshot(sql, env.db_url)
    wrong = await client.post(LOGIN, json={"username": "admin", "password": "not the password"})
    expected = _shape(wrong)
    assert expected[0] == 401
    assert wrong.json()["type"] == "urn:dc:problem:invalid-credentials"

    for payload in PAYLOADS:
        as_password = await client.post(LOGIN, json={"username": "admin", "password": payload})
        assert _shape(as_password) == expected, (payload, as_password.text)
        as_username = await client.post(
            LOGIN, json={"username": payload, "password": ADMIN_PASSWORD}
        )
        assert _shape(as_username) == expected, (payload, as_username.text)
        both = await client.post(LOGIN, json={"username": f"admin{payload}", "password": payload})
        assert _shape(both) == expected, (payload, both.text)

    assert _snapshot(sql, env.db_url) == before  # no session, no user change, no event
    # The real password still works afterwards: nothing was locked or altered.
    ok = await client.post(LOGIN, json={"username": "admin", "password": ADMIN_PASSWORD})
    assert ok.status_code == 200, ok.text


async def test_injection_payloads_as_refresh_token_are_a_plain_401(
    auth_env: tuple[Any, Any], sql: Callable[..., list[dict[str, Any]]]
) -> None:
    env, client = auth_env
    session = await client.post(LOGIN, json={"username": "admin", "password": ADMIN_PASSWORD})
    assert session.status_code == 200, session.text
    live = session.json()["refresh_token"]
    before = _snapshot(sql, env.db_url)
    unknown = await client.post(REFRESH, json={"refresh_token": secrets.token_urlsafe(32)})
    expected = _shape(unknown)
    assert expected[0] == 401
    assert unknown.json()["type"] == "urn:dc:problem:invalid-credentials"

    for payload in PAYLOADS:
        response = await client.post(REFRESH, json={"refresh_token": payload})
        assert _shape(response) == expected, (payload, response.text)
        appended = await client.post(REFRESH, json={"refresh_token": live + payload})
        assert _shape(appended) == expected, (payload, appended.text)

    assert _snapshot(sql, env.db_url) == before  # no token rotated, revoked or issued
    # The live token is untouched: it still rotates exactly once.
    rotated = await client.post(REFRESH, json={"refresh_token": live})
    assert rotated.status_code == 200, rotated.text
