"""The two identity PDP answers card membership needs (SUB-02, D-60, D-61, D-62).

``POST /internal/v1/authz/grantees`` answers which groups and identities the forwarded token's
caller answers to; ``POST /internal/v1/authz/members/check`` answers whether candidate members may
be linked at a folder. Both derive the principal from the bearer token only and fail closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from dc_auth.authorizer import AuthorizerUnavailable
from dc_auth.principal import Principal
from dc_auth.remote import RemoteAuthorizer
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer
from pydantic import SecretStr

pytestmark = pytest.mark.integration

GRANTEES = "/internal/v1/authz/grantees"
FOLDERS = "/api/v1/identity/folders"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
GrantSql = Callable[..., None]
Sql = Callable[..., list[dict[str, Any]]]
CreateUser = Callable[..., Awaitable[Any]]
Login = Callable[..., Awaitable[str]]
CreateGroup = Callable[..., Awaitable[str]]
AddMember = Callable[..., Awaitable[Any]]
CreateServiceAccount = Callable[..., Awaitable[dict[str, Any]]]

_ANYONE = Principal(
    id="x:anyone", type="user", display="anyone", issuer="x", subject="anyone", via="jwt"
)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _remote(app: Any, token: str) -> Any:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        yield RemoteAuthorizer(
            http, base_url="http://identity", bearer=SecretStr(token), timeout_s=2.0
        )


def _register_group(sql: Sql, env: Any, object_id: str, owner: str | None) -> None:
    env.directory.add_group(object_id, f"group {object_id[:8]}")
    sql(
        env.db_url,
        "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
        "display_name, created_by, updated_by) VALUES ('group', %s, %s, %s, %s, 'test', 'test')",
        ENTRA_TENANT_ID,
        object_id,
        owner,
        f"group {object_id[:8]}",
    )


async def _root(client: Any, admin: str) -> str:
    listed = await client.get(FOLDERS, headers=_bearer(admin))
    return str(next(f["id"] for f in listed.json()["items"] if f["kind"] == "root"))


async def test_a_local_user_learns_itself_and_every_local_group_it_is_in(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    create_user: CreateUser,
    login: Login,
    create_group: CreateGroup,
    add_member: AddMember,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root(client, admin)
        alice = await create_user(client, admin, "alice")
        group = await create_group(client, admin, root, "readers")
        other = await create_group(client, admin, root, "others")
        assert (await add_member(client, admin, group, "alice")).status_code == 201
        token = await login(client, "alice", alice.password)

        outbox_before = sql(env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"]
        resp = await client.post(GRANTEES, json={}, headers=_bearer(token))
        # The body is never read for identity: a body naming somebody else changes nothing.
        spoof = await client.post(
            GRANTEES, json={"principal_id": "local:" + str(uuid.uuid4())}, headers=_bearer(token)
        )
        async with _remote(env.app, token) as authz:
            via_client = await authz.grantees(_ANYONE)
        outbox_after = sql(env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"]
    assert resp.status_code == 200, resp.text
    wire = {(g["type"], g["id"]) for g in resp.json()["grantees"]}
    assert wire == {("user", alice.principal_id), ("local_group", group)}
    assert ("local_group", other) not in wire
    assert spoof.status_code == 200, spoof.text
    assert {(g["type"], g["id"]) for g in spoof.json()["grantees"]} == wire
    assert via_client == frozenset(wire)
    assert outbox_after == outbox_before  # a read: nothing is audited or written


async def test_the_answer_needs_a_live_token_and_a_live_principal(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: CreateUser,
    login: Login,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        bob = await create_user(client, admin, "bob")
        token = await login(client, "bob", bob.password)
        before = await client.post(GRANTEES, json={}, headers=_bearer(token))
        deactivated = await client.post(
            f"/api/v1/identity/users/{bob.id}/deactivate", headers=_bearer(admin)
        )
        after = await client.post(GRANTEES, json={}, headers=_bearer(token))
        anonymous = await client.post(GRANTEES, json={})
        garbage = await client.post(GRANTEES, json={}, headers=_bearer("not-a-token"))
        async with _remote(env.app, token) as authz:
            with pytest.raises(Exception) as info:
                await authz.grantees(_ANYONE)
    assert before.status_code == 200, before.text
    assert deactivated.status_code == 200, deactivated.text
    assert after.status_code == 401, after.text
    assert anonymous.status_code == 401
    assert garbage.status_code == 401
    assert getattr(info.value, "status", None) == 401


async def test_an_entra_user_learns_the_registered_groups_graph_resolved(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    alice = str(uuid.uuid4())
    group = str(uuid.uuid4())
    unregistered = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root(client, admin)
        _register_group(sql, env, group, root)
        env.directory.add_principal("user", alice, groups=(group, unregistered))
        token = fake_issuer.mint_entra(alice)
        resp = await client.post(GRANTEES, json={}, headers=_bearer(token))
        env.directory.fail = True
        outage = await client.post(GRANTEES, json={}, headers=_bearer(token))
        async with _remote(env.app, token) as authz:
            with pytest.raises(AuthorizerUnavailable):
                await authz.grantees(_ANYONE)
    assert resp.status_code == 200, resp.text
    wire = {(g["type"], g["id"]) for g in resp.json()["grantees"]}
    assert ("entra_group", group) in wire
    assert ("user", f"entra:{ENTRA_TENANT_ID}:{alice}") in wire
    assert outage.status_code == 503, outage.text
    assert outage.json()["type"] == "urn:dc:problem:service-unavailable"


async def test_an_unregistered_entra_principal_gets_the_gate_s_403(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    stranger = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        env.directory.add_principal("user", stranger, groups=())
        resp = await client.post(
            GRANTEES, json={}, headers=_bearer(fake_issuer.mint_entra(stranger))
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["type"] == "urn:dc:problem:principal-not-registered"


async def test_a_service_account_key_learns_its_own_sa_identity(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_service_account: CreateServiceAccount,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root(client, admin)
        sa = await create_service_account(client, admin, root, "pipeline")
        key = await client.post(
            f"/api/v1/identity/service-accounts/{sa['id']}/keys",
            json={"name": "k", "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            headers=_bearer(admin),
        )
        assert key.status_code == 201, key.text
        resp = await client.post(GRANTEES, json={}, headers=_bearer(key.json()["token"]))
    assert resp.status_code == 200, resp.text
    wire = {(g["type"], g["id"]) for g in resp.json()["grantees"]}
    assert ("service_account", sa["principal_id"]) in wire


# --- members/check: may these members be linked at a folder? (D-27, D-64, D-69, A3) -------------

MEMBERS = "/internal/v1/authz/members/check"
SERVICE_ACCOUNTS = "/api/v1/identity/service-accounts"


@dataclass
class _World:
    env: Any
    client: Any
    admin: str
    root: str
    org_a: str
    team_a: str
    org_b: str
    sql: Sql

    async def check(
        self, members: list[tuple[str, str]], folder: str | None = None, *, token: str | None = None
    ) -> Any:
        return await self.client.post(
            MEMBERS,
            json={
                "folder_id": folder or self.team_a,
                "members": [{"type": t, "id": i} for t, i in members],
            },
            headers=_bearer(token or self.admin),
        )

    async def valid(self, members: list[tuple[str, str]], folder: str | None = None) -> list[bool]:
        resp = await self.check(members, folder)
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert [(r["type"], r["id"]) for r in results] == members  # request order, one each
        return [bool(r["valid"]) for r in results]


@pytest.fixture
def open_world(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
) -> Callable[..., Any]:
    @asynccontextmanager
    async def _open(**overrides: object) -> Any:
        env = make_identity(**overrides)
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            root = await _root(client, admin)
            org_a, _project_a, team_a = await folder_chain(client, admin, root, "ma")
            org_b, _, _ = await folder_chain(client, admin, root, "mb")
            yield _World(env, client, admin, root, org_a, team_a, org_b, sql)

    return _open


async def test_local_groups_follow_the_grant_scope_rules(
    open_world: Callable[..., Any], create_group: CreateGroup
) -> None:
    async with open_world() as w:
        g_root = await create_group(w.client, w.admin, w.root, "root-group")
        g_a = await create_group(w.client, w.admin, w.org_a, "a-group")
        g_b = await create_group(w.client, w.admin, w.org_b, "b-group")
        members = [
            ("local_group", g_root),
            ("local_group", g_a),
            ("local_group", g_b),
            ("local_group", str(uuid.uuid4())),
            ("local_group", "not-a-uuid"),
            ("local_group", g_root.upper()),
        ]
        got = await w.valid(members)
        at_org_b = await w.valid([("local_group", g_a), ("local_group", g_b)], w.org_b)
    assert got == [True, True, False, False, False, False]
    assert at_org_b == [False, True]


async def test_a_registered_entra_group_must_also_exist_in_graph(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        live = str(uuid.uuid4())
        deleted = str(uuid.uuid4())
        unregistered = str(uuid.uuid4())
        _register_group(w.sql, w.env, live, w.root)
        _register_group(w.sql, w.env, deleted, w.root)
        w.env.directory._groups.pop((ENTRA_TENANT_ID.lower(), deleted))  # Graph forgot it
        got = await w.valid(
            [("entra_group", live), ("entra_group", deleted), ("entra_group", unregistered)]
        )
        # Org-owned registrations stay inside their Organization.
        owned_b = str(uuid.uuid4())
        _register_group(w.sql, w.env, owned_b, w.org_b)
        confined = await w.valid([("entra_group", owned_b)])
        w.env.directory.fail = True
        outage = await w.check([("entra_group", live)])
        # No registration means no Graph call: a plain false even while Graph is down.
        still_false = await w.check([("entra_group", unregistered)])
    assert got == [True, False, False]
    assert confined == [False]
    assert outage.status_code == 503, outage.text
    assert outage.json()["type"] == "urn:dc:problem:service-unavailable"
    assert still_false.status_code == 200, still_false.text
    assert [r["valid"] for r in still_false.json()["results"]] == [False]


async def test_service_accounts_follow_org_confinement_and_liveness(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world() as w:
        sa_root = await create_service_account(w.client, w.admin, w.root, "root-bot")
        sa_b = await create_service_account(w.client, w.admin, w.org_b, "b-bot")
        sa_dead = await create_service_account(w.client, w.admin, w.root, "dead-bot")
        gone = await w.client.post(
            f"{SERVICE_ACCOUNTS}/{sa_dead['id']}/deactivate", headers=_bearer(w.admin)
        )
        assert gone.status_code == 200, gone.text
        got = await w.valid(
            [
                ("service_account", sa_root["principal_id"]),
                ("service_account", sa_b["principal_id"]),
                ("service_account", sa_dead["principal_id"]),
                ("service_account", "sa:garbage"),
            ]
        )
    assert got == [True, False, False, False]


async def test_only_groups_and_service_accounts_can_be_linked_and_the_shape_is_bounded(
    open_world: Callable[..., Any], create_group: CreateGroup
) -> None:
    async with open_world() as w:
        group = await create_group(w.client, w.admin, w.root, "g")
        kinds = await w.valid(
            [
                ("user", "local:" + str(uuid.uuid4())),
                ("service_principal", f"entra:{ENTRA_TENANT_ID}:{uuid.uuid4()}"),
                ("local_group", group),
            ]
        )
        unknown_folder = await w.valid(
            [("local_group", group), ("service_account", "sa:" + str(uuid.uuid4()))],
            str(uuid.uuid4()),
        )
        too_many = await w.check([("local_group", group)] * 21)
        empty = await w.check([])
        spoof = await w.client.post(
            MEMBERS,
            json={
                "folder_id": w.team_a,
                "members": [{"type": "local_group", "id": group}],
                "principal": "local:" + str(uuid.uuid4()),
            },
            headers=_bearer(w.admin),
        )
        anonymous = await w.client.post(
            MEMBERS, json={"folder_id": w.team_a, "members": [{"type": "local_group", "id": group}]}
        )
    assert kinds == [False, False, True]
    assert unknown_folder == [False, False]
    assert too_many.status_code == 422
    assert empty.status_code == 422
    assert spoof.status_code == 422  # a body naming a principal is refused, never read
    assert anonymous.status_code == 401


async def test_a_refusal_carries_no_hint_and_the_call_writes_nothing(
    open_world: Callable[..., Any], create_group: CreateGroup
) -> None:
    async with open_world() as w:
        other_org = await create_group(w.client, w.admin, w.org_b, "b-group")
        before = w.sql(w.env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"]
        unknown = await w.check([("local_group", str(uuid.uuid4()))])
        foreign = await w.check([("local_group", other_org)])
        after = w.sql(w.env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"]
    assert unknown.status_code == foreign.status_code == 200
    for resp in (unknown, foreign):
        (result,) = resp.json()["results"]
        assert set(result) == {"type", "id", "valid"}
        assert result["valid"] is False
    assert before == after


async def test_the_principal_gate_runs_first_for_members_check(
    open_world: Callable[..., Any], create_user: CreateUser, login: Login, fake_issuer: FakeIssuer
) -> None:
    async with open_world() as w:
        stranger = str(uuid.uuid4())
        w.env.directory.add_principal("user", stranger, groups=())
        unregistered = await w.check(
            [("local_group", str(uuid.uuid4()))], token=fake_issuer.mint_entra(stranger)
        )
        bob = await create_user(w.client, w.admin, "bob-m")
        token = await login(w.client, "bob-m", bob.password)
        w.env.directory.fail = True  # a local caller never needs Graph
        local = await w.check([("local_group", str(uuid.uuid4()))], token=token)
    assert unregistered.status_code == 403
    assert unregistered.json()["type"] == "urn:dc:problem:principal-not-registered"
    assert local.status_code == 200, local.text


async def test_remote_authorizer_check_members_maps_the_answer_by_member(
    open_world: Callable[..., Any], create_group: CreateGroup
) -> None:
    async with open_world() as w:
        group = await create_group(w.client, w.admin, w.root, "g")
        stranger = ("local_group", str(uuid.uuid4()))
        async with _remote(w.env.app, w.admin) as authz:
            got = await authz.check_members(
                _ANYONE, uuid.UUID(w.team_a), [("local_group", group), stranger]
            )
            w.env.directory.fail = True
            registered = str(uuid.uuid4())
            _register_group(w.sql, w.env, registered, w.root)
            with pytest.raises(AuthorizerUnavailable):
                await authz.check_members(
                    _ANYONE, uuid.UUID(w.team_a), [("entra_group", registered)]
                )
    assert got == {("local_group", group): True, stranger: False}
