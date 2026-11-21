"""Login backoff per account and per client IP, enforced inside identity (D-04, T-01-39)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/identity/auth/login"
ADMIN_PASSWORD = "an admin password of 30 chars!"
WRONG = "definitely not the password"


@asynccontextmanager
async def _client_from(app: Any, ip: str) -> AsyncIterator[httpx.AsyncClient]:
    """A client whose ASGI scope reports ``ip`` as the peer address (what uvicorn would set)."""
    transport = httpx.ASGITransport(app=app, client=(ip, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _login(client: Any, username: str, password: str) -> Any:
    return await client.post(LOGIN, json={"username": username, "password": password})


def _throttle(sql: Any, db_url: str, key: str) -> dict[str, Any] | None:
    rows = sql(db_url, "SELECT * FROM login_throttle WHERE key = %s", key)
    return rows[0] if rows else None


def _count_calls(app: Any) -> dict[str, int]:
    """Count Argon2 work: real verifies and the constant-cost dummy verify."""
    calls = {"verify": 0, "dummy_verify": 0}
    passwords = app.state.passwords
    verify, dummy = passwords.verify, passwords.dummy_verify

    async def counted_verify(*args: Any, **kwargs: Any) -> bool:
        calls["verify"] += 1
        return bool(await verify(*args, **kwargs))

    async def counted_dummy() -> None:
        calls["dummy_verify"] += 1
        await dummy()

    passwords.verify = counted_verify
    passwords.dummy_verify = counted_dummy
    return calls


async def test_five_failures_lock_the_account_and_the_sixth_attempt_never_hashes(
    session_env: Any, sql: Any
) -> None:
    env, client = session_env
    for _ in range(5):
        assert (await _login(client, "admin", WRONG)).status_code == 401
    calls = _count_calls(env.app)

    locked = await _login(client, "admin", ADMIN_PASSWORD)  # correct password, still locked
    assert locked.status_code == 429
    assert locked.json()["type"] == "urn:dc:problem:too-many-attempts"
    assert locked.headers["content-type"].startswith("application/problem+json")
    retry_after = locked.headers["retry-after"]
    assert retry_after.isdigit()
    assert int(retry_after) >= 1
    assert calls == {"verify": 0, "dummy_verify": 0}  # lockout runs before any hashing

    sql(env.db_url, "UPDATE login_throttle SET locked_until = now() - interval '1 second'")
    ok = await _login(client, "admin", ADMIN_PASSWORD)
    assert ok.status_code == 200
    assert calls["verify"] == 1
    assert _throttle(sql, env.db_url, "acct:admin") is None  # a success resets the account key


async def test_the_lock_doubles_per_failure_and_is_capped(session_env: Any, sql: Any) -> None:
    env, client = session_env
    for _ in range(5):
        await _login(client, "admin", WRONG)
    first = _throttle(sql, env.db_url, "acct:admin")
    assert first is not None
    assert first["failures"] == 5
    lock = sql(
        env.db_url,
        "SELECT extract(epoch FROM locked_until - now()) AS s FROM login_throttle "
        "WHERE key = 'acct:admin'",
    )[0]["s"]
    assert 0 < float(lock) <= 1.0  # 2 ** (5 - 5)

    sql(
        env.db_url,
        "UPDATE login_throttle SET failures = 9, locked_until = now() - interval '1 second' "
        "WHERE key = 'acct:admin'",
    )
    await _login(client, "admin", WRONG)  # failure 10 -> 2 ** 5
    blocked = await _login(client, "admin", WRONG)
    assert blocked.status_code == 429
    assert 30 <= int(blocked.headers["retry-after"]) <= 32

    sql(
        env.db_url,
        "UPDATE login_throttle SET failures = 29, locked_until = NULL WHERE key = 'acct:admin'",
    )
    await _login(client, "admin", WRONG)  # failure 30 -> min(2 ** 25, 900)
    capped = await _login(client, "admin", WRONG)
    assert capped.status_code == 429
    assert 899 <= int(capped.headers["retry-after"]) <= 900


async def test_twenty_failures_lock_one_client_ip_but_not_another(
    session_env: Any, sql: Any
) -> None:
    env, _ = session_env
    async with _client_from(env.app, "198.51.100.7") as ip_a:
        for i in range(20):
            assert (await _login(ip_a, f"ghost{i}", WRONG)).status_code == 401
        locked = await _login(ip_a, "a-fresh-username", WRONG)
        assert locked.status_code == 429
        assert locked.json()["type"] == "urn:dc:problem:too-many-attempts"
        assert int(locked.headers["retry-after"]) >= 1
    async with _client_from(env.app, "203.0.113.9") as ip_b:
        assert (await _login(ip_b, "a-fresh-username", WRONG)).status_code == 401
    ip_row = _throttle(sql, env.db_url, "ip:198.51.100.7")
    assert ip_row is not None
    assert ip_row["failures"] == 20


async def test_an_ip_success_does_not_reset_the_ip_key(session_env: Any, sql: Any) -> None:
    env, _ = session_env
    async with _client_from(env.app, "192.0.2.44") as client:
        for i in range(3):
            await _login(client, f"ghost{i}", WRONG)
        assert (await _login(client, "admin", ADMIN_PASSWORD)).status_code == 200
    row = _throttle(sql, env.db_url, "ip:192.0.2.44")
    assert row is not None
    assert row["failures"] == 3


async def test_unknown_usernames_are_throttled_like_known_ones_with_the_same_bodies(
    session_env: Any, sql: Any
) -> None:
    env, client = session_env
    assert (await _login(client, "Ghost", WRONG)).status_code == 401
    ghost_row = _throttle(sql, env.db_url, "acct:ghost")  # lowercased account key
    assert ghost_row is not None
    assert ghost_row["failures"] == 1

    for _ in range(4):
        await _login(client, "ghost", WRONG)
    for _ in range(5):
        await _login(client, "admin", WRONG)
    locked_unknown = await _login(client, "ghost", WRONG)
    locked_known = await _login(client, "admin", WRONG)
    assert locked_unknown.status_code == locked_known.status_code == 429

    def shape(resp: Any) -> dict[str, Any]:
        body = dict(resp.json())
        body.pop("request_id", None)
        assert resp.headers["retry-after"].isdigit()
        return body

    assert shape(locked_unknown) == shape(locked_known)


async def test_unlocked_known_and_unknown_users_get_indistinguishable_401s(
    session_env: Any,
) -> None:
    _, client = session_env
    unknown = await _login(client, "nobody", WRONG)
    known = await _login(client, "admin", WRONG)
    assert unknown.status_code == known.status_code == 401

    def shape(resp: Any) -> dict[str, Any]:
        body = dict(resp.json())
        body.pop("request_id", None)
        return body

    assert shape(unknown) == shape(known)
    assert "retry-after" not in unknown.headers
    assert "retry-after" not in known.headers


async def test_throttle_rows_are_written_by_the_system_actor_and_commit_despite_the_401(
    session_env: Any, sql: Any
) -> None:
    env, client = session_env
    outbox_before = sql(env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"]
    assert (await _login(client, "admin", WRONG)).status_code == 401
    for key in ("acct:admin", "ip:127.0.0.1"):
        row = _throttle(sql, env.db_url, key)
        assert row is not None, key
        assert row["failures"] == 1
        assert row["created_by"] == "system:login-throttle"
        assert row["updated_by"] == "system:login-throttle"
    # D-23: throttle counter updates are exempt from auditing.
    assert sql(env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"] == outbox_before


async def test_throttle_keys_are_built_from_the_normalised_username_and_client_ip() -> None:
    from dc_identity.throttle import throttle_keys

    assert throttle_keys("  Admin ", "10.0.0.1") == ["acct:admin", "ip:10.0.0.1"]


async def test_locked_attempts_do_not_extend_the_lock(session_env: Any, sql: Any) -> None:
    env, client = session_env
    for _ in range(5):
        await _login(client, "admin", WRONG)
    before = _throttle(sql, env.db_url, "acct:admin")
    for _ in range(3):
        assert (await _login(client, "admin", WRONG)).status_code == 429
    after = _throttle(sql, env.db_url, "acct:admin")
    assert before is not None
    assert after is not None
    assert (after["failures"], after["locked_until"]) == (
        before["failures"],
        before["locked_until"],
    )


async def test_a_stale_failure_count_restarts_so_old_failures_never_accumulate(
    session_env: Any, sql: Any
) -> None:
    """CR-01: 20 old failures, a pause beyond the window, then one failure must not lock the IP."""
    env, _ = session_env
    ip = "198.51.100.77"
    async with _client_from(env.app, ip) as client:
        for i in range(20):
            assert (await _login(client, f"ghost{i}", WRONG)).status_code == 401
        assert (await _login(client, "fresh", WRONG)).status_code == 429  # locked inside the window

        window = env.app.state.settings.throttle_window_s
        sql(
            env.db_url,
            "UPDATE login_throttle SET locked_until = now() - interval '1 second', "
            "updated_at = now() - make_interval(secs => %s) WHERE key = %s",
            window + 60,
            f"ip:{ip}",
        )
        again = await _login(client, "fresh", WRONG)
        assert again.status_code == 401  # not 429: the stale count restarted
        row = _throttle(sql, env.db_url, f"ip:{ip}")
        assert row is not None
        assert row["failures"] == 1
        assert row["locked_until"] is None
        assert (await _login(client, "admin", ADMIN_PASSWORD)).status_code == 200


async def test_failures_inside_the_window_still_accumulate(session_env: Any, sql: Any) -> None:
    env, client = session_env
    for _ in range(3):
        await _login(client, "admin", WRONG)
    window = env.app.state.settings.throttle_window_s
    sql(
        env.db_url,
        "UPDATE login_throttle SET updated_at = now() - make_interval(secs => %s) "
        "WHERE key = 'acct:admin'",
        window - 60,
    )
    await _login(client, "admin", WRONG)
    row = _throttle(sql, env.db_url, "acct:admin")
    assert row is not None
    assert row["failures"] == 4
