"""Entra registration API: groups, users and service principals (AUTH-04, AUTH-05, AUTH-03).

An administrator admits a whole Entra group, a single Entra user or a pipeline's service principal
with one call (D-57, D-58, D-59). Every GUID is confirmed with the directory in the resolved tenant
before anything is written (D-64); registration grants nothing. Ownership follows the group model
(D-27): ROOT or an Organization owns groups and service principals, a Global admin registers users.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from dc_auth.issuers import IssuerConfig
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer
from dc_identity.directory.fake import FakeDirectoryClient

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
GROUPS = f"{IDENTITY}/entra/groups"
OTHER_TENANT = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
Sql = Callable[..., list[dict[str, Any]]]
AdminFlow = Callable[[Any, Any], Awaitable[str]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
CreateUser = Callable[..., Awaitable[Any]]
Login = Callable[..., Awaitable[str]]
GrantSql = Callable[..., None]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def slug_of(resp: Any) -> str:
    return str(resp.json()["type"]).rsplit(":", 1)[-1]


def new_guid() -> str:
    return str(uuid.uuid4())


@dataclass
class World:
    """Two Organizations (A, B) with an Organization admin each, and a Global admin."""

    env: Any
    client: Any
    sql: Sql
    fake_issuer: FakeIssuer
    admin: str
    root: str
    org_a: str
    project_a: str
    team_a: str
    org_b: str
    team_b: str
    oa: str  # Organization admin of A (token)
    ob: str  # Organization admin of B (token)
    grant_sql: GrantSql
    create_user: CreateUser
    login: Login

    @property
    def directory(self) -> FakeDirectoryClient:
        directory: FakeDirectoryClient = self.env.directory
        return directory

    def events(self, like: str = "entra_registration.%") -> list[dict[str, Any]]:
        return self.sql(
            self.env.db_url,
            "SELECT action, aggregate_type, aggregate_id, actor, before, after, scope_path "
            "FROM outbox WHERE action LIKE %s ORDER BY id",
            like,
        )

    def rows(self, kind: str) -> list[dict[str, Any]]:
        return self.sql(
            self.env.db_url,
            "SELECT * FROM entra_registrations WHERE kind = %s ORDER BY id",
            kind,
        )

    def group(self, name: str = "Data Eng", *, tenant_id: str = ENTRA_TENANT_ID) -> str:
        """A group that exists in the fake directory."""
        oid = new_guid()
        self.directory.add_group(oid, name, tenant_id=tenant_id)
        return oid

    async def register_group(
        self, token: str, owner: str, oid: str, *, tenant_id: str | None = None
    ) -> Any:
        body: dict[str, Any] = {"owner_folder_id": owner, "object_id": oid}
        if tenant_id is not None:
            body["tenant_id"] = tenant_id
        return await self.client.post(GROUPS, json=body, headers=auth(token))

    async def local_token(self, username: str, role: str | None, folder: str | None) -> str:
        user = await self.create_user(self.client, self.admin, username)
        if role is not None and folder is not None:
            self.grant_sql(self.env.db_url, user.principal_id, role, folder)
        return await self.login(self.client, user.username, user.password)


OpenWorld = Callable[..., AbstractAsyncContextManager[World]]


@pytest.fixture
def open_world(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> OpenWorld:
    @asynccontextmanager
    async def _open(
        *, extra_issuers: Sequence[IssuerConfig] = (), **overrides: object
    ) -> AsyncIterator[World]:
        env = make_identity(extra_issuers=extra_issuers, **overrides)
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
            org_a, project_a, team_a = await folder_chain(client, admin, root, "ra")
            org_b, _project_b, team_b = await folder_chain(client, admin, root, "rb")
            world = World(
                env=env,
                client=client,
                sql=sql,
                fake_issuer=fake_issuer,
                admin=admin,
                root=root,
                org_a=org_a,
                project_a=project_a,
                team_a=team_a,
                org_b=org_b,
                team_b=team_b,
                oa="",
                ob="",
                grant_sql=grant_sql,
                create_user=create_user,
                login=login,
            )
            world.oa = await world.local_token("oa-1", "org_admin", org_a)
            world.ob = await world.local_token("ob-1", "org_admin", org_b)
            yield world

    return _open


# --- Task 1: Entra group registrations ----------------------------------------------------------


async def test_a_global_admin_registers_a_root_owned_group_confirmed_by_the_directory(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        oid = w.group("Data Eng")
        resp = await w.register_group(w.admin, w.root, oid)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["display_name"] == "Data Eng"
        assert body["tenant_id"] == ENTRA_TENANT_ID
        assert body["object_id"] == oid
        assert body["owner_folder_id"] == w.root
        assert resp.headers["location"] == f"{GROUPS}/{body['id']}"
        assert ("group", ENTRA_TENANT_ID, oid) in w.directory.calls
        stored = w.rows("group")
        assert len(stored) == 1
        assert stored[0]["directly_registered"] is True
        assert stored[0]["is_active"] is True
        events = w.events()
        assert [e["action"] for e in events] == ["entra_registration.created"]
        assert events[0]["aggregate_type"] == "entra_registration"
        assert events[0]["aggregate_id"] == body["id"]
        assert events[0]["before"] is None
        assert events[0]["after"] == {
            "id": body["id"],
            "kind": "group",
            "tenant_id": ENTRA_TENANT_ID,
            "object_id": oid,
            "owner_folder_id": w.root,
            "display_name": "Data Eng",
            "is_active": True,
            "directly_registered": True,
        }
        assert [str(x) for x in events[0]["scope_path"]] == [w.root]


async def test_registering_the_same_group_twice_is_409_and_writes_one_event(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        oid = w.group()
        assert (await w.register_group(w.admin, w.root, oid)).status_code == 201
        again = await w.register_group(w.admin, w.root, oid)
        elsewhere = await w.register_group(w.admin, w.org_a, oid)
        assert again.status_code == 409, again.text
        assert slug_of(again) == "conflict"
        assert elsewhere.status_code == 409, elsewhere.text
        assert len(w.rows("group")) == 1
        assert len(w.events()) == 1


async def test_a_guid_the_directory_does_not_know_is_422_and_nothing_is_stored(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        resp = await w.register_group(w.admin, w.root, new_guid())
        assert resp.status_code == 422, resp.text
        assert slug_of(resp) == "unknown-principal"
        assert w.rows("group") == []
        assert w.events() == []


async def test_a_directory_outage_is_503_and_nothing_is_stored(open_world: OpenWorld) -> None:
    async with open_world() as w:
        oid = w.group()
        w.directory.fail = True
        resp = await w.register_group(w.admin, w.root, oid)
        assert resp.status_code == 503, resp.text
        assert slug_of(resp) == "service-unavailable"
        assert w.rows("group") == []
        assert w.events() == []


async def test_a_malformed_object_id_is_422_validation_error_and_asks_no_directory(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        for bad in ("NOT-A-GUID", new_guid().upper(), f"{new_guid()}\n", ""):
            resp = await w.register_group(w.admin, w.root, bad)
            assert resp.status_code == 422, (bad, resp.text)
            assert slug_of(resp) == "validation-error"
        extra = await w.client.post(
            GROUPS,
            json={"owner_folder_id": w.root, "object_id": new_guid(), "is_active": False},
            headers=auth(w.admin),
        )
        assert extra.status_code == 422, extra.text
        assert w.directory.calls == []
        assert w.rows("group") == []


async def test_with_several_tenants_the_tenant_is_explicit_and_graph_is_asked_in_that_tenant(
    open_world: OpenWorld, fake_issuer: FakeIssuer
) -> None:
    second = fake_issuer.entra_issuer_config(tenant_id=OTHER_TENANT, namespace="entra-b")
    async with open_world(extra_issuers=[second]) as w:
        oid = w.group("Tenant B group", tenant_id=OTHER_TENANT)
        missing = await w.register_group(w.admin, w.root, oid)
        assert missing.status_code == 422, missing.text
        assert slug_of(missing) == "validation-error"
        assert w.directory.calls == []
        created = await w.register_group(w.admin, w.root, oid, tenant_id=OTHER_TENANT)
        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == OTHER_TENANT
        assert w.directory.calls == [("group", OTHER_TENANT, oid)]
        # The same GUID is not known in the default tenant: 422, not a cross-tenant match.
        wrong = await w.register_group(w.admin, w.org_a, oid, tenant_id=ENTRA_TENANT_ID)
        assert wrong.status_code == 422, wrong.text
        assert slug_of(wrong) == "unknown-principal"
        assert [r["tenant_id"] for r in w.rows("group")] == [OTHER_TENANT]
        assert len(w.events()) == 1


async def test_a_tenant_that_is_not_configured_is_422_and_graph_is_never_asked(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        oid = w.group()
        resp = await w.register_group(w.admin, w.root, oid, tenant_id=OTHER_TENANT)
        assert resp.status_code == 422, resp.text
        assert slug_of(resp) == "validation-error"
        # With exactly one tenant, an omitted tenant_id is that tenant.
        ok = await w.register_group(w.admin, w.root, oid)
        assert ok.status_code == 201, ok.text
        assert ok.json()["tenant_id"] == ENTRA_TENANT_ID
        assert w.directory.calls == [("group", ENTRA_TENANT_ID, oid)]


async def test_without_an_entra_issuer_registration_is_503(
    open_world: OpenWorld, asgi_client: Any
) -> None:
    from dc_identity.main import create_identity_app

    async with open_world() as w:
        local_only = w.env.auth_settings.model_copy(
            update={"issuers": [c for c in w.env.auth_settings.issuers if c.kind != "entra"]}
        )
        app = create_identity_app(w.env.settings, local_only, w.directory)
        assert app.state.entra_tenant_ids == ()
        async with asgi_client(app) as client:
            resp = await client.post(
                GROUPS,
                json={"owner_folder_id": w.root, "object_id": new_guid()},
                headers=auth(w.admin),
            )
        assert resp.status_code == 503, resp.text
        assert slug_of(resp) == "service-unavailable"
        assert w.rows("group") == []


async def test_a_project_or_team_cannot_own_a_registration(open_world: OpenWorld) -> None:
    async with open_world() as w:
        for owner in (w.project_a, w.team_a):
            resp = await w.register_group(w.admin, owner, w.group())
            assert resp.status_code == 422, resp.text
            assert slug_of(resp) == "owner-not-allowed"
        assert w.rows("group") == []
        assert w.directory.calls == []  # the owner is judged before Graph is asked
        assert w.events() == []


async def test_an_org_admin_registers_for_their_own_organization_only(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        own = w.group("A group")
        resp = await w.register_group(w.oa, w.org_a, own)
        assert resp.status_code == 201, resp.text
        assert resp.json()["owner_folder_id"] == w.org_a
        other_org = await w.register_group(w.oa, w.org_b, w.group())
        at_root = await w.register_group(w.oa, w.root, w.group())
        assert other_org.status_code == 404, other_org.text
        assert slug_of(other_org) == "not-found"
        assert at_root.status_code in (403, 404), at_root.text
        assert len(w.rows("group")) == 1
        assert w.directory.calls == [("group", ENTRA_TENANT_ID, own)]  # denied callers ask no one
        assert len(w.events()) == 1
        event = w.events()[0]
        assert [str(x) for x in event["scope_path"]] == [w.root, w.org_a]


async def test_a_team_editor_never_registers_a_group(open_world: OpenWorld) -> None:
    async with open_world() as w:
        editor = await w.local_token("te-1", "team_editor", w.team_a)
        for owner in (w.org_a, w.root, w.team_a):
            resp = await w.register_group(editor, owner, w.group())
            assert resp.status_code in (403, 404), resp.text
        assert w.rows("group") == []
        assert w.directory.calls == []


async def test_the_list_is_trimmed_to_owners_the_caller_can_read_and_empty_never_403(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        g_root, g_a, g_b = w.group("g-root"), w.group("g-a"), w.group("g-b")
        assert (await w.register_group(w.admin, w.root, g_root)).status_code == 201
        assert (await w.register_group(w.admin, w.org_a, g_a)).status_code == 201
        assert (await w.register_group(w.ob, w.org_b, g_b)).status_code == 201
        everything = await w.client.get(GROUPS, headers=auth(w.admin))
        mine = await w.client.get(GROUPS, headers=auth(w.oa))
        nobody = await w.local_token("nobody-1", None, None)
        empty = await w.client.get(GROUPS, headers=auth(nobody))
        assert {i["object_id"] for i in everything.json()["items"]} == {g_root, g_a, g_b}
        assert [i["object_id"] for i in mine.json()["items"]] == [g_a]
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"items": [], "next_cursor": None}


async def test_the_list_walks_every_row_in_id_order_by_cursor(open_world: OpenWorld) -> None:
    async with open_world() as w:
        created = []
        for n in range(3):
            resp = await w.register_group(w.admin, w.root, w.group(f"g{n}"))
            assert resp.status_code == 201, resp.text
            created.append(resp.json()["id"])
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(5):
            params: dict[str, Any] = {"limit": 1}
            if cursor:
                params["cursor"] = cursor
            page = (await w.client.get(GROUPS, params=params, headers=auth(w.admin))).json()
            assert len(page["items"]) <= 1
            seen.extend(i["id"] for i in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == sorted(created)
        assert len(seen) == len(set(seen)) == 3
        bad = await w.client.get(GROUPS, params={"cursor": "###"}, headers=auth(w.admin))
        assert bad.status_code == 422, bad.text


async def test_reading_a_group_registration_needs_the_owner_to_be_readable(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        g_a, g_b = w.group("g-a"), w.group("g-b")
        a = (await w.register_group(w.admin, w.org_a, g_a)).json()["id"]
        b = (await w.register_group(w.admin, w.org_b, g_b)).json()["id"]
        own = await w.client.get(f"{GROUPS}/{a}", headers=auth(w.oa))
        foreign = await w.client.get(f"{GROUPS}/{b}", headers=auth(w.oa))
        unknown = await w.client.get(f"{GROUPS}/{uuid.uuid4()}", headers=auth(w.oa))
        assert own.status_code == 200, own.text
        assert own.json()["object_id"] == g_a
        assert foreign.status_code == 404, foreign.text
        assert unknown.status_code == 404, unknown.text
        assert foreign.json()["detail"] == unknown.json()["detail"]


async def test_an_org_admin_cannot_delete_another_organizations_registration(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        reg = (await w.register_group(w.admin, w.org_b, w.group())).json()["id"]
        resp = await w.client.delete(f"{GROUPS}/{reg}", headers=auth(w.oa))
        assert resp.status_code == 404, resp.text
        assert len(w.rows("group")) == 1
        assert [e["action"] for e in w.events()] == ["entra_registration.created"]


async def test_deleting_a_registration_ends_admission_and_removes_its_grants(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        group, alice = w.group("Data Eng"), new_guid()
        w.directory.add_principal("user", alice, groups=[group])
        token = w.fake_issuer.mint_entra(alice)
        reg = (await w.register_group(w.admin, w.org_a, group)).json()["id"]
        w.grant_sql(w.env.db_url, group, "team_reader", w.team_a, principal_type="entra_group")
        admitted = await w.client.get(f"{FOLDERS}/{w.team_a}", headers=auth(token))
        assert admitted.status_code == 200, admitted.text

        deleted = await w.client.delete(f"{GROUPS}/{reg}", headers=auth(w.oa))
        assert deleted.status_code == 204, deleted.text
        assert w.rows("group") == []
        assert (
            w.sql(
                w.env.db_url,
                "SELECT count(*) AS n FROM role_assignments WHERE principal_id = %s",
                group,
            )[0]["n"]
            == 0
        )
        refused = await w.client.get(f"{FOLDERS}/{w.team_a}", headers=auth(token))
        assert refused.status_code == 403, refused.text
        assert slug_of(refused) == "principal-not-registered"
        events = w.events()
        assert [e["action"] for e in events] == [
            "entra_registration.created",
            "entra_registration.deleted",
        ]
        gone = events[1]
        assert gone["aggregate_id"] == reg
        assert gone["after"] is None
        assert gone["before"]["object_id"] == group
        assert gone["before"]["removed_grants"] == 1
        assert len(gone["before"]["removed_grant_ids"]) == 1
        again = await w.client.delete(f"{GROUPS}/{reg}", headers=auth(w.oa))
        assert again.status_code == 404, again.text


async def test_the_deleted_event_lists_at_most_twenty_removed_grant_ids(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        group = w.group()
        reg = (await w.register_group(w.admin, w.root, group)).json()["id"]
        roles = (
            "global_admin",
            "global_reader",
            "org_admin",
            "org_reader",
            "project_admin",
            "project_editor",
            "team_admin",
            "team_editor",
            "team_reader",
            "policy_manager",
            "consumer",
        )
        for folder in (w.root, w.org_a, w.project_a):
            for role in roles:
                w.grant_sql(w.env.db_url, group, role, folder, principal_type="entra_group")
        deleted = await w.client.delete(f"{GROUPS}/{reg}", headers=auth(w.admin))
        assert deleted.status_code == 204, deleted.text
        before = w.events()[-1]["before"]
        assert before["removed_grants"] == 33
        assert len(before["removed_grant_ids"]) == 20
        assert before["removed_grant_ids"] == sorted(before["removed_grant_ids"])


async def test_deleting_a_registration_is_bounded_by_the_grant_ceiling(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        group = w.group()
        reg = (await w.register_group(w.oa, w.org_a, group)).json()["id"]
        # A Global admin granted a role at ROOT to this group; the Org admin holds nothing there.
        w.grant_sql(w.env.db_url, group, "global_reader", w.root, principal_type="entra_group")
        resp = await w.client.delete(f"{GROUPS}/{reg}", headers=auth(w.oa))
        assert resp.status_code == 403, resp.text
        assert slug_of(resp) == "grant-ceiling-exceeded"
        assert len(w.rows("group")) == 1
        assert (
            w.sql(
                w.env.db_url,
                "SELECT count(*) AS n FROM role_assignments WHERE principal_id = %s",
                group,
            )[0]["n"]
            == 1
        )
        assert [e["action"] for e in w.events()] == ["entra_registration.created"]


async def test_every_registration_route_is_401_without_a_token(open_world: OpenWorld) -> None:
    async with open_world() as w:
        some = uuid.uuid4()
        for method, path in (
            ("POST", GROUPS),
            ("GET", GROUPS),
            ("GET", f"{GROUPS}/{some}"),
            ("DELETE", f"{GROUPS}/{some}"),
        ):
            body = {"owner_folder_id": str(some), "object_id": new_guid()}
            resp = await w.client.request(method, path, json=body if method == "POST" else None)
            assert resp.status_code == 401, (method, path, resp.text)


# --- Task 2: Entra users and service principals --------------------------------------------------

USERS = f"{IDENTITY}/entra/users"
SERVICE_PRINCIPALS = f"{IDENTITY}/entra/service-principals"


def principal(oid: str, tenant_id: str = ENTRA_TENANT_ID) -> str:
    return f"entra:{tenant_id}:{oid}"


async def register_user(w: World, token: str, oid: str, *, tenant_id: str | None = None) -> Any:
    body: dict[str, Any] = {"object_id": oid}
    if tenant_id is not None:
        body["tenant_id"] = tenant_id
    return await w.client.post(USERS, json=body, headers=auth(token))


async def register_sp(
    w: World, token: str, owner: str, oid: str, *, tenant_id: str | None = None
) -> Any:
    body: dict[str, Any] = {"owner_folder_id": owner, "object_id": oid}
    if tenant_id is not None:
        body["tenant_id"] = tenant_id
    return await w.client.post(SERVICE_PRINCIPALS, json=body, headers=auth(token))


def entra_users(w: World) -> list[dict[str, Any]]:
    return w.sql(w.env.db_url, "SELECT * FROM entra_users ORDER BY id")


async def jit_service_principal(w: World, owner: str, name: str) -> str:
    """A service principal admitted through a group owned by ``owner``: it gets its JIT row."""
    group, sp = w.group(f"{name} group"), new_guid()
    w.directory.add_principal("service_principal", sp, groups=[group], display_name=name)
    assert (await w.register_group(w.admin, owner, group)).status_code == 201
    token = w.fake_issuer.mint_entra(sp, token_type="app")
    ok = await w.client.get(FOLDERS, headers=auth(token))
    assert ok.status_code == 200, ok.text
    return sp


async def test_a_global_admin_registers_an_entra_user_before_the_first_sign_in(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        frank = new_guid()
        w.directory.add_principal("user", frank, display_name="Frank")
        assert entra_users(w) == []
        resp = await register_user(w, w.admin, frank)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["principal_id"] == principal(frank)
        assert body["object_id"] == frank
        assert body["tenant_id"] == ENTRA_TENANT_ID
        assert body["display_name"] == "Frank"
        assert body["preferred_username"] is None
        assert body["registered_directly"] is True
        assert body["is_active"] is True
        assert resp.headers["location"] == f"{USERS}/{body['id']}"
        # No token of frank has been presented, yet his record exists.
        rows = entra_users(w)
        assert [str(r["id"]) for r in rows] == [body["id"]]
        events = w.events()
        assert [e["action"] for e in events] == ["entra_registration.created"]
        registration = w.rows("user")[0]
        assert registration["owner_folder_id"] is None
        assert events[0]["after"] == {
            "id": str(registration["id"]),
            "kind": "user",
            "tenant_id": ENTRA_TENANT_ID,
            "object_id": frank,
            "owner_folder_id": None,
            "display_name": "Frank",
            "is_active": True,
            "directly_registered": True,
            "entra_user_id": body["id"],
        }
        assert [str(x) for x in events[0]["scope_path"]] == [w.root]


async def test_a_user_registration_is_confirmed_unique_and_global_admin_only(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        frank, a_service_principal = new_guid(), new_guid()
        w.directory.add_principal("user", frank, display_name="Frank")
        w.directory.add_principal("service_principal", a_service_principal)
        assert (await register_user(w, w.admin, frank)).status_code == 201
        again = await register_user(w, w.admin, frank)
        assert again.status_code == 409, again.text
        assert slug_of(again) == "conflict"
        for unknown in (new_guid(), a_service_principal):
            resp = await register_user(w, w.admin, unknown)
            assert resp.status_code == 422, resp.text
            assert slug_of(resp) == "unknown-principal"
        denied = await register_user(w, w.oa, new_guid())
        assert denied.status_code == 403, denied.text
        other = new_guid()
        w.directory.add_principal("user", other)
        w.directory.fail = True
        outage = await register_user(w, w.admin, other)
        assert outage.status_code == 503, outage.text
        assert slug_of(outage) == "service-unavailable"
        assert len(w.rows("user")) == 1
        assert len(entra_users(w)) == 1
        assert len(w.events()) == 1


async def test_registering_a_user_who_already_signed_in_reuses_the_entra_users_row(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        group, alice = w.group(), new_guid()
        w.directory.add_principal("user", alice, groups=[group], display_name="Alice")
        assert (await w.register_group(w.admin, w.root, group)).status_code == 201
        first = await w.client.get(FOLDERS, headers=auth(w.fake_issuer.mint_entra(alice)))
        assert first.status_code == 200, first.text
        jit = entra_users(w)
        assert len(jit) == 1
        resp = await register_user(w, w.admin, alice)
        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] == str(jit[0]["id"])
        assert resp.json()["registered_directly"] is True
        assert resp.json()["preferred_username"] == "user@example.test"
        assert len(entra_users(w)) == 1
        registrations = [e for e in w.events() if e["aggregate_type"] == "entra_registration"]
        assert [e["action"] for e in registrations] == [
            "entra_registration.created",
            "entra_registration.created",
        ]


async def test_entra_users_are_listed_in_id_order_and_read_by_a_global_admin_only(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        group, alice, frank = w.group(), new_guid(), new_guid()
        w.directory.add_principal("user", alice, groups=[group], display_name="Alice")
        w.directory.add_principal("user", frank, display_name="Frank")
        assert (await w.register_group(w.admin, w.root, group)).status_code == 201
        assert (
            await w.client.get(FOLDERS, headers=auth(w.fake_issuer.mint_entra(alice)))
        ).status_code == 200
        frank_row = (await register_user(w, w.admin, frank)).json()
        listed = await w.client.get(USERS, headers=auth(w.admin))
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert [i["object_id"] for i in items] == [alice, frank]
        assert [i["registered_directly"] for i in items] == [False, True]
        assert [i["id"] for i in items] == sorted(i["id"] for i in items)
        page1 = (await w.client.get(USERS, params={"limit": 1}, headers=auth(w.admin))).json()
        page2 = (
            await w.client.get(
                USERS, params={"limit": 1, "cursor": page1["next_cursor"]}, headers=auth(w.admin)
            )
        ).json()
        assert [page1["items"][0]["object_id"], page2["items"][0]["object_id"]] == [alice, frank]
        assert page2["next_cursor"] is None
        one = await w.client.get(f"{USERS}/{frank_row['id']}", headers=auth(w.admin))
        assert one.status_code == 200, one.text
        assert one.json()["principal_id"] == principal(frank)
        assert (
            await w.client.get(f"{USERS}/{uuid.uuid4()}", headers=auth(w.admin))
        ).status_code == 404
        assert (await w.client.get(USERS, headers=auth(w.oa))).status_code == 403
        assert (
            await w.client.get(f"{USERS}/{frank_row['id']}", headers=auth(w.oa))
        ).status_code == 403


async def test_a_directly_registered_user_with_no_groups_is_admitted_and_holds_nothing(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        frank = new_guid()
        w.directory.add_principal("user", frank, display_name="Frank")
        assert (await register_user(w, w.admin, frank)).status_code == 201
        token = w.fake_issuer.mint_entra(frank)
        admitted = await w.client.get(FOLDERS, headers=auth(token))
        nothing = await w.client.get(f"{FOLDERS}/{w.team_a}", headers=auth(token))
        assert admitted.status_code == 200, admitted.text
        assert admitted.json()["items"] == []
        assert nothing.status_code == 404, nothing.text


async def test_an_org_admin_registers_a_service_principal_for_its_own_organization(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        sp = new_guid()
        w.directory.add_principal("service_principal", sp, display_name="Pipeline")
        resp = await register_sp(w, w.oa, w.org_a, sp)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["principal_id"] == principal(sp)
        assert body["display_name"] == "Pipeline"
        assert body["owner_folder_id"] == w.org_a
        assert body["directly_registered"] is True
        assert body["is_active"] is True
        assert resp.headers["location"] == f"{SERVICE_PRINCIPALS}/{body['id']}"
        events = w.events()
        assert [e["action"] for e in events] == ["entra_registration.created"]
        assert events[0]["after"]["kind"] == "service_principal"
        assert events[0]["after"]["directly_registered"] is True
        assert [str(x) for x in events[0]["scope_path"]] == [w.root, w.org_a]
        # Admitted with no group, and registration grants nothing: folders stay 404.
        token = w.fake_issuer.mint_entra(sp, token_type="app")
        assert (await w.client.get(FOLDERS, headers=auth(token))).status_code == 200
        assert (await w.client.get(f"{FOLDERS}/{w.team_a}", headers=auth(token))).status_code == 404


async def test_a_service_principal_registration_is_confirmed_owned_and_unique(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        sp, a_user = new_guid(), new_guid()
        w.directory.add_principal("service_principal", sp)
        w.directory.add_principal("user", a_user)
        other_org = await register_sp(w, w.oa, w.org_b, sp)
        assert other_org.status_code == 404, other_org.text
        for unknown in (new_guid(), a_user):
            resp = await register_sp(w, w.oa, w.org_a, unknown)
            assert resp.status_code == 422, resp.text
            assert slug_of(resp) == "unknown-principal"
        project = await register_sp(w, w.admin, w.project_a, sp)
        assert project.status_code == 422, project.text
        assert slug_of(project) == "owner-not-allowed"
        w.directory.fail = True
        outage = await register_sp(w, w.oa, w.org_a, sp)
        assert outage.status_code == 503, outage.text
        w.directory.fail = False
        assert w.rows("service_principal") == []
        assert w.events() == []
        assert (await register_sp(w, w.oa, w.org_a, sp)).status_code == 201
        again = await register_sp(w, w.oa, w.org_a, sp)
        assert again.status_code == 409, again.text
        assert slug_of(again) == "conflict"
        assert len(w.rows("service_principal")) == 1
        assert len(w.events()) == 1


async def test_registering_a_just_in_time_service_principal_of_the_same_owner_upgrades_the_row(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        sp = await jit_service_principal(w, w.org_a, "Etl")
        [jit] = w.rows("service_principal")
        assert jit["directly_registered"] is False
        assert jit["owner_folder_id"] is not None and str(jit["owner_folder_id"]) == w.org_a
        before = len(w.events())
        resp = await register_sp(w, w.oa, w.org_a, sp)
        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] == str(jit["id"])
        assert resp.json()["directly_registered"] is True
        [row] = w.rows("service_principal")
        assert row["id"] == jit["id"]
        assert row["directly_registered"] is True
        events = w.events()
        assert len(events) == before + 1
        assert events[-1]["action"] == "entra_registration.updated"
        assert events[-1]["aggregate_id"] == str(jit["id"])
        assert events[-1]["before"]["directly_registered"] is False
        assert events[-1]["after"]["directly_registered"] is True
        assert [e["action"] for e in events].count("entra_registration.updated") == 1
        # Now directly registered: a second registration is a conflict.
        again = await register_sp(w, w.oa, w.org_a, sp)
        assert again.status_code == 409, again.text


async def test_a_just_in_time_service_principal_of_another_owner_is_a_conflict_and_unchanged(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        sp = await jit_service_principal(w, w.org_a, "Etl")
        [jit] = w.rows("service_principal")
        before = len(w.events())
        resp = await register_sp(w, w.admin, w.root, sp)
        assert resp.status_code == 409, resp.text
        assert slug_of(resp) == "conflict"
        [row] = w.rows("service_principal")
        assert row["directly_registered"] is False
        assert row["owner_folder_id"] == jit["owner_folder_id"]
        assert len(w.events()) == before


async def test_service_principals_are_listed_trimmed_by_owner_with_directly_registered(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        jit_sp = await jit_service_principal(w, w.org_a, "Jit")
        direct_a, direct_b = new_guid(), new_guid()
        for sp in (direct_a, direct_b):
            w.directory.add_principal("service_principal", sp)
        a = (await register_sp(w, w.oa, w.org_a, direct_a)).json()
        b = (await register_sp(w, w.ob, w.org_b, direct_b)).json()
        everything = (await w.client.get(SERVICE_PRINCIPALS, headers=auth(w.admin))).json()["items"]
        mine = (await w.client.get(SERVICE_PRINCIPALS, headers=auth(w.oa))).json()["items"]
        assert [i["object_id"] for i in everything] == [jit_sp, direct_a, direct_b]
        assert [i["directly_registered"] for i in everything] == [False, True, True]
        assert [i["object_id"] for i in mine] == [jit_sp, direct_a]
        nobody = await w.local_token("nobody-2", None, None)
        empty = await w.client.get(SERVICE_PRINCIPALS, headers=auth(nobody))
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"items": [], "next_cursor": None}
        one = await w.client.get(f"{SERVICE_PRINCIPALS}/{a['id']}", headers=auth(w.oa))
        foreign = await w.client.get(f"{SERVICE_PRINCIPALS}/{b['id']}", headers=auth(w.oa))
        unknown = await w.client.get(f"{SERVICE_PRINCIPALS}/{uuid.uuid4()}", headers=auth(w.oa))
        assert one.status_code == 200, one.text
        assert one.json()["principal_id"] == principal(direct_a)
        assert foreign.status_code == 404, foreign.text
        assert unknown.status_code == 404, unknown.text
        # A group registration id is not a service principal registration.
        group_reg = (await w.register_group(w.admin, w.root, w.group())).json()["id"]
        wrong_kind = await w.client.get(f"{SERVICE_PRINCIPALS}/{group_reg}", headers=auth(w.admin))
        assert wrong_kind.status_code == 404, wrong_kind.text


async def test_users_and_service_principals_take_the_tenant_like_groups_do(
    open_world: OpenWorld, fake_issuer: FakeIssuer
) -> None:
    second = fake_issuer.entra_issuer_config(tenant_id=OTHER_TENANT, namespace="entra-b")
    async with open_world(extra_issuers=[second]) as w:
        user, sp = new_guid(), new_guid()
        w.directory.add_principal("user", user, tenant_id=OTHER_TENANT, display_name="B user")
        w.directory.add_principal("service_principal", sp, tenant_id=OTHER_TENANT)
        assert (await register_user(w, w.admin, user)).status_code == 422
        assert (await register_sp(w, w.admin, w.root, sp)).status_code == 422
        assert w.directory.calls == []
        u = await register_user(w, w.admin, user, tenant_id=OTHER_TENANT)
        s = await register_sp(w, w.admin, w.root, sp, tenant_id=OTHER_TENANT)
        assert u.status_code == 201, u.text
        assert s.status_code == 201, s.text
        assert u.json()["principal_id"] == principal(user, OTHER_TENANT)
        assert s.json()["principal_id"] == principal(sp, OTHER_TENANT)
        assert w.directory.calls == [
            ("lookup", OTHER_TENANT, user),
            ("lookup", OTHER_TENANT, sp),
        ]
        unconfigured = new_guid()
        bad = await register_user(w, w.admin, unconfigured, tenant_id=new_guid())
        assert bad.status_code == 422, bad.text


async def test_the_new_entra_routes_are_401_without_a_token(open_world: OpenWorld) -> None:
    async with open_world() as w:
        some = uuid.uuid4()
        bodies: dict[str, dict[str, Any]] = {
            USERS: {"object_id": new_guid()},
            SERVICE_PRINCIPALS: {"owner_folder_id": str(some), "object_id": new_guid()},
        }
        for base, body in bodies.items():
            for method, path in (
                ("POST", base),
                ("GET", base),
                ("GET", f"{base}/{some}"),
            ):
                resp = await w.client.request(method, path, json=body if method == "POST" else None)
                assert resp.status_code == 401, (method, path, resp.text)
