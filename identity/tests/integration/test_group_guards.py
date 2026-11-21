"""Group guards: membership never escalates and groups never cross organizations.

D-33 and A3 (a membership change counts as granting every role the group holds, including removal
and deletion), D-27 (who may own and manage groups), RBAC-08 (a group is confined to the
Organization that owns it) and T-02-40 to T-02-46. The ceiling matrix is driven from
``dc_auth.roles.GRANTABLE`` and ``ROLE_LEVELS``, so the tests follow the model.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from dc_auth.roles import GRANTABLE, ROLE_LEVELS, Role

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
GROUPS = f"{IDENTITY}/groups"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
CreateUser = Callable[..., Awaitable[Any]]
Login = Callable[..., Awaitable[str]]
Grant = Callable[..., Awaitable[Any]]
GrantSql = Callable[..., None]
CreateGroup = Callable[..., Awaitable[str]]
AddMember = Callable[..., Awaitable[Any]]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def slug_of(resp: Any) -> str:
    return str(resp.json()["type"]).rsplit(":", 1)[-1]


def neutral(resp: Any) -> dict[str, Any]:
    """A problem body without the per-request id, for byte-for-byte comparison."""
    body = dict(resp.json())
    body.pop("request_id", None)
    return body


@dataclass
class Person:
    user: Any
    token: str


@dataclass
class Env:
    """ROOT -> O -> (P -> T) and (P2 -> T2); a second organization O2 -> Q -> U."""

    client: Any
    env: Any
    sql: Sql
    admin: str
    root: str
    org: str
    project: str
    team: str
    project2: str
    team2: str
    org2: str
    project_o2: str
    team_o2: str
    oa: Person  # org_admin at O

    def kinds(self) -> dict[str, str]:
        return {
            self.org: "organization",
            self.project: "project",
            self.team: "team",
            self.project2: "project",
            self.team2: "team",
        }

    def outbox(self, action: str) -> list[dict[str, Any]]:
        return self.sql(
            self.env.db_url,
            "SELECT id, aggregate_id, before, after FROM outbox WHERE action = %s ORDER BY id",
            action,
        )


async def make_folder(client: Any, token: str, parent: str, slug: str) -> str:
    resp = await client.post(
        FOLDERS,
        json={"parent_id": parent, "slug": slug, "display_name": slug},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def person(
    e: Env,
    grant_sql: GrantSql,
    create_user: CreateUser,
    login: Login,
    name: str,
    role: str,
    at: str,
) -> Person:
    user = await create_user(e.client, e.admin, name)
    grant_sql(e.env.db_url, user.principal_id, role, at)
    return Person(user, await login(e.client, user.username, user.password))


async def build(
    client: Any,
    env: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant_sql: GrantSql,
) -> Env:
    admin = await admin_token(client, env)
    root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
    org, project, team = await folder_chain(client, admin, root, "x")
    project2 = await make_folder(client, admin, org, "x-p2")
    team2 = await make_folder(client, admin, project2, "x-p2-t")
    org2, project_o2, team_o2 = await folder_chain(client, admin, root, "y")
    e = Env(
        client, env, sql, admin, root, org, project, team, project2, team2, org2, project_o2,
        team_o2, Person(None, ""),
    )  # fmt: skip
    e.oa = await person(e, grant_sql, create_user, login, "oa-guard", "org_admin", org)
    return e


# ---------------------------------------------------------------------------------------------
# D-33 / A3: the membership ceiling


async def test_the_membership_ceiling_follows_grantable_for_every_role_in_the_organization(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant: Grant,
    grant_sql: GrantSql,
    create_group: CreateGroup,
    add_member: AddMember,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        e = await build(client, env, admin_token, sql, folder_chain, create_user, login, grant_sql)
        member = await create_user(client, e.admin, "member-ceiling")
        attempted = 0
        for folder, kind in e.kinds().items():
            for role in Role:
                if kind not in ROLE_LEVELS[role]:
                    continue
                attempted += 1
                name = f"{role.value}-{folder[:8]}"
                group = await create_group(client, e.oa.token, e.org, name)
                made = await grant(client, e.admin, folder, group, role.value, "local_group")
                assert made.status_code == 201, (role, made.text)
                member_url = f"{GROUPS}/{group}/members"
                if role in GRANTABLE[Role.ORG_ADMIN]:
                    added = await add_member(client, e.oa.token, group, member.username)
                    assert added.status_code == 201, (role, added.text)
                    removed = await client.delete(
                        f"{member_url}/{member.principal_id}", headers=auth(e.oa.token)
                    )
                    assert removed.status_code == 204, (role, removed.text)
                    deleted = await client.delete(f"{GROUPS}/{group}", headers=auth(e.oa.token))
                    assert deleted.status_code == 204, (role, deleted.text)
                else:
                    refused = await add_member(client, e.oa.token, group, member.username)
                    assert refused.status_code == 403, (role, refused.text)
                    assert slug_of(refused) == "grant-ceiling-exceeded", role
                    assert (
                        await add_member(client, e.admin, group, member.username)
                    ).status_code == 201
                    gone = f"{member_url}/{member.principal_id}"
                    refused = await client.delete(gone, headers=auth(e.oa.token))
                    assert refused.status_code == 403, (role, refused.text)
                    assert slug_of(refused) == "grant-ceiling-exceeded", role
                    refused = await client.delete(f"{GROUPS}/{group}", headers=auth(e.oa.token))
                    assert refused.status_code == 403, (role, refused.text)
                    assert slug_of(refused) == "grant-ceiling-exceeded", role
                    # Nothing changed by the refusals; the Global admin can do all three.
                    assert (await client.delete(gone, headers=auth(e.admin))).status_code == 204
                    assert (
                        await add_member(client, e.admin, group, member.username)
                    ).status_code == 201
                    deleted = await client.delete(f"{GROUPS}/{group}", headers=auth(e.admin))
                    assert deleted.status_code == 204, (role, deleted.text)
        assert attempted > 0


async def test_a_group_holding_a_role_outside_the_organization_cannot_be_changed_by_its_admin(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant: Grant,
    grant_sql: GrantSql,
    create_group: CreateGroup,
    add_member: AddMember,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        e = await build(client, env, admin_token, sql, folder_chain, create_user, login, grant_sql)
        member = await create_user(client, e.admin, "member-cross")

        # Only a Global admin may give an O-owned group a role in O2, so the state is built by
        # SQL: the guard must still hold on rows it did not create itself.
        group = await create_group(client, e.oa.token, e.org, "Straddler")
        assert (
            await grant(client, e.admin, e.team, group, "team_editor", "local_group")
        ).status_code == 201
        grant_sql(env.db_url, group, "project_admin", e.project_o2, "local_group")
        refused = await add_member(client, e.oa.token, group, member.username)
        assert refused.status_code == 403, refused.text
        assert slug_of(refused) == "grant-ceiling-exceeded"
        assert (
            await client.delete(f"{GROUPS}/{group}", headers=auth(e.oa.token))
        ).status_code == 403
        assert (await add_member(client, e.admin, group, member.username)).status_code == 201

        # A ROOT-owned group is not the Organization admin's to manage at all: 404.
        rooted = await create_group(client, e.admin, e.root, "Rooted")
        for resp in (
            await add_member(client, e.oa.token, rooted, member.username),
            await client.delete(f"{GROUPS}/{rooted}", headers=auth(e.oa.token)),
            await client.get(f"{GROUPS}/{rooted}/members", headers=auth(e.oa.token)),
        ):
            assert resp.status_code == 404, resp.text


async def test_nobody_adds_themselves_or_grants_to_a_group_they_belong_to(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant: Grant,
    grant_sql: GrantSql,
    create_group: CreateGroup,
    add_member: AddMember,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        e = await build(client, env, admin_token, sql, folder_chain, create_user, login, grant_sql)
        ta = await person(e, grant_sql, create_user, login, "ta-guard", "team_admin", e.team)

        h = await create_group(client, e.oa.token, e.org, "Has oa and ta")
        # oa adding themselves, even to a group holding nothing: 403 self-grant-forbidden.
        own = await add_member(client, e.oa.token, h, e.oa.user.username)
        assert own.status_code == 403, own.text
        assert slug_of(own) == "self-grant-forbidden"
        by_id = await client.post(
            f"{GROUPS}/{h}/members",
            json={"principal_id": e.oa.user.principal_id},
            headers=auth(e.oa.token),
        )
        assert by_id.status_code == 403
        assert slug_of(by_id) == "self-grant-forbidden"
        assert e.outbox("group.member_added") == []

        # The Global admin makes both members; neither may then grant to the group they are in.
        assert (await add_member(client, e.admin, h, e.oa.user.username)).status_code == 201
        assert (await add_member(client, e.admin, h, ta.user.username)).status_code == 201
        for role in GRANTABLE[Role.ORG_ADMIN]:
            if ROLE_LEVELS[role] & {"organization"}:
                resp = await grant(client, e.oa.token, e.org, h, role.value, "local_group")
                assert resp.status_code == 403, (role, resp.text)
                assert slug_of(resp) == "self-grant-forbidden", role
        resp = await grant(client, e.oa.token, e.team, h, "team_reader", "local_group")
        assert resp.status_code == 403
        assert slug_of(resp) == "self-grant-forbidden"
        resp = await grant(client, ta.token, e.team, h, "team_reader", "local_group")
        assert resp.status_code == 403, resp.text
        assert slug_of(resp) == "self-grant-forbidden"
        # Someone outside the group may grant to it.
        assert (
            await grant(client, e.admin, e.team, h, "team_reader", "local_group")
        ).status_code == 201
        assert e.outbox("grant.created")[0]["after"]["principal_id"] == h


# ---------------------------------------------------------------------------------------------
# D-27: who may own and manage groups


async def test_only_organization_and_global_admins_create_groups_and_only_for_root_or_an_org(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant_sql: GrantSql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        e = await build(client, env, admin_token, sql, folder_chain, create_user, login, grant_sql)
        pa = await person(e, grant_sql, create_user, login, "pa-guard", "project_admin", e.project)
        ta = await person(e, grant_sql, create_user, login, "ta-guard2", "team_admin", e.team)
        reader = await person(e, grant_sql, create_user, login, "or-guard", "org_reader", e.org)

        def post(token: str, owner: str, name: str = "Anything") -> Awaitable[Any]:
            return client.post(
                GROUPS, json={"owner_folder_id": owner, "name": name}, headers=auth(token)
            )

        # Their grants do not apply at O (they cannot even read it): 404. A reader can read O
        # but may not create groups: 403.
        for who in (pa, ta):
            assert (await post(who.token, e.org)).status_code == 404
        assert (await post(reader.token, e.org)).status_code == 403
        assert (await post(e.oa.token, e.org2)).status_code == 404
        assert (await post(e.oa.token, e.root)).status_code == 404
        # Owner kind: only ROOT or an Organization.
        for folder in (e.project, e.team):
            resp = await post(e.oa.token, folder)
            assert resp.status_code == 422, resp.text
            assert slug_of(resp) == "group-owner-not-allowed"
        resp = await post(e.admin, e.project)
        assert resp.status_code == 422
        assert slug_of(resp) == "group-owner-not-allowed"
        # The Global admin owns groups at ROOT and at any Organization.
        assert (await post(e.admin, e.root, "Root group")).status_code == 201
        assert (await post(e.admin, e.org2, "Other org group")).status_code == 201
        assert (await post(e.oa.token, e.org, "Mine")).status_code == 201
        # Unknown owner: 404 for everyone.
        assert (await post(e.admin, str(uuid.uuid4()))).status_code == 404
        assert len(e.outbox("group.created")) == 3


# ---------------------------------------------------------------------------------------------
# RBAC-08: groups never cross organizations


async def test_a_group_is_grantable_only_inside_its_owner_organization_or_anywhere_when_root_owned(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant: Grant,
    grant_sql: GrantSql,
    create_group: CreateGroup,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        e = await build(client, env, admin_token, sql, folder_chain, create_user, login, grant_sql)
        oa2 = await person(e, grant_sql, create_user, login, "oa2-guard", "org_admin", e.org2)
        ta = await person(e, grant_sql, create_user, login, "ta-guard3", "team_admin", e.team)

        g = await create_group(client, e.oa.token, e.org, "Org one")
        rooted = await create_group(client, e.admin, e.root, "Everywhere")

        # An O-owned group elsewhere is a scope mismatch for a non-global admin who can read it
        # (here an Org admin of both organizations).
        both = await person(e, grant_sql, create_user, login, "both-guard", "org_admin", e.org)
        grant_sql(env.db_url, both.user.principal_id, "org_admin", e.org2)
        mismatch = await grant(client, both.token, e.team_o2, g, "team_reader", "local_group")
        assert mismatch.status_code == 422, mismatch.text
        assert slug_of(mismatch) == "group-scope-mismatch"
        mismatch = await grant(client, both.token, e.org2, g, "org_reader", "local_group")
        assert slug_of(mismatch) == "group-scope-mismatch"
        # A Global admin is not confined by the owner (quick 261119).
        anywhere = await grant(client, e.admin, e.team_o2, g, "team_reader", "local_group")
        assert anywhere.status_code == 201, anywhere.text
        # Another organization's admin gets the same 404 as for a random uuid, wherever they try.
        unknown = await grant(
            client, oa2.token, e.team_o2, str(uuid.uuid4()), "team_reader", "local_group"
        )
        assert unknown.status_code == 404
        for folder, role in (
            (e.team_o2, "team_reader"),
            (e.org2, "org_reader"),
            (e.project_o2, "project_editor"),
        ):
            resp = await grant(client, oa2.token, folder, g, role, "local_group")
            assert resp.status_code == 404, resp.text
            assert {**neutral(resp), "instance": ""} == {**neutral(unknown), "instance": ""}
        # Inside its own organization it is fine at every level.
        assert (
            await grant(client, e.oa.token, e.org, g, "org_reader", "local_group")
        ).status_code == 201
        assert (
            await grant(client, e.oa.token, e.team, g, "team_reader", "local_group")
        ).status_code == 201

        # A ROOT-owned group is grantable anywhere by any admin holding grant:manage there (A6).
        assert (
            await grant(client, e.oa.token, e.team, rooted, "team_editor", "local_group")
        ).status_code == 201
        assert (
            await grant(client, oa2.token, e.team_o2, rooted, "team_editor", "local_group")
        ).status_code == 201
        # Even with a block flag on T, a team admin granted at T grants a ROOT group at T.
        flagged = await client.put(
            f"{FOLDERS}/{e.team}/block-inheritance", json={"enabled": True}, headers=auth(e.admin)
        )
        assert flagged.status_code == 200, flagged.text
        resp = await grant(client, ta.token, e.team, rooted, "team_reader", "local_group")
        assert resp.status_code == 201, resp.text
        # ... and still only within its own ceiling.
        resp = await grant(client, ta.token, e.team, rooted, "team_admin", "local_group")
        assert resp.status_code == 403
        assert slug_of(resp) == "grant-ceiling-exceeded"


# ---------------------------------------------------------------------------------------------
# archived owners, unknown principals, races


async def test_an_archived_owner_blocks_creation_and_adding_but_not_removal_or_deletion(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant: Grant,
    grant_sql: GrantSql,
    create_group: CreateGroup,
    add_member: AddMember,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        e = await build(client, env, admin_token, sql, folder_chain, create_user, login, grant_sql)
        m1 = await create_user(client, e.admin, "member-arch-1")
        m2 = await create_user(client, e.admin, "member-arch-2")
        keep = await create_group(client, e.oa.token, e.org, "Keep")
        drop = await create_group(client, e.oa.token, e.org, "Drop")
        assert (await add_member(client, e.oa.token, keep, m1.username)).status_code == 201
        assert (
            await grant(client, e.oa.token, e.team, keep, "team_reader", "local_group")
        ).status_code == 201

        archived = await client.post(f"{FOLDERS}/{e.org}/archive", headers=auth(e.admin))
        assert archived.status_code == 200, archived.text

        created = await client.post(
            GROUPS, json={"owner_folder_id": e.org, "name": "Late"}, headers=auth(e.oa.token)
        )
        assert created.status_code == 409, created.text
        assert slug_of(created) == "folder-archived"
        added = await add_member(client, e.oa.token, keep, m2.username)
        assert added.status_code == 409, added.text
        assert slug_of(added) == "folder-archived"
        # Nothing new may be granted either (OQ3), but cleaning up is allowed.
        resp = await grant(client, e.oa.token, e.team, drop, "team_reader", "local_group")
        assert resp.status_code == 409
        removed = await client.delete(
            f"{GROUPS}/{keep}/members/{m1.principal_id}", headers=auth(e.oa.token)
        )
        assert removed.status_code == 204, removed.text
        assert (
            await client.delete(f"{GROUPS}/{keep}", headers=auth(e.oa.token))
        ).status_code == 204
        assert (
            await client.delete(f"{GROUPS}/{drop}", headers=auth(e.oa.token))
        ).status_code == 204
        # A group already holding the role is unaffected by the refused writes.
        assert len(e.outbox("group.created")) == 2
        assert len(e.outbox("group.member_added")) == 1


async def test_unresolvable_members_look_alike_and_malformed_bodies_are_validation_errors(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant_sql: GrantSql,
    create_group: CreateGroup,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        e = await build(client, env, admin_token, sql, folder_chain, create_user, login, grant_sql)
        group = await create_group(client, e.oa.token, e.org, "Lookalike")
        url = f"{GROUPS}/{group}/members"

        responses = [
            await client.post(url, json=body, headers=auth(e.oa.token))
            for body in (
                {"username": "nobody-here"},
                {"principal_id": f"local:{uuid.uuid4()}"},
                {"principal_id": "local:not-a-uuid"},
                {"principal_id": f"local:{str(uuid.uuid4()).upper()}"},
                {"principal_id": f"fake:{uuid.uuid4()}"},
            )
        ]
        for resp in responses:
            assert resp.status_code == 422, resp.text
            assert slug_of(resp) == "unknown-principal"
        assert len({str(neutral(r)) for r in responses}) == 1
        assert e.outbox("group.member_added") == []

        for body in (
            {"principal_id": "bad"},
            {"username": "a", "principal_id": f"local:{uuid.uuid4()}"},
            {},
            {"username": None},
            {"username": ""},
            {"username": "x", "extra": 1},
        ):
            resp = await client.post(url, json=body, headers=auth(e.oa.token))
            assert resp.status_code == 422, (body, resp.text)
            assert slug_of(resp) == "validation-error", body


async def test_a_member_add_racing_a_grant_to_the_group_is_serialised_concurrency(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant: Grant,
    grant_sql: GrantSql,
    create_group: CreateGroup,
    add_member: AddMember,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        e = await build(client, env, admin_token, sql, folder_chain, create_user, login, grant_sql)
        member = await create_user(client, e.admin, "member-race")
        outcomes: list[str] = []
        for i in range(5):
            group = await create_group(client, e.oa.token, e.org, f"Race {i}")
            granted, added = await asyncio.gather(
                grant(client, e.admin, e.org, group, "org_admin", "local_group"),
                add_member(client, e.oa.token, group, member.username),
            )
            assert granted.status_code == 201, granted.text
            grant_ids = [
                r["id"]
                for r in e.sql(
                    env.db_url,
                    "SELECT id FROM outbox WHERE action = 'grant.created' "
                    "AND after->>'principal_id' = %s",
                    group,
                )
            ]
            assert len(grant_ids) == 1
            if added.status_code == 201:
                add_ids = [
                    r["id"] for r in e.outbox("group.member_added") if r["aggregate_id"] == group
                ]
                assert len(add_ids) == 1
                assert add_ids[0] < grant_ids[0]  # decided before the grant existed
                outcomes.append("added-first")
            else:
                assert added.status_code == 403, added.text
                assert slug_of(added) == "grant-ceiling-exceeded"
                assert (
                    sql(env.db_url, "SELECT 1 FROM group_members WHERE group_id = %s", group) == []
                )
                outcomes.append("refused")
        assert len(outcomes) == 5
