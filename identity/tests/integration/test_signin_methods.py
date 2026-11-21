"""Sign-in methods (quick 261120-0ye, D-37): the lockout rules (a) to (d) on PUT and on import, the
enforcement at login, refresh and the gate, the public ``/auth/policy`` and the break-glass env.

The acting admin of each case is chosen for the rule under test: a local Global admin (the
bootstrap admin), an Entra Global admin (a registered Entra user with a ROOT ``global_admin`` grant
and a token from the fake Entra issuer) or a ``fake:`` Global admin, whose principal id is neither
local nor Entra, so only rules (a), (c) and (d) apply to it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer
from dc_identity.main import create_identity_app

pytestmark = pytest.mark.integration

SETTINGS = "/api/v1/identity/settings"
AUTH = "/api/v1/identity/auth"
LOGIN = f"{AUTH}/login"
SLUG = "urn:dc:problem:signin-lockout-risk"
DISABLED = "urn:dc:problem:sign-in-method-disabled"
Sql = Callable[..., list[dict[str, Any]]]
AdminFlow = Callable[[Any, Any], Awaitable[str]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class World:
    env: Any
    client: httpx.AsyncClient
    sql: Sql
    issuer: FakeIssuer
    local_admin: str  # token of the bootstrap admin
    root: str

    def settings_events(self) -> list[dict[str, Any]]:
        return self.sql(
            self.env.db_url,
            "SELECT action, after FROM outbox WHERE action LIKE 'settings.%%' ORDER BY id",
        )

    def register_entra_user(self, *, grant: bool = True, active: bool = True) -> str:
        """An Entra user registration (direct); with ``grant`` also a ROOT ``global_admin``."""
        oid = str(uuid.uuid4())
        self.env.directory.add_principal("user", oid, tenant_id=ENTRA_TENANT_ID)
        self.sql(
            self.env.db_url,
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id,"
            " display_name, is_active, directly_registered, created_by, updated_by)"
            " VALUES ('user', %s, %s, NULL, 'Entra admin', %s, true, 'test', 'test')",
            ENTRA_TENANT_ID,
            oid,
            active,
        )
        if grant:
            self.grant(f"entra:{ENTRA_TENANT_ID}:{oid}", "user")
        return oid

    def grant(self, principal_id: str, principal_type: str, role: str = "global_admin") -> None:
        self.sql(
            self.env.db_url,
            "INSERT INTO role_assignments (folder_id, principal_type, principal_id, role,"
            " created_by, updated_by) VALUES (%s, %s, %s, %s, 'test', 'test')",
            self.root,
            principal_type,
            principal_id,
            role,
        )

    def fake_admin(self) -> str:
        self.grant("fake:ops", "user")
        return self.issuer.mint("ops")

    async def etag(self, token: str, section: str = "security") -> str:
        resp = await self.client.get(SETTINGS, headers=bearer(token))
        assert resp.status_code == 200, resp.text
        return str(next(s["etag"] for s in resp.json()["sections"] if s["section"] == section))

    async def put(self, token: str, body: dict[str, Any], section: str = "security") -> Any:
        return await self.client.put(
            f"{SETTINGS}/{section}",
            json=body,
            headers={**bearer(token), "If-Match": await self.etag(token, section)},
        )

    async def apply_import(self, token: str, sections: dict[str, Any], *, dry_run: bool) -> Any:
        document = json.dumps(
            {"format": "contractum-settings", "format_version": 1, "sections": sections}
        )
        headers = bearer(token)
        if not dry_run:
            listing = await self.client.get(SETTINGS, headers=headers)
            headers["If-Match"] = listing.json()["etag"]
        return await self.client.post(
            f"{SETTINGS}/import?dry_run={'true' if dry_run else 'false'}",
            content=document,
            headers={**headers, "Content-Type": "application/json"},
        )

    def entra_token(self, oid: str) -> str:
        return self.issuer.mint_entra(oid)


@pytest.fixture
def world(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> Callable[..., Any]:
    @asynccontextmanager
    async def _open(**overrides: Any) -> AsyncIterator[World]:
        env = make_identity(**overrides)
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
            yield World(env, client, sql, fake_issuer, admin, root)

    return _open


def problem_type(resp: Any) -> str:
    return str(resp.json()["type"])


# --- the lockout rules ----------------------------------------------------------------------------


async def test_rule_a_both_methods_off_is_refused_with_no_event(world: Any) -> None:
    async with world() as w:
        ops = w.fake_admin()
        resp = await w.put(ops, {"local_signin_enabled": False, "microsoft_signin_enabled": False})
        assert resp.status_code == 422, resp.text
        assert problem_type(resp) == SLUG
        assert resp.json()["errors"][0]["pointer"] == "/local_signin_enabled"
        assert w.settings_events() == []


async def test_rule_b_the_actor_cannot_switch_off_their_own_method(world: Any) -> None:
    async with world() as w:
        oid = w.register_entra_user()
        # a local admin turning local off (an Entra admin exists, so only rule (b) stops it)
        local = await w.put(w.local_admin, {"local_signin_enabled": False})
        assert local.status_code == 409, local.text
        assert problem_type(local) == SLUG
        # an Entra admin turning Microsoft off (a local admin exists, so only rule (b) stops it)
        entra = await w.put(w.entra_token(oid), {"microsoft_signin_enabled": False})
        assert entra.status_code == 409, entra.text
        assert problem_type(entra) == SLUG
        assert w.settings_events() == []


async def test_rule_c_local_off_needs_an_active_entra_global_admin(world: Any) -> None:
    async with world() as w:
        ops = w.fake_admin()
        refused = await w.put(ops, {"local_signin_enabled": False})
        assert refused.status_code == 409, refused.text
        assert problem_type(refused) == SLUG
        # a registration that is deactivated, or has no Global admin grant, does not count
        w.register_entra_user(grant=True, active=False)
        w.register_entra_user(grant=False)
        assert (await w.put(ops, {"local_signin_enabled": False})).status_code == 409
        assert w.settings_events() == []

        w.register_entra_user()
        saved = await w.put(ops, {"local_signin_enabled": False})
        assert saved.status_code == 200, saved.text
        assert [e["action"] for e in w.settings_events()] == ["settings.updated"]


async def test_rule_c_an_entra_group_holding_global_admin_counts(world: Any) -> None:
    async with world() as w:
        ops = w.fake_admin()
        group = str(uuid.uuid4())
        w.sql(
            w.env.db_url,
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id,"
            " display_name, is_active, directly_registered, created_by, updated_by)"
            " VALUES ('group', %s, %s, %s, 'Admins', true, true, 'test', 'test')",
            ENTRA_TENANT_ID,
            group,
            w.root,
        )
        w.grant(group, "entra_group")
        assert (await w.put(ops, {"local_signin_enabled": False})).status_code == 200


async def test_rule_d_microsoft_off_needs_an_active_local_global_admin(world: Any) -> None:
    async with world() as w:
        ops = w.fake_admin()
        w.sql(w.env.db_url, "UPDATE users SET is_active = false")
        refused = await w.put(ops, {"microsoft_signin_enabled": False})
        assert refused.status_code == 409, refused.text
        assert problem_type(refused) == SLUG
        assert w.settings_events() == []
        w.sql(w.env.db_url, "UPDATE users SET is_active = true")
        saved = await w.put(ops, {"microsoft_signin_enabled": False})
        assert saved.status_code == 200, saved.text


async def test_rule_d_a_local_group_holding_global_admin_counts(world: Any) -> None:
    async with world() as w:
        ops = w.fake_admin()
        admin_id = w.sql(w.env.db_url, "SELECT id FROM users WHERE username = 'admin'")[0]["id"]
        group = str(uuid.uuid4())
        w.sql(
            w.env.db_url,
            "INSERT INTO local_groups (id, owner_folder_id, name, created_by, updated_by)"
            " VALUES (%s, %s, 'admins', 'test', 'test')",
            group,
            w.root,
        )
        w.sql(
            w.env.db_url,
            "INSERT INTO group_members (group_id, member_type, member_id, created_by, updated_by)"
            " VALUES (%s, 'user', %s, 'test', 'test')",
            group,
            f"local:{admin_id}",
        )
        # the bootstrap admin loses its direct grant; the group carries it now
        w.sql(
            w.env.db_url,
            "DELETE FROM role_assignments WHERE principal_id = %s",
            f"local:{admin_id}",
        )
        w.grant(group, "local_group")
        assert (await w.put(ops, {"microsoft_signin_enabled": False})).status_code == 200


async def test_the_rules_apply_to_an_import_dry_run_and_apply(world: Any) -> None:
    async with world() as w:
        both = {"security": {"local_signin_enabled": False, "microsoft_signin_enabled": False}}
        dry = await w.apply_import(w.local_admin, both, dry_run=True)
        assert dry.status_code == 422, dry.text
        assert problem_type(dry) == "urn:dc:problem:settings-import-invalid"
        assert dry.json()["errors"][0]["pointer"] == "/sections/security/local_signin_enabled"

        own = {"security": {"local_signin_enabled": False}}
        w.register_entra_user()
        applied = await w.apply_import(w.local_admin, own, dry_run=False)
        assert applied.status_code == 409, applied.text  # rule (b): the importer signs in locally
        assert problem_type(applied) == SLUG
        assert w.settings_events() == []

        oid = w.register_entra_user()
        ok = await w.apply_import(w.entra_token(oid), own, dry_run=False)
        assert ok.status_code == 200, ok.text
        assert [e["action"] for e in w.settings_events()] == ["settings.imported"]


async def test_a_save_that_changes_nothing_about_an_off_method_is_not_a_lockout(world: Any) -> None:
    async with world(signin_local_force_enabled=True) as w:
        oid = w.register_entra_user()
        assert (await w.put(w.entra_token(oid), {"local_signin_enabled": False})).status_code == 200
        # local is forced on, the local admin is signed in locally: saving another field with the
        # method still off is not "turning it off", so it is allowed (the recovery path)
        again = await w.put(
            w.local_admin, {"local_signin_enabled": False, "password_min_length": 14}
        )
        assert again.status_code == 200, again.text


# --- enforcement ----------------------------------------------------------------------------------


async def turn_local_off(w: Any) -> str:
    oid = w.register_entra_user()
    resp = await w.put(w.entra_token(oid), {"local_signin_enabled": False})
    assert resp.status_code == 200, resp.text
    return str(oid)


async def test_local_off_refuses_login_before_the_password_and_counts_no_failure(
    world: Any,
) -> None:
    async with world() as w:
        await turn_local_off(w)
        for password in ("an admin password of 30 chars!", "wrong"):
            resp = await w.client.post(LOGIN, json={"username": "admin", "password": password})
            assert resp.status_code == 403, resp.text
            assert problem_type(resp) == DISABLED
        # an unknown user gets the same answer: no enumeration through the method check
        ghost = await w.client.post(LOGIN, json={"username": "nobody", "password": "x"})
        assert ghost.status_code == 403 and problem_type(ghost) == DISABLED
        assert w.sql(w.env.db_url, "SELECT * FROM login_throttle") == []


async def test_the_force_env_brings_local_sign_in_back(world: Any, fake_issuer: FakeIssuer) -> None:
    async with world() as w:
        await turn_local_off(w)
        recovered = create_identity_app(
            w.env.settings.model_copy(update={"signin_local_force_enabled": True}),
            w.env.auth_settings,
            w.env.directory,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=recovered), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                LOGIN, json={"username": "admin", "password": "an admin password of 30 chars!"}
            )
            assert resp.status_code == 200, resp.text
            policy = await client.get(f"{AUTH}/policy")
            assert policy.json()["methods"]["local"] is True


async def test_a_local_refresh_is_refused_when_local_is_off_and_a_pat_still_works(
    world: Any,
) -> None:
    async with world() as w:
        login = await w.client.post(
            LOGIN, json={"username": "admin", "password": "an admin password of 30 chars!"}
        )
        refresh = login.json()["refresh_token"]
        pat = await w.client.post(
            "/api/v1/identity/pats",
            json={
                "name": "ci",
                "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            },
            headers=bearer(login.json()["access_token"]),
        )
        assert pat.status_code == 201, pat.text
        await turn_local_off(w)

        refused = await w.client.post(f"{AUTH}/refresh", json={"refresh_token": refresh})
        assert refused.status_code == 403, refused.text
        assert problem_type(refused) == DISABLED
        # PATs are not sign-ins: they keep working
        me = await w.client.get("/api/v1/identity/me", headers=bearer(pat.json()["token"]))
        assert me.status_code == 200, me.text


async def test_microsoft_off_refuses_an_entra_user_at_the_gate_and_records_it(world: Any) -> None:
    async with world() as w:
        oid = w.register_entra_user()
        sp = str(uuid.uuid4())
        w.env.directory.add_principal("service_principal", sp, tenant_id=ENTRA_TENANT_ID)
        w.sql(
            w.env.db_url,
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id,"
            " display_name, is_active, directly_registered, created_by, updated_by)"
            " VALUES ('service_principal', %s, %s, %s, 'pipeline', true, true, 'test', 'test')",
            ENTRA_TENANT_ID,
            sp,
            w.root,
        )
        before = await w.client.get("/api/v1/identity/me", headers=bearer(w.entra_token(oid)))
        assert before.status_code == 200, before.text

        saved = await w.put(w.local_admin, {"microsoft_signin_enabled": False})
        assert saved.status_code == 200, saved.text

        refused = await w.client.get("/api/v1/identity/me", headers=bearer(w.entra_token(oid)))
        assert refused.status_code == 403, refused.text
        assert problem_type(refused) == DISABLED
        # a service principal and a local admin are unaffected
        sp_resp = await w.client.get(
            "/api/v1/identity/me",
            headers=bearer(w.issuer.mint_entra(sp, token_type="app")),  # noqa: S106
        )
        assert sp_resp.status_code == 200, sp_resp.text
        assert (
            await w.client.get("/api/v1/identity/me", headers=bearer(w.local_admin))
        ).status_code == 200
        denials = w.sql(
            w.env.db_url,
            "SELECT detail FROM outbox WHERE action = 'check.denied'"
            " AND detail->>'outcome' = 'gate_refused'",
        )
        assert denials, "the gate refusal is recorded like any other gate denial"


# --- the public policy ----------------------------------------------------------------------------


async def test_auth_policy_is_public_carries_no_secret_and_follows_the_settings(
    world: Any,
) -> None:
    async with world() as w:
        anonymous = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=w.env.app), base_url="http://testserver"
        )
        async with anonymous:
            first = await anonymous.get(f"{AUTH}/policy")
            assert first.status_code == 200, first.text
            assert first.headers["cache-control"] == "no-store"
            body = first.json()
            assert body == {
                "methods": {"local": True, "microsoft": True},
                "password_rules": {
                    "min_length": 12,
                    "require_upper": False,
                    "require_lower": False,
                    "require_digit": False,
                    "require_symbol": False,
                },
            }
            await turn_local_off(w)
            second = await anonymous.get(f"{AUTH}/policy")
            assert second.json()["methods"] == {"local": False, "microsoft": True}
            assert set(second.json()) == {"methods", "password_rules"}
