"""Session, credential, lockout and password limits from the stored security settings (quick
261120-0ye, D-38 to D-41): the absolute session cap and the idle timeout (migration 0012), the
cookie Max-Age, the stored lockout thresholds, the credential ceiling and the password rules."""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

SETTINGS = "/api/v1/identity/settings"
AUTH = "/api/v1/identity/auth"
LOGIN = f"{AUTH}/login"
ADMIN_PASSWORD = "an admin password of 30 chars!"
Sql = Callable[..., list[dict[str, Any]]]
AdminFlow = Callable[[Any, Any], Awaitable[str]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class World:
    env: Any
    client: httpx.AsyncClient
    sql: Sql
    admin: str

    async def store(self, body: dict[str, Any]) -> None:
        listing = await self.client.get(SETTINGS, headers=bearer(self.admin))
        etag = next(s["etag"] for s in listing.json()["sections"] if s["section"] == "security")
        resp = await self.client.put(
            f"{SETTINGS}/security", json=body, headers={**bearer(self.admin), "If-Match": etag}
        )
        assert resp.status_code == 200, resp.text

    async def login(self, *, cookie: bool = False) -> httpx.Response:
        headers = {"X-Token-Delivery": "cookie"} if cookie else {}
        return await self.client.post(
            LOGIN, json={"username": "admin", "password": ADMIN_PASSWORD}, headers=headers
        )

    def tokens(self) -> list[dict[str, Any]]:
        return self.sql(self.env.db_url, "SELECT * FROM refresh_tokens ORDER BY created_at")


@pytest.fixture
def world(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> Callable[..., Any]:
    @asynccontextmanager
    async def _open(**overrides: Any) -> AsyncIterator[World]:
        env = make_identity(**overrides)
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            # the bootstrap flow's own login left a session behind: start each test without one
            sql(env.db_url, "DELETE FROM refresh_tokens")
            yield World(env, client, sql, admin)

    return _open


# --- S3: session cap and idle timeout -------------------------------------------------------------


async def test_the_idle_timeout_sets_the_token_lifetime_on_login_and_rotation(world: Any) -> None:
    async with world() as w:
        await w.store({"idle_timeout_s": 900})
        login = await w.login()
        (row,) = w.tokens()
        lifetime = (row["expires_at"] - row["created_at"]).total_seconds()
        assert 890 <= lifetime <= 910
        rotated = await w.client.post(
            f"{AUTH}/refresh", json={"refresh_token": login.json()["refresh_token"]}
        )
        assert rotated.status_code == 200, rotated.text
        newest = w.tokens()[-1]
        assert 890 <= (newest["expires_at"] - newest["created_at"]).total_seconds() <= 910
        # the session start travels with the family
        assert newest["session_started_at"] == w.tokens()[0]["session_started_at"]


async def test_the_default_idle_timeout_is_the_env_value(world: Any) -> None:
    async with world(refresh_token_ttl_s=7200) as w:
        await w.login()
        (row,) = w.tokens()
        assert 7190 <= (row["expires_at"] - row["created_at"]).total_seconds() <= 7210


async def test_the_absolute_session_cap_refuses_a_rotation_without_an_event(world: Any) -> None:
    async with world() as w:
        await w.store({"session_max_s": 3600})
        login = await w.login()
        refresh = login.json()["refresh_token"]
        events = len(w.sql(w.env.db_url, "SELECT id FROM outbox"))
        w.sql(
            w.env.db_url,
            "UPDATE refresh_tokens SET session_started_at = now() - interval '2 hours'",
        )
        refused = await w.client.post(f"{AUTH}/refresh", json={"refresh_token": refresh})
        assert refused.status_code == 401, refused.text
        assert refused.json()["type"] == "urn:dc:problem:invalid-credentials"
        assert len(w.sql(w.env.db_url, "SELECT id FROM outbox")) == events
        # the token was not consumed or revoked by the refusal
        (row,) = w.tokens()
        assert row["rotated_at"] is None and row["revoked_at"] is None


async def test_a_session_inside_the_cap_rotates(world: Any) -> None:
    async with world() as w:
        await w.store({"session_max_s": 3600})
        login = await w.login()
        w.sql(
            w.env.db_url,
            "UPDATE refresh_tokens SET session_started_at = now() - interval '30 minutes'",
        )
        ok = await w.client.post(
            f"{AUTH}/refresh", json={"refresh_token": login.json()["refresh_token"]}
        )
        assert ok.status_code == 200, ok.text


async def test_the_env_default_session_cap_is_seven_days(world: Any) -> None:
    async with world() as w:
        login = await w.login()
        refresh = login.json()["refresh_token"]
        w.sql(
            w.env.db_url,
            "UPDATE refresh_tokens SET session_started_at = now() - interval '8 days'",
        )
        refused = await w.client.post(f"{AUTH}/refresh", json={"refresh_token": refresh})
        assert refused.status_code == 401, refused.text


async def test_the_cookie_max_age_is_the_shorter_of_idle_and_the_rest_of_the_session(
    world: Any,
) -> None:
    async with world() as w:
        await w.store({"session_max_s": 3600, "idle_timeout_s": 3600})
        login = await w.login(cookie=True)
        assert login.status_code == 200, login.text
        assert 3590 <= max_age(login) <= 3600
        # 50 minutes in, 10 minutes of the session are left: the cookie says so
        w.sql(
            w.env.db_url,
            "UPDATE refresh_tokens SET session_started_at = now() - interval '50 minutes'",
        )
        cookie = login.cookies.get("__Secure-dc_refresh") or re.search(
            r"__Secure-dc_refresh=([^;]+)", login.headers["set-cookie"]
        ).group(1)  # type: ignore[union-attr]
        refreshed = await w.client.post(
            f"{AUTH}/refresh",
            headers={"X-Token-Delivery": "cookie", "Cookie": f"__Secure-dc_refresh={cookie}"},
        )
        assert refreshed.status_code == 200, refreshed.text
        assert 590 <= max_age(refreshed) <= 610


def max_age(response: httpx.Response) -> int:
    match = re.search(r"Max-Age=(\d+)", response.headers["set-cookie"])
    assert match is not None, response.headers["set-cookie"]
    return int(match.group(1))


async def test_an_idle_timeout_above_the_session_length_is_refused(world: Any) -> None:
    async with world(session_max_s=3600) as w:
        listing = await w.client.get(SETTINGS, headers=bearer(w.admin))
        etag = next(s["etag"] for s in listing.json()["sections"] if s["section"] == "security")
        resp = await w.client.put(
            f"{SETTINGS}/security",
            json={"idle_timeout_s": 7200},
            headers={**bearer(w.admin), "If-Match": etag},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["errors"][0]["pointer"] == "/idle_timeout_s"
        both = await w.client.put(
            f"{SETTINGS}/security",
            json={"idle_timeout_s": 7200, "session_max_s": 3600},
            headers={**bearer(w.admin), "If-Match": etag},
        )
        assert both.status_code == 422
        assert both.json()["errors"][0]["pointer"] == "/idle_timeout_s"


# --- S4: lockout thresholds -----------------------------------------------------------------------


async def test_the_stored_lockout_threshold_and_max_lock_are_honoured(world: Any) -> None:
    async with world() as w:
        await w.store({"lockout_account_threshold": 3, "lockout_max_lock_s": 60})
        for _ in range(3):
            resp = await w.client.post(LOGIN, json={"username": "admin", "password": "wrong"})
            assert resp.status_code == 401
        locked = await w.client.post(LOGIN, json={"username": "admin", "password": ADMIN_PASSWORD})
        assert locked.status_code == 429, locked.text
        assert 1 <= int(locked.headers["retry-after"]) <= 60


async def test_the_env_thresholds_apply_when_nothing_is_stored(world: Any) -> None:
    async with world() as w:
        for _ in range(4):
            await w.client.post(LOGIN, json={"username": "admin", "password": "wrong"})
        still = await w.login()
        assert still.status_code == 200  # the env threshold is 5


# --- S2: the credential ceiling -------------------------------------------------------------------


async def test_a_pat_expiry_above_the_stored_ceiling_is_refused_below_it_is_created(
    world: Any,
) -> None:
    async with world() as w:
        await w.store({"credential_max_ttl_days": 30})

        async def create(days: int) -> httpx.Response:
            return await w.client.post(
                "/api/v1/identity/pats",
                json={
                    "name": f"t{days}",
                    "expires_at": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
                },
                headers=bearer(w.admin),
            )

        too_far = await create(31)
        assert too_far.status_code == 422, too_far.text
        assert too_far.json()["type"] == "urn:dc:problem:expiry-out-of-range"
        assert (await create(29)).status_code == 201


async def test_a_raised_ceiling_allows_longer_pats_than_the_env_value(world: Any) -> None:
    async with world() as w:
        await w.store({"credential_max_ttl_days": 1000})
        resp = await w.client.post(
            "/api/v1/identity/pats",
            json={
                "name": "long",
                "expires_at": (datetime.now(UTC) + timedelta(days=900)).isoformat(),
            },
            headers=bearer(w.admin),
        )
        assert resp.status_code == 201, resp.text


# --- S5: password rules ---------------------------------------------------------------------------


async def change_password(w: Any, token: str, current: str, new: str) -> httpx.Response:
    return await w.client.post(
        f"{AUTH}/password",
        json={"current_password": current, "new_password": new},
        headers=bearer(token),
    )


async def test_password_rules_apply_when_a_new_password_is_set_never_at_login(world: Any) -> None:
    async with world() as w:
        await w.store(
            {
                "password_min_length": 14,
                "password_require_upper": True,
                "password_require_digit": True,
                "password_require_symbol": True,
            }
        )
        # the existing 30-character password has no rule-conforming shape guarantee: login works
        assert (await w.login()).status_code == 200

        cases = {
            "short1!Aaaaaa": "14 to 128",  # 13 characters
            "alllowercase1!x": "upper-case",
            "NoDigitsHere!!!!": "digit",
            "NoSymbolsHere123": "symbol",
        }
        for candidate, fragment in cases.items():
            resp = await change_password(w, w.admin, ADMIN_PASSWORD, candidate)
            assert resp.status_code == 422, (candidate, resp.text)
            assert resp.json()["type"] == "urn:dc:problem:password-policy"
            assert fragment in resp.json()["detail"], resp.text
            assert candidate not in resp.text  # never echoed
        good = await change_password(w, w.admin, ADMIN_PASSWORD, "Valid-Passw0rd-123")
        assert good.status_code == 204, good.text


async def test_user_creation_follows_the_rules_too(world: Any) -> None:
    async with world() as w:
        await w.store({"password_min_length": 20})
        weak = await w.client.post(
            "/api/v1/identity/users",
            json={
                "username": "weak",
                "password": secrets.token_urlsafe(10),
                "display_name": "Weak",
            },
            headers=bearer(w.admin),
        )
        assert weak.status_code == 422, weak.text
        assert weak.json()["type"] == "urn:dc:problem:password-policy"
        strong = await w.client.post(
            "/api/v1/identity/users",
            json={
                "username": "strong",
                "password": secrets.token_urlsafe(20),
                "display_name": "Strong",
            },
            headers=bearer(w.admin),
        )
        assert strong.status_code == 201, strong.text


# --- the concurrent session limit (quick 261120-s0p, D-09) ---------------------------------------


def evictions(w: World) -> list[dict[str, Any]]:
    return w.sql(
        w.env.db_url,
        "SELECT aggregate_type, aggregate_id, before, after, actor->>'id' AS actor_id "
        "FROM outbox WHERE action = 'user.sessions_evicted' ORDER BY id",
    )


def live_families(w: World, user: str = "admin") -> list[str]:
    rows = w.sql(
        w.env.db_url,
        "SELECT DISTINCT family_id FROM refresh_tokens r JOIN users u ON u.id = r.user_id "
        "WHERE u.username = %s AND r.rotated_at IS NULL AND r.revoked_at IS NULL "
        "AND r.expires_at > now()",
        user,
    )
    return sorted(str(r["family_id"]) for r in rows)


async def test_a_sign_in_beyond_the_limit_ends_the_oldest_session_with_one_event(
    world: Any,
) -> None:
    async with world() as w:
        await w.store({"max_sessions_per_user": 3})
        first = await w.login()
        second = await w.login()
        third = await w.login()
        assert len(live_families(w)) == 3
        assert evictions(w) == []
        oldest = w.sql(
            w.env.db_url,
            "SELECT family_id FROM refresh_tokens ORDER BY session_started_at, id LIMIT 1",
        )[0]["family_id"]
        fourth = await w.login()
        assert fourth.status_code == 200, fourth.text  # sign-in is never refused
        assert len(live_families(w)) == 3
        assert str(oldest) not in live_families(w)
        gone = await w.client.post(
            f"{AUTH}/refresh", json={"refresh_token": first.json()["refresh_token"]}
        )
        assert gone.status_code == 401
        for kept in (second, third, fourth):
            ok = await w.client.post(
                f"{AUTH}/refresh", json={"refresh_token": kept.json()["refresh_token"]}
            )
            assert ok.status_code == 200, ok.text
        (event,) = evictions(w)
        assert event["aggregate_type"] == "user"
        assert event["before"] == {"active_sessions": 3}
        assert event["after"] == {
            "active_sessions": 2,
            "reason": "session_limit",
            "limit": 3,
            "revoked_session_ids": [str(oldest)],
        }
        assert str(event["actor_id"]).startswith("local:")
        assert str(event["actor_id"]) == f"local:{event['aggregate_id']}"


async def test_lowering_the_limit_touches_nothing_until_the_next_sign_in(world: Any) -> None:
    async with world() as w:
        for _ in range(5):
            assert (await w.login()).status_code == 200
        assert len(live_families(w)) == 5
        await w.store({"max_sessions_per_user": 2})
        assert len(live_families(w)) == 5
        assert evictions(w) == []
        assert (await w.login()).status_code == 200
        assert len(live_families(w)) == 2
        (event,) = evictions(w)
        assert event["before"] == {"active_sessions": 5}
        assert event["after"]["active_sessions"] == 1
        assert event["after"]["limit"] == 2
        assert len(event["after"]["revoked_session_ids"]) == 4


async def test_a_sign_in_under_the_limit_writes_no_event_and_others_are_not_counted(
    world: Any, create_user: Any, login: Any
) -> None:
    async with world() as w:
        await w.store({"max_sessions_per_user": 2})
        bob = await create_user(w.client, w.admin, "bob", display_name="Bob")
        for _ in range(2):
            await login(w.client, "bob", bob.password)
        assert (await w.login()).status_code == 200
        assert evictions(w) == []
        assert len(live_families(w)) == 1
        assert len(live_families(w, "bob")) == 2
        # bob's own third sign-in ends bob's oldest, never the admin's
        await login(w.client, "bob", bob.password)
        assert len(live_families(w, "bob")) == 2
        assert len(live_families(w)) == 1
        (event,) = evictions(w)
        assert event["aggregate_id"] == bob.id


async def test_concurrent_sign_ins_at_the_limit_leave_at_most_the_limit(world: Any) -> None:
    async with world() as w:
        await w.store({"max_sessions_per_user": 3})
        results = await asyncio.gather(*(w.login() for _ in range(8)))
        assert all(r.status_code == 200 for r in results)
        assert len(live_families(w)) <= 3
        revoked = sum(len(e["after"]["revoked_session_ids"]) for e in evictions(w))
        assert revoked == 8 - len(live_families(w))


async def test_the_restricted_bootstrap_login_never_evicts(
    make_identity: Any, asgi_client: Any, bootstrap_token: Any, sql: Sql
) -> None:
    env = make_identity(max_sessions_per_user=1)
    async with asgi_client(env.app) as client:
        user = sql(env.db_url, "SELECT id FROM users WHERE username = 'admin'")
        if not user:  # bootstrap runs at startup; the admin row exists once the app is up
            raise AssertionError("bootstrap admin missing")
        for n in range(2):
            sql(
                env.db_url,
                "INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at, "
                "created_by, updated_by) VALUES (%s, gen_random_uuid(), %s, "
                "now() + interval '1 day', 'test', 'test')",
                user[0]["id"],
                f"hash-{n}",
            )
        token = await bootstrap_token(client, env)
        assert token
        rows = sql(env.db_url, "SELECT revoked_at FROM refresh_tokens")
        assert [r["revoked_at"] for r in rows] == [None, None]
        assert sql(env.db_url, "SELECT id FROM outbox WHERE action = 'user.sessions_evicted'") == []
