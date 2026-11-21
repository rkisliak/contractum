"""Deactivating and reactivating a local user (AUTH-08, D-75, D-76, D-34, D-79).

A Global admin deactivates a local user in one audited cascade: the account turns inactive and
every refresh token and PAT it owns is revoked, listed in full in the single ``user.deactivated``
event. The user's access JWT, refresh token and PAT are refused on the very next request, on
identity and through the internal check endpoint every other service calls. Reactivation restores
the account and its grants but never a secret, and (session cutoff) refuses every access JWT and
refresh token issued at or before it.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
LOGIN = f"{IDENTITY}/auth/login"
REFRESH = f"{IDENTITY}/auth/refresh"
FOLDERS = f"{IDENTITY}/folders"
CHECK = "/internal/v1/authz/check"
Sql = Callable[..., list[dict[str, Any]]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _slug(resp: Any) -> str:
    return str(resp.json()["type"]).rsplit(":", 1)[-1]


async def _wait_past_second() -> None:
    """Sleep until the wall clock is past the whole second it reads now (the cutoff's second)."""
    target = int(time.time()) + 1
    await asyncio.sleep(max(0.0, target + 0.05 - time.time()))


@dataclass
class World:
    env: Any
    client: Any
    admin: str
    root: str
    org: str
    team: str
    sql: Sql
    create_user: Callable[..., Awaitable[Any]]
    login: Callable[..., Awaitable[str]]
    grant: Callable[..., Awaitable[Any]]
    make_pat: Callable[..., Awaitable[dict[str, Any]]]

    async def new_user(self, username: str, role: str | None = "team_reader") -> Any:
        user = await self.create_user(self.client, self.admin, username)
        if role is not None:
            granted = await self.grant(self.client, self.admin, self.team, user.principal_id, role)
            assert granted.status_code == 201, granted.text
        return user

    async def session(self, user: Any) -> dict[str, Any]:
        """Log in with the password: the full body (access and refresh token)."""
        resp = await self.client.post(
            LOGIN, json={"username": user.username, "password": user.password}
        )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body

    async def deactivate(self, token: str, user_id: str) -> Any:
        return await self.client.post(
            f"{IDENTITY}/users/{user_id}/deactivate", headers=_bearer(token)
        )

    async def reactivate(self, token: str, user_id: str) -> Any:
        return await self.client.post(
            f"{IDENTITY}/users/{user_id}/reactivate", headers=_bearer(token)
        )

    async def folder(self, token: str, folder_id: str) -> Any:
        return await self.client.get(f"{FOLDERS}/{folder_id}", headers=_bearer(token))

    async def check(self, token: str) -> Any:
        return await self.client.post(
            CHECK,
            json={"permission": "folder:read", "folder_id": self.team},
            headers=_bearer(token),
        )

    async def refresh(self, raw: str) -> Any:
        return await self.client.post(REFRESH, json={"refresh_token": raw})

    def outbox(self, action: str) -> list[dict[str, Any]]:
        return self.sql(
            self.env.db_url,
            "SELECT before, after, actor, aggregate_id FROM outbox WHERE action = %s ORDER BY id",
            action,
        )

    def unrevoked_refresh_ids(self, user_id: str) -> list[str]:
        rows = self.sql(
            self.env.db_url,
            "SELECT id FROM refresh_tokens WHERE user_id = %s AND revoked_at IS NULL ORDER BY id",
            user_id,
        )
        return [str(r["id"]) for r in rows]

    def credential_ids(self, user_id: str) -> list[str]:
        rows = self.sql(
            self.env.db_url,
            "SELECT id FROM api_credentials WHERE local_user_id = %s ORDER BY id",
            user_id,
        )
        return [str(r["id"]) for r in rows]


@pytest.fixture
def open_world(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: Callable[[Any, Any], Awaitable[str]],
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    grant: Callable[..., Awaitable[Any]],
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
    sql: Sql,
) -> Callable[..., Any]:
    @asynccontextmanager
    async def _open(**overrides: object) -> AsyncIterator[World]:
        env = make_identity(**overrides)
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
            org, _project, team = await folder_chain(client, admin, root, "dz")
            yield World(
                env=env,
                client=client,
                admin=admin,
                root=root,
                org=org,
                team=team,
                sql=sql,
                create_user=create_user,
                login=login,
                grant=grant,
                make_pat=create_pat,
            )

    return _open


async def test_deactivation_stops_jwt_refresh_token_and_pat_in_one_event(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        u = await w.new_user("uma")
        body = await w.session(u)
        jwt, refresh = body["access_token"], body["refresh_token"]
        pat = await w.make_pat(w.client, jwt)
        assert (await w.folder(jwt, w.team)).status_code == 200
        assert (await w.folder(pat["token"], w.team)).status_code == 200
        refresh_before = w.unrevoked_refresh_ids(u.id)
        credentials_before = w.credential_ids(u.id)
        assert len(refresh_before) == 1 and len(credentials_before) == 1

        resp = await w.deactivate(w.admin, u.id)
        events = w.outbox("user.deactivated")
        wire = json.dumps(events, default=str)
        folder_by_jwt = await w.folder(jwt, w.team)  # first request after the commit, no sleep
        check_by_jwt = await w.check(jwt)
        by_refresh = await w.refresh(refresh)
        folder_by_pat = await w.folder(pat["token"], w.team)

    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False
    assert len(events) == 1
    revoked = events[0]["after"]["revoked"]
    assert revoked == {
        "refresh_tokens": 1,
        "refresh_token_ids": refresh_before,
        "credentials": 1,
        "credential_ids": credentials_before,
    }
    assert events[0]["after"]["is_active"] is False and events[0]["before"]["is_active"] is True
    _, public_id, secret = pat["token"].split("_", 2)
    assert pat["token"] not in wire and public_id not in wire and secret not in wire
    assert refresh not in wire
    assert folder_by_jwt.status_code == 401, folder_by_jwt.text
    assert check_by_jwt.status_code == 401, check_by_jwt.text
    assert by_refresh.status_code == 401, by_refresh.text
    assert folder_by_pat.status_code == 401, folder_by_pat.text


async def test_the_event_lists_every_one_of_25_pats_and_3_refresh_tokens(
    open_world: Callable[..., Any],
) -> None:
    async with open_world(max_active_tokens=1000) as w:
        v = await w.new_user("vera")
        sessions = [await w.session(v) for _ in range(3)]
        pats = [
            await w.make_pat(w.client, sessions[0]["access_token"], name=f"p{i}") for i in range(25)
        ]
        refresh_before = w.unrevoked_refresh_ids(v.id)
        credentials_before = w.credential_ids(v.id)
        resp = await w.deactivate(w.admin, v.id)
        events = w.outbox("user.deactivated")
        pat_statuses = [(await w.folder(p["token"], w.team)).status_code for p in pats]
        refresh_statuses = [(await w.refresh(s["refresh_token"])).status_code for s in sessions]

    assert resp.status_code == 200, resp.text
    assert len(events) == 1
    revoked = events[0]["after"]["revoked"]
    assert revoked["credentials"] == 25 and len(credentials_before) == 25
    assert revoked["credential_ids"] == credentials_before
    assert revoked["refresh_tokens"] == 3 and len(refresh_before) == 3
    assert revoked["refresh_token_ids"] == refresh_before
    assert set(pat_statuses) == {401} and set(refresh_statuses) == {401}


async def test_rotated_and_expired_unrevoked_refresh_tokens_are_listed_too(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        user = await w.new_user("walt")
        a = await w.session(user)
        b = await w.session(user)
        rotated = await w.refresh(a["refresh_token"])  # A is rotated but unrevoked, A2 issued
        assert rotated.status_code == 200, rotated.text
        w.sql(
            w.env.db_url,
            "UPDATE refresh_tokens SET expires_at = now() - interval '1 hour' "
            "WHERE token_hash = encode(sha256(convert_to(%s::text, 'UTF8')), 'hex')",
            b["refresh_token"],
        )
        expected = w.unrevoked_refresh_ids(user.id)
        assert len(expected) == 3
        resp = await w.deactivate(w.admin, user.id)
        events = w.outbox("user.deactivated")
        left = w.unrevoked_refresh_ids(user.id)

    assert resp.status_code == 200, resp.text
    assert len(events) == 1
    revoked = events[0]["after"]["revoked"]
    assert revoked["refresh_token_ids"] == expected
    assert revoked["refresh_tokens"] == len(expected) == 3
    assert left == []


async def test_deactivate_and_reactivate_are_idempotent_and_never_revive_secrets(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        u = await w.new_user("ivan")
        body = await w.session(u)
        pat = await w.make_pat(w.client, body["access_token"])
        first = await w.deactivate(w.admin, u.id)
        again = await w.deactivate(w.admin, u.id)
        deactivated_events = w.outbox("user.deactivated")
        reactivated = await w.reactivate(w.admin, u.id)
        reactivated_events = w.outbox("user.reactivated")
        await _wait_past_second()
        fresh_jwt = await w.login(w.client, u.username, u.password)
        fresh = await w.client.post(LOGIN, json={"username": u.username, "password": u.password})
        reads_team = await w.folder(fresh_jwt, w.team)
        rotated = await w.refresh(fresh.json()["refresh_token"])
        old_pat = await w.folder(pat["token"], w.team)
        old_refresh = await w.refresh(body["refresh_token"])
        reactivated_again = await w.reactivate(w.admin, u.id)
        reactivated_events_after = w.outbox("user.reactivated")

    assert first.status_code == 200 and first.json()["is_active"] is False
    assert again.status_code == 200 and again.json()["is_active"] is False
    assert len(deactivated_events) == 1
    assert reactivated.status_code == 200 and reactivated.json()["is_active"] is True
    assert len(reactivated_events) == 1
    assert reads_team.status_code == 200, reads_team.text  # the grant is restored
    assert rotated.status_code == 200, rotated.text
    assert old_pat.status_code == 401
    assert old_refresh.status_code == 401
    assert reactivated_again.status_code == 200 and reactivated_again.json()["is_active"] is True
    assert len(reactivated_events_after) == 1


async def test_the_last_active_global_admin_cannot_be_deactivated(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        admin_id = str(
            w.sql(w.env.db_url, "SELECT id FROM users WHERE username = 'admin'")[0]["id"]
        )
        refused = await w.deactivate(w.admin, admin_id)
        still = await w.folder(w.admin, w.team)

        g = await w.new_user("gina", role=None)
        granted = await w.grant(w.client, w.admin, w.root, g.principal_id, "global_admin")
        assert granted.status_code == 201, granted.text
        g_jwt = await w.login(w.client, g.username, g.password)
        assert (await w.folder(g_jwt, w.team)).status_code == 200
        gone = await w.deactivate(w.admin, g.id)
        g_after = await w.folder(g_jwt, w.team)

        reactivated = await w.reactivate(w.admin, g.id)
        assert reactivated.status_code == 200
        await _wait_past_second()
        g_fresh = await w.login(w.client, g.username, g.password)
        by_g = await w.deactivate(g_fresh, admin_id)
        g_still = await w.folder(g_fresh, w.team)

    assert refused.status_code == 409, refused.text
    assert _slug(refused) == "last-global-admin"
    assert still.status_code == 200
    assert gone.status_code == 200 and g_after.status_code == 401
    assert by_g.status_code == 200, by_g.text
    assert g_still.status_code == 200


async def test_only_an_interactive_global_admin_may_deactivate_or_reactivate(
    open_world: Callable[..., Any],
) -> None:
    import uuid

    async with open_world() as w:
        target = await w.new_user("tara")
        oa = await w.new_user("olga", role=None)
        granted = await w.grant(w.client, w.admin, w.org, oa.principal_id, "org_admin")
        assert granted.status_code == 201, granted.text
        oa_jwt = await w.login(w.client, oa.username, oa.password)
        admin_pat = (await w.make_pat(w.client, w.admin, name="adm"))["token"]
        by_org_admin = await w.deactivate(oa_jwt, target.id)
        reactivate_by_org_admin = await w.reactivate(oa_jwt, target.id)
        by_pat = await w.deactivate(admin_pat, target.id)
        reactivate_by_pat = await w.reactivate(admin_pat, target.id)
        unknown = await w.deactivate(w.admin, str(uuid.uuid4()))
        unknown_re = await w.reactivate(w.admin, str(uuid.uuid4()))
        anonymous = await w.client.post(f"{IDENTITY}/users/{target.id}/deactivate")
        state = w.sql(w.env.db_url, "SELECT is_active FROM users WHERE id = %s", target.id)[0]
        events = w.outbox("user.deactivated")

    assert by_org_admin.status_code == 403 and reactivate_by_org_admin.status_code == 403
    assert by_pat.status_code == 403 and _slug(by_pat) == "interactive-session-required"
    assert reactivate_by_pat.status_code == 403
    assert _slug(reactivate_by_pat) == "interactive-session-required"
    assert unknown.status_code == 404 and unknown_re.status_code == 404
    assert anonymous.status_code == 401
    assert state["is_active"] is True and events == []


# --- Session cutoff: nothing issued at or before a reactivation works after it (D-75, T-04-53) ----


async def _escaped_refresh_token(w: World, user_id: str) -> str:
    """A refresh token minted for an inactive user in its own unit of work (a login or refresh
    that raced the deactivation commit and so escaped its revocation)."""
    import uuid

    from dc_core.uow import UnitOfWork
    from dc_identity.models import OutboxEvent, User
    from dc_identity.sessions import SESSION_SYSTEM_ACTOR, issue_refresh_token
    from sqlalchemy import select

    uow = UnitOfWork(
        w.env.app.state.session_factory,
        actor=SESSION_SYSTEM_ACTOR,
        outbox_model=OutboxEvent,
        service=w.env.settings.service,
    )
    async with uow:
        user = (
            await uow.session.execute(select(User).where(User.id == uuid.UUID(user_id)))
        ).scalar_one()
        raw = await issue_refresh_token(uow.session, user, ttl_s=w.env.settings.refresh_token_ttl_s)
        await uow.commit()
    return raw


async def test_after_a_reactivation_no_older_jwt_or_refresh_token_works(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        s = await w.new_user("sven")
        body = await w.session(s)
        jwt, revoked_refresh = body["access_token"], body["refresh_token"]
        assert (await w.folder(jwt, w.team)).status_code == 200
        assert (await w.deactivate(w.admin, s.id)).status_code == 200
        escaped = await _escaped_refresh_token(w, s.id)
        assert (await w.reactivate(w.admin, s.id)).status_code == 200
        cutoff = w.sql(w.env.db_url, "SELECT sessions_valid_after FROM users WHERE id = %s", s.id)
        old_folder = await w.folder(jwt, w.team)
        old_check = await w.check(jwt)
        by_revoked = await w.refresh(revoked_refresh)
        by_escaped = await w.refresh(escaped)
        await _wait_past_second()
        fresh = await w.session(s)
        fresh_folder = await w.folder(fresh["access_token"], w.team)
        fresh_check = await w.check(fresh["access_token"])
        rotated = await w.refresh(fresh["refresh_token"])

    assert cutoff[0]["sessions_valid_after"] is not None
    assert old_folder.status_code == 401, old_folder.text
    assert old_check.status_code == 401, old_check.text
    assert by_revoked.status_code == 401, by_revoked.text
    assert by_escaped.status_code == 401, by_escaped.text
    assert fresh_folder.status_code == 200, fresh_folder.text
    assert fresh_check.status_code == 200, fresh_check.text
    assert rotated.status_code == 200, rotated.text


async def test_a_user_that_was_never_deactivated_keeps_its_jwt_and_refresh_token(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        n = await w.new_user("nina")
        body = await w.session(n)
        cutoff = w.sql(w.env.db_url, "SELECT sessions_valid_after FROM users WHERE id = %s", n.id)
        folder = await w.folder(body["access_token"], w.team)
        rotated = await w.refresh(body["refresh_token"])

    assert cutoff[0]["sessions_valid_after"] is None
    assert folder.status_code == 200 and rotated.status_code == 200


async def test_the_cutoff_column_exists_on_users_entra_users_and_registrations(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        rows = w.sql(
            w.env.db_url,
            "SELECT table_name, is_nullable, data_type FROM information_schema.columns "
            "WHERE column_name = 'sessions_valid_after' ORDER BY table_name",
        )

    assert [(r["table_name"], r["is_nullable"], r["data_type"]) for r in rows] == [
        ("entra_registrations", "YES", "timestamp with time zone"),
        ("entra_users", "YES", "timestamp with time zone"),
        ("users", "YES", "timestamp with time zone"),
    ]


# --- Entra users and service principals (Plan 14: D-75, D-76, T-04-56, T-04-60) -------------------

REVOKED_KEYS = {"refresh_tokens", "refresh_token_ids", "credentials", "credential_ids"}


def _guid() -> str:
    import uuid

    return str(uuid.uuid4())


def _entra_id(object_id: str) -> str:
    return f"entra:{ENTRA_TENANT_ID}:{object_id}"


def _register_group_sql(w: World, owner: str, group: str, name: str = "pipelines") -> None:
    w.env.directory.add_group(group, name)
    w.sql(
        w.env.db_url,
        "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
        "display_name, created_by, updated_by) VALUES ('group', %s, %s, %s, %s, 'test', 'test')",
        ENTRA_TENANT_ID,
        group,
        owner,
        name,
    )


async def _post(w: World, path: str, token: str | None = None) -> Any:
    headers = _bearer(token) if token is not None else {}
    return await w.client.post(f"{IDENTITY}{path}", headers=headers)


async def _sp_folder(w: World, issuer: FakeIssuer, object_id: str, folder: str) -> Any:
    token = issuer.mint_entra(object_id, token_type="app")  # noqa: S106
    return await w.client.get(f"{FOLDERS}/{folder}", headers=_bearer(token))


def _registration_id(w: World, object_id: str) -> str:
    rows = w.sql(
        w.env.db_url,
        "SELECT id FROM entra_registrations WHERE kind = 'service_principal' AND object_id = %s",
        object_id,
    )
    return str(rows[0]["id"])


async def test_deactivating_an_entra_user_is_immediate_even_with_a_warm_directory_cache(
    open_world: Callable[..., Any],
    fake_issuer: FakeIssuer,
    grant_sql: Callable[..., None],
) -> None:
    alice, group = _guid(), _guid()
    async with open_world(directory_cache_ttl_s=60) as w:
        _register_group_sql(w, w.root, group, "engineers")
        w.env.directory.add_principal("user", alice, groups=(group,))
        grant_sql(w.env.db_url, _entra_id(alice), "team_reader", w.team)
        jwt = fake_issuer.mint_entra(alice)
        assert (await w.folder(jwt, w.team)).status_code == 200  # warms the cache, creates the row
        pat = await w.make_pat(w.client, jwt)
        assert (await w.folder(pat["token"], w.team)).status_code == 200
        row = w.sql(w.env.db_url, "SELECT id FROM entra_users WHERE object_id = %s", alice)[0]
        entra_user_id = str(row["id"])
        credential_ids = [
            str(r["id"])
            for r in w.sql(
                w.env.db_url,
                "SELECT id FROM api_credentials WHERE entra_user_id = %s ORDER BY id",
                entra_user_id,
            )
        ]
        assert len(credential_ids) == 1

        resp = await _post(w, f"/entra/users/{entra_user_id}/deactivate", w.admin)
        events = w.outbox("entra_user.deactivated")
        wire = json.dumps(events, default=str)
        by_jwt = await w.folder(jwt, w.team)  # first request after the commit: no Graph wait
        by_pat = await w.folder(pat["token"], w.team)
        again = await _post(w, f"/entra/users/{entra_user_id}/deactivate", w.admin)
        events_after_again = w.outbox("entra_user.deactivated")

    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False and resp.json()["id"] == entra_user_id
    assert len(events) == 1
    assert set(events[0]["after"]["revoked"]) == REVOKED_KEYS
    assert events[0]["after"]["revoked"] == {
        "refresh_tokens": 0,
        "refresh_token_ids": [],
        "credentials": 1,
        "credential_ids": credential_ids,
    }
    assert events[0]["after"]["is_active"] is False and events[0]["before"]["is_active"] is True
    _, public_id, secret = pat["token"].split("_", 2)
    assert pat["token"] not in wire and public_id not in wire and secret not in wire
    assert by_jwt.status_code == 401, by_jwt.text
    assert by_pat.status_code == 401, by_pat.text
    assert again.status_code == 200 and again.json()["is_active"] is False
    assert len(events_after_again) == 1


async def test_a_reactivated_entra_user_needs_a_token_issued_after_it_and_never_regains_its_pat(
    open_world: Callable[..., Any],
    fake_issuer: FakeIssuer,
    grant_sql: Callable[..., None],
) -> None:
    alice, group = _guid(), _guid()
    async with open_world(directory_cache_ttl_s=60) as w:
        _register_group_sql(w, w.root, group, "engineers")
        w.env.directory.add_principal("user", alice, groups=(group,))
        grant_sql(w.env.db_url, _entra_id(alice), "team_reader", w.team)
        old = fake_issuer.mint_entra(alice)
        assert (await w.folder(old, w.team)).status_code == 200
        pat = await w.make_pat(w.client, old)
        eid = str(
            w.sql(w.env.db_url, "SELECT id FROM entra_users WHERE object_id = %s", alice)[0]["id"]
        )
        assert (await _post(w, f"/entra/users/{eid}/deactivate", w.admin)).status_code == 200
        reactivated = await _post(w, f"/entra/users/{eid}/reactivate", w.admin)
        events = w.outbox("entra_user.reactivated")
        cutoff = w.sql(
            w.env.db_url, "SELECT sessions_valid_after FROM entra_users WHERE id = %s", eid
        )
        old_after = await w.folder(old, w.team)
        await _wait_past_second()
        fresh = fake_issuer.mint_entra(alice)
        fresh_after = await w.folder(fresh, w.team)
        old_pat = await w.folder(pat["token"], w.team)
        old_still = await w.folder(old, w.team)
        again = await _post(w, f"/entra/users/{eid}/reactivate", w.admin)
        events_again = w.outbox("entra_user.reactivated")

    assert reactivated.status_code == 200 and reactivated.json()["is_active"] is True
    assert len(events) == 1 and len(events_again) == 1
    assert cutoff[0]["sessions_valid_after"] is not None
    assert old_after.status_code == 401, old_after.text
    assert fresh_after.status_code == 200, fresh_after.text
    assert old_pat.status_code == 401
    assert old_still.status_code == 401
    assert again.status_code == 200


async def test_only_an_interactive_global_admin_deactivates_an_entra_user(
    open_world: Callable[..., Any],
    fake_issuer: FakeIssuer,
    grant_sql: Callable[..., None],
) -> None:
    import uuid

    alice, group = _guid(), _guid()
    async with open_world() as w:
        _register_group_sql(w, w.root, group, "engineers")
        w.env.directory.add_principal("user", alice, groups=(group,))
        assert (await w.folder(fake_issuer.mint_entra(alice), w.team)).status_code == 404
        eid = str(
            w.sql(w.env.db_url, "SELECT id FROM entra_users WHERE object_id = %s", alice)[0]["id"]
        )
        oa = await w.new_user("olga", role=None)
        assert (
            await w.grant(w.client, w.admin, w.org, oa.principal_id, "org_admin")
        ).status_code == 201
        oa_jwt = await w.login(w.client, oa.username, oa.password)
        admin_pat = (await w.make_pat(w.client, w.admin, name="adm"))["token"]
        paths = [f"/entra/users/{eid}/deactivate", f"/entra/users/{eid}/reactivate"]
        by_org_admin = [await _post(w, p, oa_jwt) for p in paths]
        by_pat = [await _post(w, p, admin_pat) for p in paths]
        anonymous = await _post(w, paths[0])
        unknown = [await _post(w, p.replace(eid, str(uuid.uuid4())), w.admin) for p in paths]
        state = w.sql(w.env.db_url, "SELECT is_active FROM entra_users WHERE id = %s", eid)[0]
        events = w.outbox("entra_user.deactivated")

    assert [r.status_code for r in by_org_admin] == [403, 403]
    assert [r.status_code for r in by_pat] == [403, 403]
    assert {_slug(r) for r in by_pat} == {"interactive-session-required"}
    assert anonymous.status_code == 401
    assert [r.status_code for r in unknown] == [404, 404]
    assert state["is_active"] is True and events == []


async def test_deactivating_a_directly_registered_service_principal_beats_a_registered_group(
    open_world: Callable[..., Any],
    fake_issuer: FakeIssuer,
    grant_sql: Callable[..., None],
) -> None:
    sp1, group = _guid(), _guid()
    async with open_world() as w:
        _register_group_sql(w, w.root, group)
        w.env.directory.add_principal("service_principal", sp1, groups=(group,))
        registered = await w.client.post(
            f"{IDENTITY}/entra/service-principals",
            json={"owner_folder_id": w.root, "object_id": sp1},
            headers=_bearer(w.admin),
        )
        assert registered.status_code == 201, registered.text
        grant_sql(w.env.db_url, _entra_id(sp1), "consumer", w.team, "service_principal")
        rid = registered.json()["id"]
        old = fake_issuer.mint_entra(sp1, token_type="app")  # noqa: S106
        assert (await w.folder(old, w.team)).status_code == 200

        resp = await _post(w, f"/entra/service-principals/{rid}/deactivate", w.admin)
        events = w.outbox("entra_registration.deactivated")
        by_old = await w.folder(old, w.team)
        by_new = await _sp_folder(w, fake_issuer, sp1, w.team)
        again = await _post(w, f"/entra/service-principals/{rid}/deactivate", w.admin)
        events_again = w.outbox("entra_registration.deactivated")
        reactivated = await _post(w, f"/entra/service-principals/{rid}/reactivate", w.admin)
        reactivated_events = w.outbox("entra_registration.reactivated")
        cutoff = w.sql(
            w.env.db_url, "SELECT sessions_valid_after FROM entra_registrations WHERE id = %s", rid
        )
        old_after = await w.folder(old, w.team)
        await _wait_past_second()
        fresh_after = await _sp_folder(w, fake_issuer, sp1, w.team)
        again_re = await _post(w, f"/entra/service-principals/{rid}/reactivate", w.admin)
        reactivated_events_again = w.outbox("entra_registration.reactivated")

    assert resp.status_code == 200 and resp.json()["is_active"] is False, resp.text
    assert len(events) == 1 and len(events_again) == 1
    assert events[0]["after"]["revoked"] == {
        "refresh_tokens": 0,
        "refresh_token_ids": [],
        "credentials": 0,
        "credential_ids": [],
    }
    assert by_old.status_code == 401 and by_new.status_code == 401
    assert again.status_code == 200
    assert reactivated.status_code == 200 and reactivated.json()["is_active"] is True
    assert len(reactivated_events) == 1 and len(reactivated_events_again) == 1
    assert cutoff[0]["sessions_valid_after"] is not None
    assert old_after.status_code == 401, old_after.text
    assert fresh_after.status_code == 200, fresh_after.text
    assert again_re.status_code == 200


async def test_deactivating_a_just_in_time_service_principal_row_blocks_it_despite_the_group(
    open_world: Callable[..., Any],
    fake_issuer: FakeIssuer,
    grant_sql: Callable[..., None],
) -> None:
    sp2, group = _guid(), _guid()
    async with open_world() as w:
        oa = await w.new_user("oona", role=None)
        ob = await w.new_user("ben", role=None)
        other_org, _p, _t = await _second_org(w)
        assert (
            await w.grant(w.client, w.admin, w.org, oa.principal_id, "org_admin")
        ).status_code == 201
        assert (
            await w.grant(w.client, w.admin, other_org, ob.principal_id, "org_admin")
        ).status_code == 201
        oa_jwt = await w.login(w.client, oa.username, oa.password)
        ob_jwt = await w.login(w.client, ob.username, ob.password)
        _register_group_sql(w, w.org, group)
        grant_sql(w.env.db_url, group, "team_reader", w.team, "entra_group")
        w.env.directory.add_principal("service_principal", sp2, groups=(group,))
        old = fake_issuer.mint_entra(sp2, token_type="app")  # noqa: S106
        assert (await w.folder(old, w.team)).status_code == 200  # creates the just-in-time row
        rid = _registration_id(w, sp2)
        direct = w.sql(
            w.env.db_url, "SELECT directly_registered FROM entra_registrations WHERE id = %s", rid
        )[0]["directly_registered"]
        assert direct is False

        by_other_org = await _post(w, f"/entra/service-principals/{rid}/deactivate", ob_jwt)
        resp = await _post(w, f"/entra/service-principals/{rid}/deactivate", oa_jwt)
        events = w.outbox("entra_registration.deactivated")
        blocked = await w.folder(old, w.team)
        reactivated = await _post(w, f"/entra/service-principals/{rid}/reactivate", oa_jwt)
        old_after = await w.folder(old, w.team)
        await _wait_past_second()
        fresh_after = await _sp_folder(w, fake_issuer, sp2, w.team)

    assert by_other_org.status_code == 404, by_other_org.text
    assert resp.status_code == 200 and resp.json()["is_active"] is False, resp.text
    assert len(events) == 1
    assert set(events[0]["after"]["revoked"]) == REVOKED_KEYS
    assert blocked.status_code == 401, blocked.text
    assert reactivated.status_code == 200
    assert old_after.status_code == 401, old_after.text
    assert fresh_after.status_code == 200, fresh_after.text


async def _second_org(w: World) -> tuple[str, str, str]:
    resp = await w.client.post(
        FOLDERS,
        json={"parent_id": w.root, "slug": "other-org", "display_name": "Other"},
        headers=_bearer(w.admin),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"]), "", ""


async def test_service_principal_routes_refuse_pats_unknown_ids_and_group_rows(
    open_world: Callable[..., Any],
    fake_issuer: FakeIssuer,
) -> None:
    import uuid

    sp1, group = _guid(), _guid()
    async with open_world() as w:
        oa = await w.new_user("opal", role=None)
        ob = await w.new_user("bert", role=None)
        other_org, _p, _t = await _second_org(w)
        assert (
            await w.grant(w.client, w.admin, w.org, oa.principal_id, "org_admin")
        ).status_code == 201
        assert (
            await w.grant(w.client, w.admin, other_org, ob.principal_id, "org_admin")
        ).status_code == 201
        oa_jwt = await w.login(w.client, oa.username, oa.password)
        ob_jwt = await w.login(w.client, ob.username, ob.password)
        oa_pat = (await w.make_pat(w.client, oa_jwt))["token"]
        ob_pat = (await w.make_pat(w.client, ob_jwt))["token"]
        w.env.directory.add_principal("service_principal", sp1)
        registered = await w.client.post(
            f"{IDENTITY}/entra/service-principals",
            json={"owner_folder_id": w.org, "object_id": sp1},
            headers=_bearer(w.admin),
        )
        assert registered.status_code == 201, registered.text
        rid = registered.json()["id"]
        _register_group_sql(w, w.org, group)
        group_rid = str(
            w.sql(w.env.db_url, "SELECT id FROM entra_registrations WHERE object_id = %s", group)[
                0
            ]["id"]
        )
        paths = [
            f"/entra/service-principals/{rid}/deactivate",
            f"/entra/service-principals/{rid}/reactivate",
        ]
        owner_pat = [await _post(w, p, oa_pat) for p in paths]
        other_pat = [await _post(w, p, ob_pat) for p in paths]
        other_jwt = [await _post(w, p, ob_jwt) for p in paths]
        unknown = [await _post(w, p.replace(rid, str(uuid.uuid4())), w.admin) for p in paths]
        group_row = [await _post(w, p.replace(rid, group_rid), w.admin) for p in paths]
        anonymous = await _post(w, paths[0])
        state = w.sql(w.env.db_url, "SELECT is_active FROM entra_registrations WHERE id = %s", rid)[
            0
        ]
        events = w.outbox("entra_registration.deactivated")

    assert [r.status_code for r in owner_pat] == [403, 403]
    assert {_slug(r) for r in owner_pat} == {"interactive-session-required"}
    assert [r.status_code for r in other_pat] == [404, 404]
    assert [r.status_code for r in other_jwt] == [404, 404]
    assert [r.status_code for r in unknown] == [404, 404]
    assert [r.status_code for r in group_row] == [404, 404]
    assert anonymous.status_code == 401
    assert state["is_active"] is True and events == []


# --- Service accounts (Plan 14: D-75, D-76, D-79, T-04-56, T-04-57) -----------------------------

SERVICE_ACCOUNTS = f"{IDENTITY}/service-accounts"


def _expiry(days: float = 30) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


async def _new_key(w: World, sa_id: str, name: str, token: str | None = None) -> Any:
    return await w.client.post(
        f"{SERVICE_ACCOUNTS}/{sa_id}/keys",
        json={"name": name, "expires_at": _expiry()},
        headers=_bearer(token or w.admin),
    )


async def test_deactivating_a_service_account_revokes_every_key_for_good(
    open_world: Callable[..., Any],
    create_service_account: Callable[..., Awaitable[dict[str, Any]]],
    grant_sql: Callable[..., None],
) -> None:
    async with open_world() as w:
        oa = await w.new_user("odin", role=None)
        ob = await w.new_user("borys", role=None)
        other_org, _p, _t = await _second_org(w)
        assert (
            await w.grant(w.client, w.admin, w.org, oa.principal_id, "org_admin")
        ).status_code == 201
        assert (
            await w.grant(w.client, w.admin, other_org, ob.principal_id, "org_admin")
        ).status_code == 201
        oa_jwt = await w.login(w.client, oa.username, oa.password)
        ob_jwt = await w.login(w.client, ob.username, ob.password)
        sa = await create_service_account(w.client, w.admin, w.org, "bot")
        sa_id = sa["id"]
        grant_sql(w.env.db_url, sa["principal_id"], "consumer", w.team, "service_account")
        k1 = (await _new_key(w, sa_id, "k1")).json()
        k2 = (await _new_key(w, sa_id, "k2")).json()
        assert (await w.folder(k1["token"], w.team)).status_code == 200
        assert (await w.folder(k2["token"], w.team)).status_code == 200
        key_ids = sorted([k1["id"], k2["id"]])

        by_other_org = await _post(w, f"/service-accounts/{sa_id}/deactivate", ob_jwt)
        resp = await _post(w, f"/service-accounts/{sa_id}/deactivate", oa_jwt)
        events = w.outbox("service_account.deactivated")
        wire = json.dumps(events, default=str)
        by_k1 = await w.folder(k1["token"], w.team)
        by_k2 = await w.folder(k2["token"], w.team)
        again = await _post(w, f"/service-accounts/{sa_id}/deactivate", oa_jwt)
        events_again = w.outbox("service_account.deactivated")
        refused_key = await _new_key(w, sa_id, "k3")
        reactivated = await _post(w, f"/service-accounts/{sa_id}/reactivate", oa_jwt)
        reactivated_events = w.outbox("service_account.reactivated")
        old_k1 = await w.folder(k1["token"], w.team)
        old_k2 = await w.folder(k2["token"], w.team)
        new_key = await _new_key(w, sa_id, "k3")
        works = await w.folder(new_key.json()["token"], w.team)
        again_re = await _post(w, f"/service-accounts/{sa_id}/reactivate", oa_jwt)
        reactivated_events_again = w.outbox("service_account.reactivated")

    assert by_other_org.status_code == 404, by_other_org.text
    assert resp.status_code == 200 and resp.json()["is_active"] is False, resp.text
    assert len(events) == 1 and len(events_again) == 1
    assert events[0]["after"]["revoked"] == {
        "refresh_tokens": 0,
        "refresh_token_ids": [],
        "credentials": 2,
        "credential_ids": key_ids,
    }
    for token in (k1["token"], k2["token"]):
        _, public_id, secret = token.split("_", 2)
        assert token not in wire and public_id not in wire and secret not in wire
    assert by_k1.status_code == 401 and by_k2.status_code == 401
    assert again.status_code == 200
    assert refused_key.status_code == 409, refused_key.text
    assert reactivated.status_code == 200 and reactivated.json()["is_active"] is True
    assert len(reactivated_events) == 1 and len(reactivated_events_again) == 1
    assert old_k1.status_code == 401 and old_k2.status_code == 401
    assert new_key.status_code == 201, new_key.text
    assert works.status_code == 200, works.text
    assert again_re.status_code == 200


async def test_a_root_owned_service_account_is_deactivated_by_a_global_admin_only(
    open_world: Callable[..., Any],
    create_service_account: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    async with open_world() as w:
        oa = await w.new_user("orla", role=None)
        assert (
            await w.grant(w.client, w.admin, w.org, oa.principal_id, "org_admin")
        ).status_code == 201
        oa_jwt = await w.login(w.client, oa.username, oa.password)
        sa = await create_service_account(w.client, w.admin, w.root, "root-bot")
        by_org_admin = await _post(w, f"/service-accounts/{sa['id']}/deactivate", oa_jwt)
        by_admin = await _post(w, f"/service-accounts/{sa['id']}/deactivate", w.admin)

    assert by_org_admin.status_code == 404, by_org_admin.text
    assert by_admin.status_code == 200 and by_admin.json()["is_active"] is False


async def test_service_account_routes_refuse_pats_and_unknown_ids(
    open_world: Callable[..., Any],
    create_service_account: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    import uuid

    async with open_world() as w:
        oa = await w.new_user("ola", role=None)
        ob = await w.new_user("bea", role=None)
        other_org, _p, _t = await _second_org(w)
        assert (
            await w.grant(w.client, w.admin, w.org, oa.principal_id, "org_admin")
        ).status_code == 201
        assert (
            await w.grant(w.client, w.admin, other_org, ob.principal_id, "org_admin")
        ).status_code == 201
        oa_jwt = await w.login(w.client, oa.username, oa.password)
        ob_jwt = await w.login(w.client, ob.username, ob.password)
        oa_pat = (await w.make_pat(w.client, oa_jwt))["token"]
        ob_pat = (await w.make_pat(w.client, ob_jwt))["token"]
        sa = await create_service_account(w.client, w.admin, w.org, "pat-bot")
        paths = [
            f"/service-accounts/{sa['id']}/deactivate",
            f"/service-accounts/{sa['id']}/reactivate",
        ]
        owner_pat = [await _post(w, p, oa_pat) for p in paths]
        other_pat = [await _post(w, p, ob_pat) for p in paths]
        unknown = [await _post(w, p.replace(sa["id"], str(uuid.uuid4())), w.admin) for p in paths]
        anonymous = await _post(w, paths[0])
        state = w.sql(
            w.env.db_url, "SELECT is_active FROM service_accounts WHERE id = %s", sa["id"]
        )[0]
        events = w.outbox("service_account.deactivated")

    assert [r.status_code for r in owner_pat] == [403, 403]
    assert {_slug(r) for r in owner_pat} == {"interactive-session-required"}
    assert [r.status_code for r in other_pat] == [404, 404]
    assert [r.status_code for r in unknown] == [404, 404]
    assert anonymous.status_code == 401
    assert state["is_active"] is True and events == []
