"""Local groups: create, add members, grant a role to the group (RBAC-02, ROADMAP SC1).

The tracer proves the whole path an Organization admin walks: create an owned group, add a user by
username, grant Team editor to the group on a team folder, and the member immediately holds
``contract:create`` there.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
GROUPS = f"{IDENTITY}/groups"
CHECK = "/internal/v1/authz/check"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
CreateUser = Callable[..., Awaitable[Any]]
Login = Callable[..., Awaitable[str]]
Grant = Callable[..., Awaitable[Any]]
CreateGroup = Callable[..., Awaitable[str]]
AddMember = Callable[..., Awaitable[Any]]
GrantSql = Callable[..., None]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def slug_of(resp: Any) -> str:
    return str(resp.json()["type"]).rsplit(":", 1)[-1]


def root_id(sql: Sql, env: Any) -> str:
    return str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])


def events(sql: Sql, env: Any, like: str) -> list[dict[str, Any]]:
    return sql(
        env.db_url,
        "SELECT action, aggregate_type, aggregate_id, actor, before, after, scope_path "
        "FROM outbox WHERE action LIKE %s ORDER BY id",
        like,
    )


async def test_an_org_admin_creates_a_group_adds_a_user_and_grants_the_group_team_editor(
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
        admin = await admin_token(client, env)
        org, project, team = await folder_chain(client, admin, root_id(sql, env), "g")
        oa = await create_user(client, admin, "oa-1")
        dev = await create_user(client, admin, "dev-1")
        grant_sql(env.db_url, oa.principal_id, "org_admin", org)
        oa_token = await login(client, oa.username, oa.password)
        dev_token = await login(client, dev.username, dev.password)

        created = await client.post(
            GROUPS, json={"owner_folder_id": org, "name": "Ingest Devs"}, headers=auth(oa_token)
        )
        assert created.status_code == 201, created.text
        group = created.json()
        group_id = group["id"]
        assert created.headers["location"] == f"{GROUPS}/{group_id}"
        assert group["owner_folder_id"] == org
        assert group["name"] == "Ingest Devs"
        assert group["created_by"] == oa.principal_id
        assert str(uuid.UUID(group_id)) == group_id  # a bare lowercase uuid

        added = await add_member(client, oa_token, group_id, "dev-1")
        assert added.status_code == 201, added.text
        assert added.json()["member_id"] == dev.principal_id
        assert added.json()["member_type"] == "user"
        assert added.json()["username"] == "dev-1"

        granted = await grant(client, oa_token, team, group_id, "team_editor", "local_group")
        assert granted.status_code == 201, granted.text
        body = granted.json()
        assert body["principal_type"] == "local_group"
        assert body["principal_id"] == group_id
        stored = sql(
            env.db_url,
            "SELECT principal_id FROM role_assignments WHERE id = %s",
            body["id"],
        )
        assert stored[0]["principal_id"] == group_id

        # The member holds the role at once, and only at and below the grant.
        access = await client.get(f"{FOLDERS}/{team}/effective-access", headers=auth(dev_token))
        assert access.status_code == 200, access.text
        assert access.json()["roles"] == ["team_editor"]
        check = await client.post(
            CHECK,
            json={"permission": "contract:create", "folder_id": team},
            headers=auth(dev_token),
        )
        assert check.status_code == 200, check.text
        assert check.json()["outcome"] == "allow"
        assert (
            await client.get(f"{FOLDERS}/{project}", headers=auth(dev_token))
        ).status_code == 404

        actions = events(sql, env, "group.%") + events(sql, env, "grant.%")
        assert [e["action"] for e in actions] == [
            "group.created",
            "group.member_added",
            "grant.created",
        ]
        assert all(e["actor"]["id"] == oa.principal_id for e in actions)
        assert all(e["actor"]["type"] == "user" for e in actions)
        assert actions[1]["aggregate_type"] == "group"
        assert actions[1]["aggregate_id"] == group_id
        assert actions[1]["after"] == {
            "group_id": group_id,
            "member_type": "user",
            "member_id": dev.principal_id,
        }
        assert actions[0]["after"] == {
            "id": group_id,
            "owner_folder_id": org,
            "name": "Ingest Devs",
        }


async def test_group_grant_wire_shape_is_a_bare_lowercase_uuid(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    grant: Grant,
    create_group: CreateGroup,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        org, _, team = await folder_chain(client, admin, root_id(sql, env), "w")
        group_id = await create_group(client, admin, org, "Shape")

        for bad in (f"local:{group_id}", group_id.upper(), "not-a-uuid"):
            resp = await grant(client, admin, team, bad, "team_reader", "local_group")
            assert resp.status_code == 422, (bad, resp.text)
            assert slug_of(resp) == "validation-error"
        # A well-formed id that names no group is a 404, like any unknown group.
        unknown = await grant(client, admin, team, str(uuid.uuid4()), "team_reader", "local_group")
        assert unknown.status_code == 404, unknown.text
        assert slug_of(unknown) == "not-found"
        # A user id is not valid for a group grant, and vice versa.
        as_user = await grant(client, admin, team, group_id, "team_reader", "user")
        assert as_user.status_code == 422
        assert slug_of(as_user) == "validation-error"


async def test_membership_is_guarded_from_the_first_member(
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
        admin = await admin_token(client, env)
        org, _, _ = await folder_chain(client, admin, root_id(sql, env), "m")
        oa = await create_user(client, admin, "oa-m")
        other = await create_user(client, admin, "other-m")
        grant_sql(env.db_url, oa.principal_id, "org_admin", org)
        oa_token = await login(client, oa.username, oa.password)

        # Adding yourself is refused whatever the group holds (D-33).
        mine = await create_group(client, oa_token, org, "Mine")
        own = await add_member(client, oa_token, mine, "oa-m")
        assert own.status_code == 403, own.text
        assert slug_of(own) == "self-grant-forbidden"

        # A group holding org_admin at O (granted by the admin) is beyond oa's ceiling.
        high = await create_group(client, oa_token, org, "High")
        assert (
            await grant(client, admin, org, high, "org_admin", "local_group")
        ).status_code == 201
        refused = await add_member(client, oa_token, high, other.username)
        assert refused.status_code == 403, refused.text
        assert slug_of(refused) == "grant-ceiling-exceeded"
        assert (await add_member(client, admin, high, other.username)).status_code == 201
        assert len(events(sql, env, "group.member_added")) == 1


def neutral(resp: Any) -> dict[str, Any]:
    """A problem body without the per-request id and instance, for byte-for-byte comparison."""
    body = dict(resp.json())
    body.pop("request_id", None)
    body.pop("instance", None)
    return body


@dataclass
class World:
    """ROOT -> O -> P -> T, a second organization O2, oa (org_admin at O), dev and a ROOT group."""

    root: str
    org: str
    project: str
    team: str
    org2: str
    admin: str
    oa: Any
    oa_token: str
    dev: Any
    dev_token: str
    group: str  # "Ingest Devs", owned by O, holds team_editor at T, dev is a member


async def build_world(
    client: Any,
    env: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant: Grant,
    grant_sql: GrantSql,
    create_group: CreateGroup,
    add_member: AddMember,
) -> World:
    admin = await admin_token(client, env)
    root = root_id(sql, env)
    org, project, team = await folder_chain(client, admin, root, "w")
    org2, _, _ = await folder_chain(client, admin, root, "v")
    oa = await create_user(client, admin, "oa-w")
    dev = await create_user(client, admin, "dev-w")
    grant_sql(env.db_url, oa.principal_id, "org_admin", org)
    oa_token = await login(client, oa.username, oa.password)
    dev_token = await login(client, dev.username, dev.password)
    group = await create_group(client, oa_token, org, "Ingest Devs")
    assert (await add_member(client, oa_token, group, dev.username)).status_code == 201
    assert (
        await grant(client, oa_token, team, group, "team_editor", "local_group")
    ).status_code == 201
    return World(root, org, project, team, org2, admin, oa, oa_token, dev, dev_token, group)


async def test_groups_list_is_trimmed_to_what_the_caller_may_read_with_keyset_pages(
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
        w = await build_world(
            client, env, admin_token, sql, folder_chain, create_user, login, grant, grant_sql,
            create_group, add_member,
        )  # fmt: skip
        other = await create_group(client, w.admin, w.org2, "Elsewhere")
        rooted = await create_group(client, w.admin, w.root, "Everywhere")

        everything = await client.get(GROUPS, headers=auth(w.admin))
        assert everything.status_code == 200, everything.text
        assert {g["id"] for g in everything.json()["items"]} == {w.group, other, rooted}
        assert not {"total", "count"} & set(everything.json())
        ids = [g["id"] for g in everything.json()["items"]]
        assert ids == sorted(ids)

        mine = await client.get(GROUPS, headers=auth(w.oa_token))
        assert [g["id"] for g in mine.json()["items"]] == [w.group]
        # A member who is not an admin sees nothing, with a 200 and an empty page.
        member = await client.get(GROUPS, headers=auth(w.dev_token))
        assert member.status_code == 200
        assert member.json() == {"items": [], "next_cursor": None}

        filtered = await client.get(
            GROUPS, params={"owner_folder_id": w.org2}, headers=auth(w.admin)
        )
        assert [g["id"] for g in filtered.json()["items"]] == [other]
        hidden = await client.get(
            GROUPS, params={"owner_folder_id": w.org2}, headers=auth(w.oa_token)
        )
        assert hidden.json()["items"] == []

        first = await client.get(GROUPS, params={"limit": 2}, headers=auth(w.admin))
        assert len(first.json()["items"]) == 2
        assert first.json()["next_cursor"]
        second = await client.get(
            GROUPS,
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
            headers=auth(w.admin),
        )
        assert [g["id"] for g in first.json()["items"] + second.json()["items"]] == sorted(
            [w.group, other, rooted]
        )
        assert second.json()["next_cursor"] is None
        bad = await client.get(GROUPS, params={"cursor": "!!"}, headers=auth(w.admin))
        assert bad.status_code == 422


async def test_reading_a_group_is_404_for_members_and_other_organizations(
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
        w = await build_world(
            client, env, admin_token, sql, folder_chain, create_user, login, grant, grant_sql,
            create_group, add_member,
        )  # fmt: skip
        oa2 = await create_user(client, w.admin, "oa-v")
        grant_sql(env.db_url, oa2.principal_id, "org_admin", w.org2)
        oa2_token = await login(client, oa2.username, oa2.password)

        for token in (w.oa_token, w.admin):
            resp = await client.get(f"{GROUPS}/{w.group}", headers=auth(token))
            assert resp.status_code == 200, resp.text
            assert resp.json()["name"] == "Ingest Devs"
        unknown = await client.get(f"{GROUPS}/{uuid.uuid4()}", headers=auth(w.dev_token))
        for token in (w.dev_token, oa2_token):
            resp = await client.get(f"{GROUPS}/{w.group}", headers=auth(token))
            assert resp.status_code == 404
            assert neutral(resp) == neutral(unknown)
            members = await client.get(f"{GROUPS}/{w.group}/members", headers=auth(token))
            assert members.status_code == 404
        # No token at all is 401.
        assert (await client.get(f"{GROUPS}/{w.group}")).status_code == 401


async def test_rename_a_group_and_the_unique_name_rules(
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
        w = await build_world(
            client, env, admin_token, sql, folder_chain, create_user, login, grant, grant_sql,
            create_group, add_member,
        )  # fmt: skip
        taken = await create_group(client, w.oa_token, w.org, "Taken")

        renamed = await client.patch(
            f"{GROUPS}/{w.group}", json={"name": "Ingest Team"}, headers=auth(w.oa_token)
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "Ingest Team"
        again = await client.patch(
            f"{GROUPS}/{w.group}", json={"name": "Ingest Team"}, headers=auth(w.oa_token)
        )
        assert again.status_code == 200
        evs = events(sql, env, "group.renamed")
        assert len(evs) == 1
        assert evs[0]["before"]["name"] == "Ingest Devs"
        assert evs[0]["after"]["name"] == "Ingest Team"

        clash = await client.patch(
            f"{GROUPS}/{w.group}", json={"name": "TAKEN"}, headers=auth(w.oa_token)
        )
        assert clash.status_code == 409
        assert slug_of(clash) == "conflict"
        dup = await client.post(
            GROUPS,
            json={"owner_folder_id": w.org, "name": " taken "},
            headers=auth(w.oa_token),
        )
        assert dup.status_code == 409
        # The same name under another owner is fine.
        assert (
            await client.post(
                GROUPS, json={"owner_folder_id": w.org2, "name": "Taken"}, headers=auth(w.admin)
            )
        ).status_code == 201
        assert taken

        for body in (
            {},
            {"name": ""},
            {"name": "x" * 101},
            {"name": "ok", "id": "x"},
            {"name": None},
        ):
            resp = await client.patch(f"{GROUPS}/{w.group}", json=body, headers=auth(w.oa_token))
            assert resp.status_code == 422, body
        # A member who is not an admin cannot see the group, so cannot rename it.
        assert (
            await client.patch(
                f"{GROUPS}/{w.group}", json={"name": "Mine"}, headers=auth(w.dev_token)
            )
        ).status_code == 404


async def test_members_listing_and_removal_take_effect_at_once(
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
        w = await build_world(
            client, env, admin_token, sql, folder_chain, create_user, login, grant, grant_sql,
            create_group, add_member,
        )  # fmt: skip
        listed = await client.get(f"{GROUPS}/{w.group}/members", headers=auth(w.oa_token))
        assert listed.status_code == 200, listed.text
        item = listed.json()["items"][0]
        assert item["member_id"] == w.dev.principal_id
        assert item["username"] == "dev-w"
        assert item["display_name"] == "dev-w"
        assert item["added_by"] == w.oa.principal_id
        assert listed.json()["next_cursor"] is None

        gone = await client.delete(
            f"{GROUPS}/{w.group}/members/{w.dev.principal_id}", headers=auth(w.oa_token)
        )
        assert gone.status_code == 204, gone.text
        assert gone.content == b""
        access = await client.get(f"{FOLDERS}/{w.team}/effective-access", headers=auth(w.dev_token))
        assert access.status_code == 404  # lost team_editor, so cannot even read the team
        evs = events(sql, env, "group.member_removed")
        assert len(evs) == 1
        assert evs[0]["before"]["member_id"] == w.dev.principal_id
        assert evs[0]["after"] is None
        again = await client.delete(
            f"{GROUPS}/{w.group}/members/{w.dev.principal_id}", headers=auth(w.oa_token)
        )
        assert again.status_code == 404
        assert len(events(sql, env, "group.member_removed")) == 1
        bad = await client.delete(f"{GROUPS}/{w.group}/members/bad", headers=auth(w.oa_token))
        assert bad.status_code == 422

        # Several members page by keyset.
        names = []
        for i in range(3):
            user = await create_user(client, w.admin, f"extra-{i}")
            names.append(user.principal_id)
            assert (await add_member(client, w.oa_token, w.group, user.username)).status_code == 201
        page = await client.get(
            f"{GROUPS}/{w.group}/members", params={"limit": 2}, headers=auth(w.oa_token)
        )
        assert len(page.json()["items"]) == 2
        rest = await client.get(
            f"{GROUPS}/{w.group}/members",
            params={"limit": 2, "cursor": page.json()["next_cursor"]},
            headers=auth(w.oa_token),
        )
        got = [m["member_id"] for m in page.json()["items"] + rest.json()["items"]]
        assert got == sorted(names)
        assert rest.json()["next_cursor"] is None


async def test_deleting_a_group_removes_members_and_grants_with_one_event(
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
        w = await build_world(
            client, env, admin_token, sql, folder_chain, create_user, login, grant, grant_sql,
            create_group, add_member,
        )  # fmt: skip
        assert (
            await grant(client, w.oa_token, w.project, w.group, "project_editor", "local_group")
        ).status_code == 201
        grant_ids = {
            str(r["id"])
            for r in sql(
                env.db_url,
                "SELECT id FROM role_assignments WHERE principal_type = 'local_group' "
                "AND principal_id = %s",
                w.group,
            )
        }
        assert len(grant_ids) == 2

        deleted = await client.delete(f"{GROUPS}/{w.group}", headers=auth(w.oa_token))
        assert deleted.status_code == 204, deleted.text
        assert deleted.content == b""
        assert sql(env.db_url, "SELECT 1 FROM local_groups WHERE id = %s", w.group) == []
        assert sql(env.db_url, "SELECT 1 FROM group_members WHERE group_id = %s", w.group) == []
        assert (
            sql(
                env.db_url,
                "SELECT 1 FROM role_assignments WHERE principal_type = 'local_group' "
                "AND principal_id = %s",
                w.group,
            )
            == []
        )
        evs = events(sql, env, "group.deleted")
        assert len(evs) == 1
        before = evs[0]["before"]
        assert before["id"] == w.group
        assert before["name"] == "Ingest Devs"
        assert before["removed_grants"] == 2
        assert set(before["removed_grant_ids"]) == grant_ids
        assert before["removed_members"] == 1
        assert evs[0]["after"] is None
        # The former member loses access immediately; the group is gone for everyone.
        access = await client.get(f"{FOLDERS}/{w.team}/effective-access", headers=auth(w.dev_token))
        assert access.status_code == 404
        assert (await client.get(f"{GROUPS}/{w.group}", headers=auth(w.admin))).status_code == 404
        assert (
            await client.delete(f"{GROUPS}/{w.group}", headers=auth(w.oa_token))
        ).status_code == 404
        assert len(events(sql, env, "group.deleted")) == 1


async def test_the_deleted_grants_list_in_the_event_is_bounded(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_group: CreateGroup,
    grant: Grant,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        org, _, _ = await folder_chain(client, admin, root_id(sql, env), "b")
        group = await create_group(client, admin, org, "Many")
        for i in range(23):
            made = await client.post(
                FOLDERS,
                json={"parent_id": org, "slug": f"pp-{i}", "display_name": f"pp-{i}"},
                headers=auth(admin),
            )
            assert made.status_code == 201, made.text
        # Grants of a bound role at 23 projects.
        projects = sql(
            env.db_url, "SELECT id FROM folder WHERE kind = 'project' AND slug LIKE 'pp-%%'"
        )
        for row in projects:
            resp = await grant(
                client, admin, str(row["id"]), group, "project_editor", "local_group"
            )
            assert resp.status_code == 201, resp.text
        assert (await client.delete(f"{GROUPS}/{group}", headers=auth(admin))).status_code == 204
        before = events(sql, env, "group.deleted")[0]["before"]
        assert before["removed_grants"] == 23
        assert len(before["removed_grant_ids"]) == 20


async def test_grantable_groups_lists_root_and_own_organization_groups_only(
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
        w = await build_world(
            client, env, admin_token, sql, folder_chain, create_user, login, grant, grant_sql,
            create_group, add_member,
        )  # fmt: skip
        other = await create_group(client, w.admin, w.org2, "Elsewhere")
        rooted = await create_group(client, w.admin, w.root, "Everywhere")
        ta = await create_user(client, w.admin, "ta-w")
        grant_sql(env.db_url, ta.principal_id, "team_admin", w.team)
        ta_token = await login(client, ta.username, ta.password)

        url = f"{FOLDERS}/{w.team}/grantable-groups"
        for token in (w.oa_token, w.admin, ta_token):
            resp = await client.get(url, headers=auth(token))
            assert resp.status_code == 200, resp.text
            got = {g["id"]: g for g in resp.json()["items"]}
            assert set(got) == {w.group, rooted}
            assert set(got[w.group]) == {"id", "owner_folder_id", "name"}
            assert got[rooted]["owner_folder_id"] == w.root
            assert other not in got
        # Accepted exposure (A6): the team admin has no group:read at the Organization, so the
        # same group is 404 by id and absent from the group list, yet it is named above.
        assert (await client.get(f"{GROUPS}/{w.group}", headers=auth(ta_token))).status_code == 404
        listed = await client.get(GROUPS, headers=auth(ta_token))
        assert w.group not in {g["id"] for g in listed.json()["items"]}
        elsewhere = await client.get(f"{FOLDERS}/{w.org2}/grantable-groups", headers=auth(w.admin))
        assert {g["id"] for g in elsewhere.json()["items"]} == {other, rooted}
        at_root = await client.get(f"{FOLDERS}/{w.root}/grantable-groups", headers=auth(w.admin))
        assert {g["id"] for g in at_root.json()["items"]} == {rooted}
        # dev can read the team but cannot manage grants: 403. A stranger: 404.
        assert (await client.get(url, headers=auth(w.dev_token))).status_code == 403
        oa2 = await create_user(client, w.admin, "oa-v2")
        grant_sql(env.db_url, oa2.principal_id, "org_admin", w.org2)
        assert (
            await client.get(url, headers=auth(await login(client, oa2.username, oa2.password)))
        ).status_code == 404


# --- Plan 12: Entra users in local groups (D-80, D-33) ---------------------------------------


def entra_id(oid: str) -> str:
    return f"entra:{ENTRA_TENANT_ID}:{oid}"


async def register_entra_user(client: Any, env: Any, admin: str, name: str) -> str:
    """A Global admin registers an Entra user (Graph-confirmed by the fake); returns the oid."""
    oid = str(uuid.uuid4())
    env.directory.add_principal("user", oid, display_name=name)
    resp = await client.post(
        f"{IDENTITY}/entra/users", json={"object_id": oid}, headers=auth(admin)
    )
    assert resp.status_code == 201, resp.text
    return oid


async def test_an_entra_user_added_to_a_group_then_holds_the_group_s_role(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    create_group: CreateGroup,
    grant: Grant,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        listed = await client.get(FOLDERS, headers=auth(admin))
        root = str(next(f["id"] for f in listed.json()["items"] if f["kind"] == "root"))
        org, _project, team = await folder_chain(client, admin, root, "ent")
        alice = await register_entra_user(client, env, admin, "Alice")
        headers = auth(fake_issuer.mint_entra(alice, name="Alice"))
        group = await create_group(client, admin, org, "Entra people")
        assert (
            await grant(client, admin, team, group, "team_reader", "local_group")
        ).status_code == 201
        assert (await client.get(f"{FOLDERS}/{team}", headers=headers)).status_code == 404

        added = await client.post(
            f"{GROUPS}/{group}/members",
            json={"principal_id": entra_id(alice)},
            headers=auth(admin),
        )
        assert added.status_code == 201, added.text
        body = added.json()
        assert body["member_type"] == "user"
        assert body["member_id"] == entra_id(alice)
        assert body["display_name"] == "Alice"
        assert (await client.get(f"{FOLDERS}/{team}", headers=headers)).status_code == 200

        members = await client.get(f"{GROUPS}/{group}/members", headers=auth(admin))
        assert [(m["member_type"], m["member_id"]) for m in members.json()["items"]] == [
            ("user", entra_id(alice))
        ]
        removed = await client.delete(
            f"{GROUPS}/{group}/members/{entra_id(alice)}", headers=auth(admin)
        )
        assert removed.status_code == 204, removed.text
        assert (await client.get(f"{FOLDERS}/{team}", headers=headers)).status_code == 404


async def test_an_entra_id_without_an_active_record_is_unknown_principal(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    create_group: CreateGroup,
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        listed = await client.get(FOLDERS, headers=auth(admin))
        root = str(next(f["id"] for f in listed.json()["items"] if f["kind"] == "root"))
        org, _project, _team = await folder_chain(client, admin, root, "ent2")
        group = await create_group(client, admin, org, "G")
        inactive = await register_entra_user(client, env, admin, "Gone")
        sql(env.db_url, "UPDATE entra_users SET is_active = false WHERE object_id = %s", inactive)
        answers = []
        for principal_id in (entra_id(str(uuid.uuid4())), entra_id(inactive)):
            resp = await client.post(
                f"{GROUPS}/{group}/members",
                json={"principal_id": principal_id},
                headers=auth(admin),
            )
            assert resp.status_code == 422, resp.text
            assert slug_of(resp) == "unknown-principal"
            body = dict(resp.json())
            body.pop("request_id")
            answers.append(body)
        assert answers[0] == answers[1]
        rows = sql(env.db_url, "SELECT count(*) AS n FROM group_members")
        assert rows == [{"n": 0}]


async def test_entra_members_obey_the_ceiling_and_the_self_grant_guard(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    create_group: CreateGroup,
    grant: Grant,
    grant_sql: GrantSql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        listed = await client.get(FOLDERS, headers=auth(admin))
        root = str(next(f["id"] for f in listed.json()["items"] if f["kind"] == "root"))
        org_a, _pa, team_a = await folder_chain(client, admin, root, "ca")
        org_b, _pb, _tb = await folder_chain(client, admin, root, "cb")
        boss = await register_entra_user(client, env, admin, "Boss")
        alice = await register_entra_user(client, env, admin, "Alice")
        grant_sql(env.db_url, entra_id(boss), "org_admin", org_a)
        boss_headers = auth(fake_issuer.mint_entra(boss))

        group = await create_group(client, admin, org_a, "Straddler")
        assert (
            await grant(client, admin, team_a, group, "team_editor", "local_group")
        ).status_code == 201
        # The API never lets an A-owned group hold a role in B, so the state is built by SQL.
        grant_sql(env.db_url, group, "project_admin", org_b, "local_group")
        url = f"{GROUPS}/{group}/members"
        refused = await client.post(
            url, json={"principal_id": entra_id(alice)}, headers=boss_headers
        )
        assert refused.status_code == 403, refused.text
        assert slug_of(refused) == "grant-ceiling-exceeded"

        # Nobody adds themselves, also through an Entra identity.
        plain = await create_group(client, admin, org_a, "Plain")
        selfie = await client.post(
            f"{GROUPS}/{plain}/members", json={"principal_id": entra_id(boss)}, headers=boss_headers
        )
        assert selfie.status_code == 403, selfie.text
        assert slug_of(selfie) == "self-grant-forbidden"
        ok = await client.post(
            f"{GROUPS}/{plain}/members",
            json={"principal_id": entra_id(alice)},
            headers=boss_headers,
        )
        assert ok.status_code == 201, ok.text
