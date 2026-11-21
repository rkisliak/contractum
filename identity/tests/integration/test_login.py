"""Refresh-token sessions (AUTH-01, D-03) and the D-23 audit proofs for every revocation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/identity/auth/login"
REFRESH = "/api/v1/identity/auth/refresh"
LOGOUT = "/api/v1/identity/auth/logout"
PASSWORD = "/api/v1/identity/auth/password"
ADMIN_PASSWORD = "an admin password of 30 chars!"
NEW_PASSWORD = "another admin password 31 chars"


def _outbox(sql: Callable[..., list[dict[str, Any]]], db_url: str) -> list[dict[str, Any]]:
    return sql(
        db_url,
        "SELECT id, action, actor, before, after, aggregate_type, aggregate_id "
        "FROM outbox ORDER BY id",
    )


def _admin_id(sql: Callable[..., list[dict[str, Any]]], db_url: str) -> str:
    return str(sql(db_url, "SELECT id FROM users WHERE username = 'admin'")[0]["id"])


async def _login(client: Any) -> dict[str, Any]:
    resp = await client.post(LOGIN, json={"username": "admin", "password": ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_login_returns_an_opaque_refresh_token_and_stores_only_its_sha256(
    session_env: Any, sql: Any
) -> None:
    env, client = session_env
    body = await _login(client)
    raw = body["refresh_token"]
    assert body["scope"] == "api"
    assert len(raw) >= 43
    rows = sql(env.db_url, "SELECT * FROM refresh_tokens")
    assert len(rows) == 1
    row = rows[0]
    assert row["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert all(raw != value for value in row.values())
    assert row["rotated_at"] is None
    assert row["revoked_at"] is None
    assert row["created_by"] == f"local:{_admin_id(sql, env.db_url)}"


async def test_the_restricted_bootstrap_login_has_no_refresh_token(
    make_identity: Callable[..., Any], asgi_client: Any, sql: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        resp = await client.post(LOGIN, json={"username": "admin", "password": env.secret})
    assert resp.status_code == 200
    assert "refresh_token" not in resp.json()
    assert sql(env.db_url, "SELECT 1 FROM refresh_tokens") == []


async def test_refresh_rotates_the_token_and_marks_the_old_one_rotated(
    session_env: Any, sql: Any
) -> None:
    env, client = session_env
    first = (await _login(client))["refresh_token"]
    resp = await client.post(REFRESH, json={"refresh_token": first})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    second = body["refresh_token"]
    assert second != first
    assert body["scope"] == "api"
    assert body["token_type"] == "Bearer"
    principal = await env.app.state.verifier.verify(body["access_token"])
    assert principal.id == f"local:{_admin_id(sql, env.db_url)}"

    rows = {
        r["token_hash"]: r
        for r in sql(env.db_url, "SELECT * FROM refresh_tokens ORDER BY created_at, id")
    }
    old = rows[hashlib.sha256(first.encode()).hexdigest()]
    new = rows[hashlib.sha256(second.encode()).hexdigest()]
    assert old["rotated_at"] is not None
    assert new["rotated_at"] is None
    assert old["family_id"] == new["family_id"]


async def test_refresh_token_reuse_revokes_the_whole_family(session_env: Any) -> None:
    env, client = session_env
    first = (await _login(client))["refresh_token"]
    second = (await client.post(REFRESH, json={"refresh_token": first})).json()["refresh_token"]

    reused = await client.post(REFRESH, json={"refresh_token": first})
    assert reused.status_code == 401
    assert reused.json()["type"] == "urn:dc:problem:invalid-credentials"
    assert reused.headers["content-type"].startswith("application/problem+json")

    newest = await client.post(REFRESH, json={"refresh_token": second})
    assert newest.status_code == 401


async def test_logout_revokes_the_family_and_is_204_for_unknown_tokens(
    session_env: Any,
) -> None:
    _, client = session_env
    third = (await _login(client))["refresh_token"]
    out = await client.post(LOGOUT, json={"refresh_token": third})
    assert out.status_code == 204
    assert out.content == b""
    assert (await client.post(REFRESH, json={"refresh_token": third})).status_code == 401

    unknown = await client.post(LOGOUT, json={"refresh_token": "not-a-real-token-at-all"})
    assert unknown.status_code == 204


async def test_logout_also_kills_the_newest_token_of_the_family(session_env: Any) -> None:
    _, client = session_env
    first = (await _login(client))["refresh_token"]
    newest = (await client.post(REFRESH, json={"refresh_token": first})).json()["refresh_token"]
    assert (await client.post(LOGOUT, json={"refresh_token": first})).status_code == 204
    assert (await client.post(REFRESH, json={"refresh_token": newest})).status_code == 401


async def test_expired_or_malformed_refresh_requests_are_rejected_without_writes(
    session_env: Any, sql: Any
) -> None:
    env, client = session_env
    token = (await _login(client))["refresh_token"]
    sql(env.db_url, "UPDATE refresh_tokens SET expires_at = now() - interval '1 second'")
    before = _outbox(sql, env.db_url)
    assert (await client.post(REFRESH, json={"refresh_token": token})).status_code == 401
    assert (await client.post(REFRESH, json={"refresh_token": "nope"})).status_code == 401
    # D-UI-08-12: no token in the body or the cookie is a plain 401, so a reload without a session
    # is mapped to sign-in; an unknown body field is still a validation error.
    assert (await client.post(REFRESH, json={})).status_code == 401
    assert (await client.post(REFRESH, json={"refresh_token": token, "x": 1})).status_code == 422
    assert _outbox(sql, env.db_url) == before
    assert sql(env.db_url, "SELECT count(*) AS n FROM refresh_tokens")[0]["n"] == 1


async def test_audit_exempt_login_and_refresh_add_no_outbox_rows(
    session_env: Any, sql: Any
) -> None:
    env, client = session_env
    before = len(_outbox(sql, env.db_url))
    first = (await _login(client))["refresh_token"]
    assert len(_outbox(sql, env.db_url)) == before
    resp = await client.post(REFRESH, json={"refresh_token": first})
    assert resp.status_code == 200
    assert len(_outbox(sql, env.db_url)) == before


async def test_audit_logout_writes_exactly_one_typed_event_and_none_for_repeats(
    session_env: Any, sql: Any
) -> None:
    env, client = session_env
    admin_id = _admin_id(sql, env.db_url)
    third = (await _login(client))["refresh_token"]
    family = sql(env.db_url, "SELECT family_id FROM refresh_tokens")[0]["family_id"]
    before = len(_outbox(sql, env.db_url))

    assert (await client.post(LOGOUT, json={"refresh_token": third})).status_code == 204
    events = _outbox(sql, env.db_url)
    assert len(events) == before + 1
    event = events[-1]
    assert event["action"] == "session.logged_out"
    assert event["aggregate_type"] == "session"
    assert event["aggregate_id"] == str(family)
    assert event["actor"]["type"] == "user"
    assert event["actor"]["id"] == f"local:{admin_id}"
    assert event["actor"]["via"] == "refresh_token"
    assert event["before"] == {"status": "active", "user_id": admin_id}
    assert event["after"] == {"status": "revoked", "user_id": admin_id, "reason": "logout"}

    assert (await client.post(LOGOUT, json={"refresh_token": third})).status_code == 204
    assert (await client.post(LOGOUT, json={"refresh_token": "random-unknown"})).status_code == 204
    assert len(_outbox(sql, env.db_url)) == before + 1


async def test_audit_reuse_commits_one_system_event_even_though_the_response_is_401(
    session_env: Any, sql: Any
) -> None:
    env, client = session_env
    admin_id = _admin_id(sql, env.db_url)
    first = (await _login(client))["refresh_token"]
    second = (await client.post(REFRESH, json={"refresh_token": first})).json()["refresh_token"]
    family = sql(env.db_url, "SELECT family_id FROM refresh_tokens LIMIT 1")[0]["family_id"]
    before = len(_outbox(sql, env.db_url))

    assert (await client.post(REFRESH, json={"refresh_token": first})).status_code == 401
    events = _outbox(sql, env.db_url)  # a fresh database session: the revocation committed
    assert len(events) == before + 1
    event = events[-1]
    assert event["action"] == "session.reuse_detected"
    assert event["aggregate_type"] == "session"
    assert event["aggregate_id"] == str(family)
    assert event["actor"]["id"] == "system:session"
    assert event["actor"]["type"] == "system"
    assert event["before"] == {"status": "active", "user_id": admin_id}
    assert event["after"]["reason"] == "reuse_detected"
    assert event["after"]["status"] == "revoked"
    live = sql(
        env.db_url,
        "SELECT count(*) AS n FROM refresh_tokens WHERE revoked_at IS NULL AND rotated_at IS NULL",
    )
    assert live[0]["n"] == 0

    assert (await client.post(REFRESH, json={"refresh_token": first})).status_code == 401
    assert (await client.post(REFRESH, json={"refresh_token": second})).status_code == 401
    assert len(_outbox(sql, env.db_url)) == before + 1


async def test_audit_revoke_all_is_recorded_in_the_single_rotation_event(
    session_env: Any, sql: Any
) -> None:
    env, client = session_env
    first = await _login(client)
    second = await _login(client)
    before = len(_outbox(sql, env.db_url))

    resp = await client.post(
        PASSWORD,
        json={"new_password": NEW_PASSWORD, "current_password": ADMIN_PASSWORD},
        headers={"Authorization": f"Bearer {first['access_token']}"},
    )
    assert resp.status_code == 204, resp.text
    events = _outbox(sql, env.db_url)
    assert len(events) == before + 1
    event = events[-1]
    assert event["action"] == "user.password_rotated"
    assert event["before"]["active_sessions"] == 2
    assert event["after"]["active_sessions"] == 0
    assert not any(e["action"].startswith("session.") for e in events)
    for body in (first, second):
        assert (
            await client.post(REFRESH, json={"refresh_token": body["refresh_token"]})
        ).status_code == 401
    assert len(_outbox(sql, env.db_url)) == before + 1


async def test_audit_snapshots_never_contain_a_refresh_token_or_its_hash(
    session_env: Any, sql: Any
) -> None:
    env, client = session_env
    raws = [(await _login(client))["refresh_token"] for _ in range(2)]
    rotated = (await client.post(REFRESH, json={"refresh_token": raws[0]})).json()["refresh_token"]
    raws.append(rotated)
    await client.post(REFRESH, json={"refresh_token": raws[0]})  # reuse -> event
    third = (await _login(client))["refresh_token"]
    raws.append(third)
    await client.post(LOGOUT, json={"refresh_token": third})  # logout -> event
    haystack = json.dumps(_outbox(sql, env.db_url), default=str)
    assert "session.reuse_detected" in haystack
    assert "session.logged_out" in haystack
    for raw in raws:
        assert raw not in haystack
        assert hashlib.sha256(raw.encode()).hexdigest() not in haystack
    for row in sql(env.db_url, "SELECT token_hash FROM refresh_tokens"):
        assert row["token_hash"] not in haystack


async def test_an_account_without_a_password_never_signs_in_once_bootstrap_is_consumed(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: Callable[..., Any],
    create_user: Callable[..., Any],
    sql: Any,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        user = await create_user(client, token, "nopass")
        sql(env.db_url, "UPDATE users SET password_hash = NULL")
        attempts = [
            await client.post(LOGIN, json={"username": user.username, "password": user.password}),
            await client.post(LOGIN, json={"username": "nopass", "password": env.secret}),
            # The bootstrap secret no longer opens the admin account once rotation consumed it.
            await client.post(LOGIN, json={"username": "admin", "password": env.secret}),
        ]
    assert [(r.status_code, r.json()["type"]) for r in attempts] == [
        (401, "urn:dc:problem:invalid-credentials")
    ] * 3
    assert (
        sql(
            env.db_url,
            "SELECT 1 FROM refresh_tokens WHERE user_id <> %s",
            _admin_id(sql, env.db_url),
        )
        == []
    )
