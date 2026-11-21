"""``GET /api/v1/identity/me`` (D-UI-08-19, UI-07): who the caller is and what it holds.

The restricted bootstrap session is refused, no token is 401, and an admin's answer carries the
grant form of its id, its ROOT assignment and its global permissions. The reads are never
audited and only inform the UI (D-UI-08-21).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dc_auth.authorizer import AuthorizerUnavailable
from dc_auth.roles import PERMISSIONS, READ_PERMISSIONS, is_bound_to
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer

pytestmark = pytest.mark.integration

ME = "/api/v1/identity/me"
CAPABILITIES = f"{ME}/capabilities"
CHECK = "/internal/v1/authz/check"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_the_global_admin_learns_who_it_is_and_what_it_holds(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: Callable[[Any, Any], Awaitable[str]],
    sql: Callable[..., list[dict[str, Any]]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        resp = await client.get(ME, headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    admin = sql(env.db_url, "SELECT id FROM users WHERE username = %s", "admin")[0]["id"]
    root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
    assert body["principal_id"] == f"local:{admin}"
    assert body["principal_type"] == "user"
    assert body["idp"] == "local"
    assert body["via"] == "jwt"
    assert body["interactive"] is True
    assert body["read_only"] is False
    assert body["username"] == "admin"
    assert body["preferred_username"] is None
    assert body["groups"] == []
    assert "me:read" in body["global_permissions"]
    assert "user:create" in body["global_permissions"]
    assert body["global_permissions"] == sorted(body["global_permissions"])
    assert [
        (item["folder_id"], item["folder_path"], item["role"], item["via_group_id"])
        for item in body["assignments"]
    ] == [(str(root), "", "global_admin", None)]
    assert set(body["assignments"][0]) == {
        "grant_id",
        "folder_id",
        "folder_path",
        "role",
        "via_group_id",
    }


async def test_a_restricted_bootstrap_token_is_refused(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    bootstrap_token: Callable[[Any, Any], Awaitable[str]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        restricted = await bootstrap_token(client, env)
        resp = await client.get(ME, headers=_bearer(restricted))
    assert resp.status_code == 403, resp.text
    assert resp.json()["type"] == "urn:dc:problem:password-rotation-required"


async def test_no_token_is_401(make_identity: Callable[..., Any], asgi_client: Any) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        resp = await client.get(ME)
    assert resp.status_code == 401, resp.text
    assert resp.json()["type"] == "urn:dc:problem:unauthenticated"


# --- Task 2: /me/capabilities and its parity with the PDP ---------------------------------------


def _expiry(days: float = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


@dataclass
class World:
    client: Any
    env: Any
    admin: str
    root: str
    org: str
    project: str
    team: str
    other_org: str
    tokens: dict[str, str] = field(default_factory=dict)

    @property
    def folders(self) -> dict[str, str]:
        return {
            "root": self.root,
            "organization": self.org,
            "project": self.project,
            "team": self.team,
        }

    async def capabilities(self, token: str, folder_id: object | None = None) -> Any:
        params = {} if folder_id is None else {"folder_id": str(folder_id)}
        return await self.client.get(CAPABILITIES, params=params, headers=_bearer(token))

    async def allowed(self, token: str, permission: str, folder_id: str | None) -> bool:
        """True when the PDP answers ALLOW (an interactive-only refusal is a 403, not ALLOW)."""
        resp = await self.client.post(
            CHECK,
            json={"permission": permission, "folder_id": folder_id},
            headers=_bearer(token),
        )
        return bool(resp.status_code == 200 and resp.json()["outcome"] == "allow")


@pytest.fixture
def open_world(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: Callable[[Any, Any], Awaitable[str]],
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    grant: Callable[..., Awaitable[Any]],
    create_group: Callable[..., Awaitable[str]],
    add_member: Callable[..., Awaitable[Any]],
    create_service_account: Callable[..., Awaitable[dict[str, Any]]],
    sql: Callable[..., list[dict[str, Any]]],
) -> Callable[..., Any]:
    @asynccontextmanager
    async def _open() -> AsyncIterator[World]:
        env = make_identity()
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
            org, project, team = await folder_chain(client, admin, root, "me")
            other_org, _, _ = await folder_chain(client, admin, root, "other")
            world = World(client, env, admin, root, org, project, team, other_org)
            world.tokens["admin_jwt"] = admin
            for name, read_only in (("admin_ro_pat", True), ("admin_pat", False)):
                made = await client.post(
                    "/api/v1/identity/pats",
                    json={"name": name, "expires_at": _expiry(), "read_only": read_only},
                    headers=_bearer(admin),
                )
                assert made.status_code == 201, made.text
                world.tokens[name] = made.json()["token"]
            # A service account holding Project admin on the project: it holds grant:manage by
            # role, and a key may never exercise it (D-79).
            account = await create_service_account(client, admin, root, "me-bot")
            granted = await grant(
                client, admin, project, account["principal_id"], "project_admin", "service_account"
            )
            assert granted.status_code == 201, granted.text
            key = await client.post(
                f"/api/v1/identity/service-accounts/{account['id']}/keys",
                json={"name": "k", "expires_at": _expiry()},
                headers=_bearer(admin),
            )
            assert key.status_code == 201, key.text
            world.tokens["sa_key"] = key.json()["token"]
            # A Team reader through a local group: no direct grant of its own.
            member = await create_user(client, admin, "grouped")
            group = await create_group(client, admin, org, "readers")
            added = await add_member(client, admin, group, member.username)
            assert added.status_code == 201, added.text
            via_group = await grant(client, admin, team, group, "team_reader", "local_group")
            assert via_group.status_code == 201, via_group.text
            world.tokens["group_member"] = await login(client, member.username, member.password)
            # A user with no grant at all.
            nobody = await create_user(client, admin, "nobody")
            world.tokens["nobody"] = await login(client, nobody.username, nobody.password)
            yield world

    return _open


async def test_the_admin_sees_its_team_permissions_and_only_roles_it_could_grant_there(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        resp = await w.capabilities(w.admin, w.team)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["folder_id"] == w.team
    assert body["roles"] == ["global_admin"]
    assert {"folder:read", "grant:manage"} <= set(body["permissions"])
    assert body["inheritance_blocked"] is False
    assert body["grantable_roles"]
    assert all(is_bound_to(role, "team") for role in body["grantable_roles"])
    assert body["grantable_roles"] == sorted(body["grantable_roles"])


async def test_a_read_only_pat_lists_no_write_and_cannot_grant(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        resp = await w.capabilities(w.tokens["admin_ro_pat"], w.team)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["permissions"]
    assert set(body["permissions"]) <= READ_PERMISSIONS
    assert body["grantable_roles"] == []


async def test_a_full_pat_and_a_key_never_list_an_interactive_only_permission(
    open_world: Callable[..., Any],
) -> None:
    interactive = {name for name, spec in PERMISSIONS.items() if spec.interactive_only}
    async with open_world() as w:
        pat = await w.capabilities(w.tokens["admin_pat"], w.team)
        key = await w.capabilities(w.tokens["sa_key"], w.project)
        jwt = await w.capabilities(w.admin, w.project)
    assert pat.status_code == 200 and key.status_code == 200, (pat.text, key.text)
    for body in (pat.json(), key.json()):
        assert not interactive & set(body["permissions"])
        assert "grant:manage" not in body["permissions"]
        assert body["grantable_roles"] == []
    assert "folder:read" in key.json()["permissions"]
    assert key.json()["roles"] == ["project_admin"]
    # Non-vacuous: the same folder offers the JWT admin what the key lost.
    assert "grant:manage" in jwt.json()["permissions"]


async def test_a_group_member_holds_the_inherited_role_at_the_team(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        resp = await w.capabilities(w.tokens["group_member"], w.team)
        sibling = await w.capabilities(w.tokens["group_member"], w.other_org)
    assert resp.status_code == 200, resp.text
    assert resp.json()["roles"] == ["team_reader"]
    assert "folder:read" in resp.json()["permissions"]
    assert resp.json()["grantable_roles"] == []
    assert sibling.status_code == 404, sibling.text


async def test_an_unknown_and_an_unreadable_folder_are_the_same_recorded_404(
    open_world: Callable[..., Any], sql: Callable[..., list[dict[str, Any]]]
) -> None:
    query = (
        "SELECT count(*) AS n FROM outbox WHERE aggregate_type = 'authz' "
        "AND action = 'check.denied'"
    )
    async with open_world() as w:
        before = sql(w.env.db_url, query)[0]["n"]
        unknown = await w.capabilities(w.tokens["nobody"], uuid.uuid4())
        unreadable = await w.capabilities(w.tokens["nobody"], w.team)
        admin_unknown = await w.capabilities(w.admin, uuid.uuid4())
        after = sql(w.env.db_url, query)[0]["n"]
    for resp in (unknown, unreadable, admin_unknown):
        assert resp.status_code == 404, resp.text
        assert resp.json()["type"] == "urn:dc:problem:not-found"
    assert unknown.json()["detail"] == unreadable.json()["detail"]
    assert after - before == 3


async def test_without_a_folder_the_capabilities_equal_the_global_permissions_of_me(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        for name, token in w.tokens.items():
            me = await w.client.get(ME, headers=_bearer(token))
            caps = await w.capabilities(token)
            assert me.status_code == 200 and caps.status_code == 200, (name, me.text, caps.text)
            body = caps.json()
            assert body["folder_id"] is None, name
            assert body["permissions"] == me.json()["global_permissions"], name
            assert body["inheritance_blocked"] is False
            assert body["grantable_roles"] == []
        admin_caps = (await w.capabilities(w.admin)).json()
    assert admin_caps["roles"] == ["global_admin"]


async def test_a_restricted_bootstrap_token_is_refused_by_capabilities(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    bootstrap_token: Callable[[Any, Any], Awaitable[str]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        restricted = await bootstrap_token(client, env)
        resp = await client.get(CAPABILITIES, headers=_bearer(restricted))
    assert resp.status_code == 403, resp.text
    assert resp.json()["type"] == "urn:dc:problem:password-rotation-required"


async def test_a_malformed_folder_id_is_422(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        resp = await w.client.get(
            CAPABILITIES, params={"folder_id": "not-a-uuid"}, headers=_bearer(w.admin)
        )
    assert resp.status_code == 422, resp.text


async def test_me_trims_assignments_and_names_the_group_a_grant_came_through(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        grouped = await w.client.get(ME, headers=_bearer(w.tokens["group_member"]))
        nobody = await w.client.get(ME, headers=_bearer(w.tokens["nobody"]))
        key = await w.client.get(ME, headers=_bearer(w.tokens["sa_key"]))
        ro = await w.client.get(ME, headers=_bearer(w.tokens["admin_ro_pat"]))
    body = grouped.json()
    assert [group["display_name"] for group in body["groups"]] == ["readers"]
    assert body["groups"][0]["type"] == "local_group"
    assert [(a["folder_path"], a["role"], a["via_group_id"]) for a in body["assignments"]] == [
        ("me/me-p/me-t1", "team_reader", body["groups"][0]["id"])
    ]
    assert nobody.json()["assignments"] == []
    assert key.json()["principal_type"] == "service_account"
    assert key.json()["principal_id"].startswith("sa:")
    assert key.json()["via"] == "key" and key.json()["interactive"] is False
    assert key.json()["display_name"] == "me-bot"
    assert ro.json()["via"] == "pat" and ro.json()["read_only"] is True
    assert ro.json()["principal_id"].startswith("local:")
    assert ro.json()["principal_type"] == "user"
    assert ro.json()["interactive"] is False
    assert "pat:manage" not in ro.json()["global_permissions"]
    assert set(ro.json()["global_permissions"]) <= READ_PERMISSIONS


async def test_me_does_not_name_a_folder_the_caller_cannot_read(
    open_world: Callable[..., Any],
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    grant: Callable[..., Awaitable[Any]],
    sql: Callable[..., list[dict[str, Any]]],
) -> None:
    """A Notifier grant at ROOT holds ``mail:send`` and no ``folder:read``: the row is not named."""
    async with open_world() as w:
        user = await create_user(w.client, w.admin, "mailer")
        made = await grant(w.client, w.admin, w.root, user.principal_id, "notifier")
        assert made.status_code == 201, made.text
        rows = sql(
            w.env.db_url,
            "SELECT count(*) AS n FROM role_assignments WHERE principal_id = %s",
            user.principal_id,
        )
        token = await login(w.client, user.username, user.password)
        me = await w.client.get(ME, headers=_bearer(token))
    assert rows[0]["n"] == 1
    assert me.status_code == 200, me.text
    assert me.json()["assignments"] == []
    assert "mail:send" in me.json()["global_permissions"]


@pytest.mark.parametrize("kind", ["root", "organization", "project", "team"])
async def test_capabilities_parity_with_the_pdp_across_five_principals_and_four_levels(
    open_world: Callable[..., Any], kind: str
) -> None:
    """Every listed permission is ALLOW in the PDP and every folder permission left out is not."""
    folder_permissions = [name for name, spec in PERMISSIONS.items() if spec.target == "folder"]
    async with open_world() as w:
        folder_id = w.folders[kind]
        principals = ("admin_jwt", "admin_ro_pat", "admin_pat", "sa_key", "group_member")
        checked = 0
        for who in principals:
            token = w.tokens[who]
            resp = await w.capabilities(token, folder_id)
            if resp.status_code == 404:
                listed: set[str] = set()
            else:
                assert resp.status_code == 200, (who, kind, resp.text)
                listed = set(resp.json()["permissions"])
            for name in folder_permissions:
                allowed = await w.allowed(token, name, folder_id)
                assert allowed == (name in listed), (who, kind, name, allowed)
                checked += 1
            if resp.status_code == 200:
                assert listed <= set(folder_permissions), (who, kind)
        assert checked == len(principals) * len(folder_permissions)


async def test_global_permissions_parity_with_the_pdp_for_five_principals(
    open_world: Callable[..., Any],
) -> None:
    """``global_permissions`` lists exactly the principal-level and ROOT permissions ALLOWed."""
    async with open_world() as w:
        for who in ("admin_jwt", "admin_ro_pat", "admin_pat", "sa_key", "group_member"):
            token = w.tokens[who]
            me = await w.client.get(ME, headers=_bearer(token))
            listed = set(me.json()["global_permissions"])
            for name, spec in PERMISSIONS.items():
                if spec.target == "special":
                    continue
                allowed = await w.allowed(token, name, w.root if spec.target == "folder" else None)
                assert allowed == (name in listed), (who, name, allowed)


# --- Entra, generic OIDC and an authorization outage ---------------------------------------------

OTHER_TENANT = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _register_group(sql: Callable[..., Any], env: Any, tenant: str, group: str, owner: str) -> None:
    sql(
        env.db_url,
        "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
        "display_name, created_by, updated_by) VALUES ('group', %s, %s, %s, 'engineers', "
        "'test', 'test')",
        tenant,
        group,
        owner,
    )


async def test_an_entra_user_sees_each_registered_group_once_by_jwt_and_by_pat(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    sql: Callable[..., list[dict[str, Any]]],
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    alice, group, unregistered = (str(uuid.uuid4()) for _ in range(3))
    env.directory.add_group(group, "engineers")
    env.directory.add_principal("user", alice, groups=(group, unregistered))
    jwt = fake_issuer.mint_entra(alice, name="Alice Entra", preferred_username="alice@corp.test")
    async with asgi_client(env.app) as client:
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        # The same group GUID registered in a second tenant must not list the group twice.
        _register_group(sql, env, ENTRA_TENANT_ID, group, root)
        _register_group(sql, env, OTHER_TENANT, group, root)
        by_jwt = await client.get(ME, headers=_bearer(jwt))
        made = await client.post(
            "/api/v1/identity/pats",
            json={"name": "entra-pat", "expires_at": _expiry()},
            headers=_bearer(jwt),
        )
        assert made.status_code == 201, made.text
        by_pat = await client.get(ME, headers=_bearer(made.json()["token"]))
    assert by_jwt.status_code == 200, by_jwt.text
    assert by_pat.status_code == 200, by_pat.text
    expected_groups = [{"id": group, "type": "entra_group", "display_name": "engineers"}]
    for body, via in ((by_jwt.json(), "jwt"), (by_pat.json(), "pat")):
        assert body["principal_id"] == f"entra:{ENTRA_TENANT_ID}:{alice}"
        assert body["principal_type"] == "entra_user"
        assert body["idp"] == "entra"
        assert body["via"] == via
        assert body["display_name"] == "Alice Entra"
        assert body["username"] is None
        assert body["preferred_username"] == "alice@corp.test"
        assert body["groups"] == expected_groups
        assert body["assignments"] == []
    assert by_jwt.json()["interactive"] is True
    assert by_pat.json()["interactive"] is False


async def test_a_generic_oidc_caller_is_answered_from_its_token_alone(
    make_identity: Callable[..., Any], asgi_client: Any, fake_issuer: FakeIssuer
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        resp = await client.get(ME, headers=_bearer(fake_issuer.mint("pipeline-7", name="Bot 7")))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["principal_id"] == "fake:pipeline-7"
    assert body["principal_type"] == "user"
    assert body["idp"] == "local"
    assert body["display_name"] == "Bot 7"
    assert body["username"] is None
    assert body["preferred_username"] is None
    assert body["groups"] == []
    assert body["assignments"] == []


class _Outage:
    """Delegates to the real authorizer except for the handler's own calls, which are down."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def check(self, principal: Any, permission: str, *args: Any, **kwargs: Any) -> Any:
        if permission == "folder:read":
            raise AuthorizerUnavailable("down")
        return await self._inner.check(principal, permission, *args, **kwargs)

    async def accessible_folders(self, principal: Any, permission: str) -> Any:
        raise AuthorizerUnavailable("down")


async def test_me_and_capabilities_are_503_while_authorization_is_unavailable(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: Callable[[Any, Any], Awaitable[str]],
    sql: Callable[..., list[dict[str, Any]]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        state = env.app.state
        original = state.authorizer_factory
        state.authorizer_factory = lambda request: _Outage(original(request))
        try:
            me = await client.get(ME, headers=_bearer(token))
            capabilities = await client.get(
                CAPABILITIES, params={"folder_id": root}, headers=_bearer(token)
            )
        finally:
            state.authorizer_factory = original
    for resp in (me, capabilities):
        assert resp.status_code == 503, resp.text
        assert resp.json()["type"] == "urn:dc:problem:service-unavailable"
