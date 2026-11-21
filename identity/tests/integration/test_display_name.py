"""``PATCH /api/v1/identity/me`` and ``PATCH /api/v1/identity/users/{id}``: display names.

A local user renames themself (interactive sign-in only); an Entra principal is 409 and nothing is
written; every effective change writes exactly one ``user.display_name_changed`` outbox event with
before and after; the current name again is a 200 no-op without an event; the new name is what
``POST /principals/names`` and ``GET /me`` answer straight away.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer

pytestmark = pytest.mark.integration

ME = "/api/v1/identity/me"
NAMES = "/api/v1/identity/principals/names"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _events(sql: Sql, env: Any) -> list[dict[str, Any]]:
    return sql(
        env.db_url,
        "SELECT action, aggregate_type, aggregate_id, before, after, scope_path "
        "FROM outbox WHERE action = 'user.display_name_changed' ORDER BY id",
    )


async def _names(client: Any, token: str, principal_id: str) -> str:
    resp = await client.post(NAMES, json={"ids": [principal_id]}, headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    return str(resp.json()["items"][0]["display_name"])


async def test_a_local_user_renames_themself_with_one_audited_write(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        ana = await create_user(client, admin, "ana", display_name="Ana")
        token = await login(client, ana.username, ana.password)
        resp = await client.patch(
            ME, json={"display_name": "  Ana Smith  "}, headers=_bearer(token)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["display_name"] == "Ana Smith"
        assert body["username"] == "ana"
        assert body["principal_id"] == ana.principal_id
        assert set(body) == {"principal_id", "display_name", "username", "updated_at"}

        me = await client.get(ME, headers=_bearer(token))
        assert me.json()["display_name"] == "Ana Smith"
        assert await _names(client, admin, ana.principal_id) == "Ana Smith"

    events = _events(sql, env)
    assert len(events) == 1
    event = events[0]
    assert event["aggregate_type"] == "user"
    assert str(event["aggregate_id"]) == ana.id
    assert event["before"]["display_name"] == "Ana"
    assert event["after"]["display_name"] == "Ana Smith"
    root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
    assert [str(item) for item in event["scope_path"]] == [str(root)]
    row = sql(env.db_url, "SELECT updated_by FROM users WHERE id = %s", ana.id)[0]
    assert row["updated_by"] == ana.principal_id
    assert not {"password_hash", "email"} & set(event["after"])


async def test_the_current_name_again_is_a_noop_without_an_event(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        ana = await create_user(client, admin, "ana", display_name="Ana")
        token = await login(client, ana.username, ana.password)
        first = await client.patch(ME, json={"display_name": "Ana Smith"}, headers=_bearer(token))
        stamp = sql(env.db_url, "SELECT updated_at FROM users WHERE id = %s", ana.id)[0]
        again = await client.patch(ME, json={"display_name": " Ana Smith "}, headers=_bearer(token))
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert len(_events(sql, env)) == 1
    assert sql(env.db_url, "SELECT updated_at FROM users WHERE id = %s", ana.id)[0] == stamp


async def test_refusals_without_a_name_change_or_an_event(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    bootstrap_token: Callable[[Any, Any], Awaitable[str]],
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
    create_service_account: Callable[..., Awaitable[dict[str, Any]]],
    sql: Sql,
) -> None:
    env = make_identity()
    body = {"display_name": "Mallory"}
    async with asgi_client(env.app) as client:
        restricted = await bootstrap_token(client, env)
        gated = await client.patch(ME, json=body, headers=_bearer(restricted))
        assert gated.status_code == 403, gated.text
        assert gated.json()["type"] == "urn:dc:problem:password-rotation-required"

        admin = await admin_token(client, env)
        ana = await create_user(client, admin, "ana", display_name="Ana")
        token = await login(client, ana.username, ana.password)
        anonymous = await client.patch(ME, json=body)
        assert anonymous.status_code == 401, anonymous.text

        pat = await create_pat(client, token)
        by_pat = await client.patch(ME, json=body, headers=_bearer(pat["token"]))
        assert by_pat.status_code == 403, by_pat.text
        assert by_pat.json()["type"] == "urn:dc:problem:interactive-session-required"

        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        account = await create_service_account(client, admin, root, "bot")
        key = await client.post(
            f"/api/v1/identity/service-accounts/{account['id']}/keys",
            json={"name": "k", "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            headers=_bearer(admin),
        )
        assert key.status_code == 201, key.text
        by_key = await client.patch(ME, json=body, headers=_bearer(key.json()["token"]))
        assert by_key.status_code == 403, by_key.text
        assert by_key.json()["type"] == "urn:dc:problem:interactive-session-required"

        assert await _names(client, admin, ana.principal_id) == "Ana"
    assert _events(sql, env) == []


async def test_an_entra_user_gets_409_and_nothing_is_written(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
) -> None:
    env = make_identity()
    oid = str(uuid.uuid4())
    env.directory.add_principal("user", oid, tenant_id=ENTRA_TENANT_ID, groups=())
    sql(
        env.db_url,
        "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
        "display_name, is_active, directly_registered, created_by, updated_by) "
        "VALUES ('user', %s, %s, NULL, 'Entra Eve', true, true, 'test', 'test')",
        ENTRA_TENANT_ID,
        oid,
    )
    async with asgi_client(env.app) as client:
        await admin_token(client, env)
        token = fake_issuer.mint_entra(oid, name="Entra Eve")
        resp = await client.patch(ME, json={"display_name": "Eve Evil"}, headers=_bearer(token))
    assert resp.status_code == 409, resp.text
    problem = resp.json()
    assert problem["type"] == "urn:dc:problem:display-name-managed-externally"
    assert "Eve Evil" not in resp.text
    assert _events(sql, env) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"display_name": "bad\u202ename"},
        {"display_name": "line\nbreak"},
        {"display_name": "   "},
        {"display_name": "x" * 201},
        {"display_name": "Fine", "username": "root"},
        {},
    ],
)
async def test_invalid_names_are_422_and_never_echoed(
    payload: dict[str, Any],
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        ana = await create_user(client, admin, "ana", display_name="Ana")
        token = await login(client, ana.username, ana.password)
        resp = await client.patch(ME, json=payload, headers=_bearer(token))
        assert await _names(client, admin, ana.principal_id) == "Ana"
    assert resp.status_code == 422, resp.text
    assert resp.json()["type"] == "urn:dc:problem:validation-error"
    if "display_name" in payload:
        pointers = {error["pointer"] for error in resp.json()["errors"]}
        assert pointers & {"/body/display_name", "/body/username"}
        # The random request id is hex and can contain "bad"; judge the problem without it.
        echoed = json.dumps({k: v for k, v in resp.json().items() if k != "request_id"})
        assert "bad" not in echoed and "line" not in echoed
        assert payload["display_name"].strip() == "" or payload["display_name"] not in echoed
    assert _events(sql, env) == []


async def test_user_creation_shares_the_display_name_rule(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        resp = await client.post(
            "/api/v1/identity/users",
            json={"username": "ctl", "password": "a-Long-Pass-phrase-1!", "display_name": "a\tb"},
            headers=_bearer(admin),
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["pointer"] == "/body/display_name"


# --- PATCH /users/{user_id}: a Global admin renames any local user -------------------------------

USERS = "/api/v1/identity/users"


async def test_the_global_admin_renames_a_local_user_active_or_not(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        bo = await create_user(client, admin, "bo.lee", display_name="Bo")
        resp = await client.patch(
            f"{USERS}/{bo.id}", json={"display_name": "  Bo Lee "}, headers=_bearer(admin)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["display_name"] == "Bo Lee"
        assert body["id"] == bo.id
        admin_id = sql(env.db_url, "SELECT id FROM users WHERE username = 'admin'")[0]["id"]
        assert body["updated_by"] == f"local:{admin_id}"
        assert await _names(client, admin, bo.principal_id) == "Bo Lee"

        again = await client.patch(
            f"{USERS}/{bo.id}", json={"display_name": "Bo Lee"}, headers=_bearer(admin)
        )
        assert again.status_code == 200, again.text
        assert again.json() == body
        assert len(_events(sql, env)) == 1

        off = await client.post(f"{USERS}/{bo.id}/deactivate", headers=_bearer(admin))
        assert off.status_code == 200, off.text
        inactive = await client.patch(
            f"{USERS}/{bo.id}", json={"display_name": "Bo Retired"}, headers=_bearer(admin)
        )
        assert inactive.status_code == 200, inactive.text
        assert inactive.json()["display_name"] == "Bo Retired"
        assert inactive.json()["is_active"] is False

    events = _events(sql, env)
    assert len(events) == 2
    assert events[0]["before"]["display_name"] == "Bo"
    assert events[0]["after"]["display_name"] == "Bo Lee"
    assert str(events[0]["aggregate_id"]) == bo.id


async def test_only_the_global_admin_holds_user_update() -> None:
    from dc_auth.roles import PERMISSIONS, ROLE_PERMISSIONS, Role

    assert PERMISSIONS["user:update"].interactive_only is True
    holders = {role for role, held in ROLE_PERMISSIONS.items() if "user:update" in held}
    assert holders == {Role.GLOBAL_ADMIN}


async def test_nobody_else_renames_a_user_and_unknown_ids_are_404(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    grant: Callable[..., Awaitable[Any]],
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    sql: Sql,
) -> None:
    env = make_identity()
    body = {"display_name": "Hijacked"}
    entra_oid = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        org, _project, team = await folder_chain(client, admin, root, "dn")
        victim = await create_user(client, admin, "victim", display_name="Victim")
        org_admin = await create_user(client, admin, "orgadmin")
        editor = await create_user(client, admin, "editor")
        nobody = await create_user(client, admin, "nobody")
        assert (
            await grant(client, admin, org, org_admin.principal_id, "org_admin")
        ).status_code == 201
        assert (
            await grant(client, admin, team, editor.principal_id, "team_editor")
        ).status_code == 201
        target = f"{USERS}/{victim.id}"
        for who in (org_admin, editor, nobody):
            token = await login(client, who.username, who.password)
            denied = await client.patch(target, json=body, headers=_bearer(token))
            assert denied.status_code == 403, (who.username, denied.text)

        anonymous = await client.patch(target, json=body)
        assert anonymous.status_code == 401, anonymous.text

        missing = await client.patch(f"{USERS}/{uuid.uuid4()}", json=body, headers=_bearer(admin))
        assert missing.status_code == 404, missing.text
        assert missing.json()["type"] == "urn:dc:problem:not-found"

        env.directory.add_principal("user", entra_oid, tenant_id=ENTRA_TENANT_ID, groups=())
        entra_row = sql(
            env.db_url,
            "INSERT INTO entra_users (tenant_id, object_id, display_name, created_by, updated_by) "
            "VALUES (%s, %s, 'Entra Eve', 'test', 'test') RETURNING id",
            ENTRA_TENANT_ID,
            entra_oid,
        )
        entra = await client.patch(
            f"{USERS}/{entra_row[0]['id']}", json=body, headers=_bearer(admin)
        )
        assert entra.status_code == 404, entra.text

        assert await _names(client, admin, victim.principal_id) == "Victim"
    assert _events(sql, env) == []


async def test_a_pat_of_the_admin_cannot_rename_and_bad_names_are_422(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        victim = await create_user(client, admin, "victim", display_name="Victim")
        pat = await create_pat(client, admin)
        by_pat = await client.patch(
            f"{USERS}/{victim.id}",
            json={"display_name": "Nope"},
            headers=_bearer(pat["token"]),
        )
        assert by_pat.status_code == 403, by_pat.text
        assert by_pat.json()["type"] == "urn:dc:problem:interactive-session-required"
        bad = await client.patch(
            f"{USERS}/{victim.id}",
            json={"display_name": "evil\u202ename"},
            headers=_bearer(admin),
        )
        assert bad.status_code == 422, bad.text
        assert bad.json()["errors"][0]["pointer"] == "/body/display_name"
        assert "evil" not in bad.text
    assert _events(sql, env) == []
