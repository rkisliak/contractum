"""Escalation guards of the grant routes (RBAC-05, RBAC-09): D-30, D-32, D-33, D-34 and the races.

The level-binding and ceiling matrices are driven from ``dc_auth.roles.ROLE_LEVELS`` and
``GRANTABLE``, so the tests follow the model instead of restating it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from dc_auth.roles import GRANTABLE, ROLE_LEVELS, Role
from dc_auth.testing import FakeIssuer

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
GRANTS = f"{IDENTITY}/grants"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
CreateUser = Callable[..., Awaitable[Any]]
Login = Callable[..., Awaitable[str]]
Grant = Callable[..., Awaitable[Any]]
GrantSql = Callable[..., None]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def slug_of(resp: Any) -> str:
    return str(resp.json()["type"]).rsplit(":", 1)[-1]


def neutral(resp: Any) -> dict[str, Any]:
    """A problem body without the per-request id, for byte-for-byte comparison."""
    body = dict(resp.json())
    body.pop("request_id", None)
    return body


@dataclass(frozen=True)
class Tree:
    """ROOT -> O -> (P -> T) and (P2 -> T2)."""

    root: str
    org: str
    project: str
    team: str
    project2: str
    team2: str

    def by_kind(self) -> dict[str, str]:
        return {
            "root": self.root,
            "organization": self.org,
            "project": self.project,
            "team": self.team,
        }


async def make_folder(client: Any, token: str, parent: str, slug: str) -> str:
    resp = await client.post(
        FOLDERS,
        json={"parent_id": parent, "slug": slug, "display_name": slug},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def build_tree(
    client: Any, token: str, env: Any, sql: Sql, folder_chain: FolderChain
) -> Tree:
    root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
    org, project, team = await folder_chain(client, token, root, "x")
    project2 = await make_folder(client, token, org, "x-p2")
    team2 = await make_folder(client, token, project2, "x-p2-t")
    return Tree(root, org, project, team, project2, team2)


def outbox(sql: Sql, env: Any, action: str) -> list[dict[str, Any]]:
    return sql(
        env.db_url, "SELECT id, before, after FROM outbox WHERE action = %s ORDER BY id", action
    )


def grant_rows(sql: Sql, env: Any, folder_id: str) -> list[dict[str, Any]]:
    return sql(
        env.db_url,
        "SELECT id, principal_id, role FROM role_assignments WHERE folder_id = %s ORDER BY id",
        folder_id,
    )


def direct_local_global_admins(sql: Sql, env: Any) -> int:
    rows = sql(
        env.db_url,
        "SELECT count(*) AS n FROM role_assignments ra "
        "JOIN folder f ON f.id = ra.folder_id AND f.kind = 'root' "
        "JOIN users u ON ra.principal_id = 'local:' || u.id::text "
        "WHERE ra.role = 'global_admin' AND ra.principal_type = 'user' AND u.is_active",
    )
    return int(rows[0]["n"])


async def test_level_binding_is_exhaustive_over_every_role_and_folder_kind(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        grantee = await create_user(client, admin, "bound-user")
        for role in Role:
            for kind, folder in tree.by_kind().items():
                resp = await grant(client, admin, folder, grantee.principal_id, role.value)
                if kind in ROLE_LEVELS[role]:
                    assert resp.status_code == 201, (role, kind, resp.text)
                else:
                    assert resp.status_code == 422, (role, kind, resp.text)
                    assert slug_of(resp) == "role-level-mismatch", (role, kind)
        # RBAC-09: Policy Manager and Consumer are grantable at every level.
        for role in (Role.POLICY_MANAGER, Role.CONSUMER):
            assert ROLE_LEVELS[role] == {"root", "organization", "project", "team"}
            held = sql(
                env.db_url,
                "SELECT count(*) AS n FROM role_assignments WHERE principal_id = %s AND role = %s",
                grantee.principal_id,
                role.value,
            )
            assert held[0]["n"] == 4
        # A mismatch writes nothing and records nothing.
        expected = sum(len(ROLE_LEVELS[r]) for r in Role)
        total = sql(
            env.db_url,
            "SELECT count(*) AS n FROM role_assignments WHERE principal_id = %s",
            grantee.principal_id,
        )
        assert total[0]["n"] == expected
        assert len(outbox(sql, env, "grant.created")) == expected


async def test_the_ceiling_follows_grantable_for_every_role_bound_in_the_subtree(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
    grant_sql: GrantSql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        kinds = {
            tree.org: "organization",
            tree.project: "project",
            tree.team: "team",
            tree.project2: "project",
            tree.team2: "team",
        }
        actors = (
            (
                "oa",
                Role.ORG_ADMIN,
                tree.org,
                [tree.org, tree.project, tree.team, tree.project2, tree.team2],
            ),
            ("pa", Role.PROJECT_ADMIN, tree.project, [tree.project, tree.team]),
            ("ta", Role.TEAM_ADMIN, tree.team, [tree.team]),
        )
        for sub, actor_role, at, subtree in actors:
            assert actor_role not in GRANTABLE[actor_role]  # nobody grants their own admin role
            grant_sql(env.db_url, f"fake:{sub}", actor_role.value, at)
            token = fake_issuer.mint(sub)
            grantee = await create_user(client, admin, f"grantee-{sub}")
            attempted = 0
            for folder in subtree:
                for role in Role:
                    if kinds[folder] not in ROLE_LEVELS[role]:
                        continue
                    attempted += 1
                    resp = await grant(client, token, folder, grantee.principal_id, role.value)
                    if role in GRANTABLE[actor_role]:
                        assert resp.status_code == 201, (sub, folder, role, resp.text)
                    else:
                        assert resp.status_code == 403, (sub, folder, role, resp.text)
                        assert slug_of(resp) == "grant-ceiling-exceeded", (sub, role)
            assert attempted > 0
            created = sum(
                1
                for folder in subtree
                for role in GRANTABLE[actor_role]
                if kinds[folder] in ROLE_LEVELS[role]
            )
            rows = sql(
                env.db_url,
                "SELECT count(*) AS n FROM role_assignments WHERE principal_id = %s",
                grantee.principal_id,
            )
            assert rows[0]["n"] == created

        # An Org admin cannot even reach ROOT: the gate answers 404, never a Global role.
        org_admin = fake_issuer.mint("oa")
        denied = await grant(
            client,
            org_admin,
            tree.root,
            (await create_user(client, admin, "g-root")).principal_id,
            "global_reader",
        )
        assert denied.status_code == 404


async def test_grants_outside_the_subtree_or_without_manage_are_refused(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
    grant_sql: GrantSql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        grantee = await create_user(client, admin, "outside-user")
        grant_sql(env.db_url, "fake:pa", "project_admin", tree.project)
        grant_sql(env.db_url, "fake:or", "org_reader", tree.org)
        # A sibling project's team is invisible to a Project admin: 404.
        resp = await grant(
            client, fake_issuer.mint("pa"), tree.team2, grantee.principal_id, "team_reader"
        )
        assert resp.status_code == 404
        # An Org reader can read the team but not manage its grants: 403.
        resp = await grant(
            client, fake_issuer.mint("or"), tree.team, grantee.principal_id, "team_reader"
        )
        assert resp.status_code == 403
        assert grant_rows(sql, env, tree.team) == []
        assert grant_rows(sql, env, tree.team2) == []


async def test_the_ceiling_is_evaluated_with_block_flags_applied(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
    grant_sql: GrantSql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        grantee = await create_user(client, admin, "blocked-user")
        grant_sql(env.db_url, "fake:oa", "org_admin", tree.org)
        grant_sql(env.db_url, "fake:ta", "team_admin", tree.team)
        org_admin, team_admin = fake_issuer.mint("oa"), fake_issuer.mint("ta")
        assert (
            await grant(client, org_admin, tree.team, grantee.principal_id, "team_reader")
        ).status_code == 201

        blocked = await client.put(
            f"{FOLDERS}/{tree.team}/block-inheritance", json={"enabled": True}, headers=auth(admin)
        )
        assert blocked.status_code == 200, blocked.text
        # The Org admin's inherited roles are cut at T: it can no longer grant there.
        resp = await grant(client, org_admin, tree.team, grantee.principal_id, "team_editor")
        assert resp.status_code == 404
        # A Team admin granted at T itself keeps working.
        resp = await grant(client, team_admin, tree.team, grantee.principal_id, "team_editor")
        assert resp.status_code == 201, resp.text


async def test_revoking_follows_the_same_ceiling_as_granting(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
    grant_sql: GrantSql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        holder = await create_user(client, admin, "holder-user")
        other = await create_user(client, admin, "holder-two")
        grant_sql(env.db_url, "fake:oa", "org_admin", tree.org)
        grant_sql(env.db_url, "fake:ta", "team_admin", tree.team)
        grant_sql(env.db_url, holder.principal_id, "team_admin", tree.team)
        grant_sql(env.db_url, other.principal_id, "team_editor", tree.team)
        by_role = {r["role"]: str(r["id"]) for r in grant_rows(sql, env, tree.team)}

        team_admin = fake_issuer.mint("ta")
        refused = await client.delete(f"{GRANTS}/{by_role['team_admin']}", headers=auth(team_admin))
        assert refused.status_code == 403
        assert slug_of(refused) == "grant-ceiling-exceeded"
        assert len(grant_rows(sql, env, tree.team)) == 3  # holder, other and fake:ta
        assert outbox(sql, env, "grant.revoked") == []

        org_admin = fake_issuer.mint("oa")
        ok = await client.delete(f"{GRANTS}/{by_role['team_editor']}", headers=auth(org_admin))
        assert ok.status_code == 204, ok.text
        assert len(outbox(sql, env, "grant.revoked")) == 1


async def test_nobody_grants_a_role_to_themselves(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant: Grant,
    grant_sql: GrantSql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        me = await create_user(client, admin, "self-granter")
        grant_sql(env.db_url, me.principal_id, "team_admin", tree.team)
        grant_sql(env.db_url, "fake:ta", "team_admin", tree.team)
        token = await login(client, me.username, me.password)

        resp = await grant(client, token, tree.team, me.principal_id, "team_editor")
        assert resp.status_code == 403, resp.text
        assert slug_of(resp) == "self-grant-forbidden"
        # The same for an actor whose own id is not a local user: the guard runs before the
        # grantee lookup.
        resp = await grant(client, fake_issuer.mint("ta"), tree.team, "fake:ta", "team_editor")
        assert resp.status_code == 403
        assert slug_of(resp) == "self-grant-forbidden"
        assert [r["role"] for r in grant_rows(sql, env, tree.team)] == ["team_admin", "team_admin"]
        # A different grantee is fine.
        friend = await create_user(client, admin, "self-friend")
        assert (
            await grant(client, token, tree.team, friend.principal_id, "team_editor")
        ).status_code == 201


async def test_the_last_direct_local_global_admin_cannot_be_revoked(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    create_user: CreateUser,
    login: Login,
    grant: Grant,
    grant_sql: GrantSql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        # A non-local Global admin does not count towards the minimum (D-34).
        grant_sql(env.db_url, "fake:root", "global_admin", root)
        bootstrap = next(
            r for r in grant_rows(sql, env, root) if str(r["principal_id"]).startswith("local:")
        )
        refused = await client.delete(f"{GRANTS}/{bootstrap['id']}", headers=auth(admin))
        assert refused.status_code == 409, refused.text
        assert slug_of(refused) == "last-global-admin"
        assert direct_local_global_admins(sql, env) == 1
        assert outbox(sql, env, "grant.revoked") == []

        # A1: the Global admin grants global_admin to a second local user.
        second = await create_user(client, admin, "second-admin")
        created = await grant(client, admin, root, second.principal_id, "global_admin")
        assert created.status_code == 201, created.text
        assert direct_local_global_admins(sql, env) == 2

        # Self-revocation is allowed (D-33 restricts granting only) once another remains.
        ok = await client.delete(f"{GRANTS}/{bootstrap['id']}", headers=auth(admin))
        assert ok.status_code == 204, ok.text
        assert direct_local_global_admins(sql, env) == 1
        # The survivor is now the last one.
        second_token = await login(client, second.username, second.password)
        again = await client.delete(f"{GRANTS}/{created.json()['id']}", headers=auth(second_token))
        assert again.status_code == 409
        assert slug_of(again) == "last-global-admin"


async def test_two_racing_revocations_of_the_last_two_global_admins_concurrency(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
    create_user: CreateUser,
    grant_sql: GrantSql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        second = await create_user(client, admin, "race-admin")
        grant_sql(env.db_url, second.principal_id, "global_admin", root)
        # A third Global admin principal issues both revocations.
        grant_sql(env.db_url, "fake:root", "global_admin", root)
        actor = fake_issuer.mint("root")
        ids = [
            str(r["id"])
            for r in grant_rows(sql, env, root)
            if str(r["principal_id"]).startswith("local:")
        ]
        assert len(ids) == 2
        assert direct_local_global_admins(sql, env) == 2

        responses = await asyncio.gather(
            *(client.delete(f"{GRANTS}/{gid}", headers=auth(actor)) for gid in ids)
        )
        assert sorted(r.status_code for r in responses) == [204, 409]
        loser = next(r for r in responses if r.status_code == 409)
        assert slug_of(loser) == "last-global-admin"
        assert direct_local_global_admins(sql, env) == 1
        assert len(outbox(sql, env, "grant.revoked")) == 1


async def test_an_identical_grant_twice_is_a_conflict_and_a_grant_applies_at_the_folder_adjacency(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant: Grant,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        user = await create_user(client, admin, "dup-user")
        token = await login(client, user.username, user.password)

        first = await grant(client, admin, tree.org, user.principal_id, "org_reader")
        assert first.status_code == 201, first.text
        second = await grant(client, admin, tree.org, user.principal_id, "org_reader")
        assert second.status_code == 409, second.text
        assert slug_of(second) == "conflict"
        assert len(grant_rows(sql, env, tree.org)) == 1
        assert len(outbox(sql, env, "grant.created")) == 1  # the 409 wrote nothing
        # Inclusive boundary: the grant applies at the granted folder itself and below it.
        for folder in (tree.org, tree.project, tree.team):
            assert (await client.get(f"{FOLDERS}/{folder}", headers=auth(token))).status_code == 200
        # A different role at the same folder is a different grant.
        other = await grant(client, admin, tree.org, user.principal_id, "consumer")
        assert other.status_code == 201


async def test_invalid_and_empty_bodies_are_422_and_unknown_users_look_alike(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        real = await create_user(client, admin, "real-user")
        gone = await create_user(client, admin, "gone-user")
        sql(env.db_url, "UPDATE users SET is_active = false WHERE id = %s", gone.id)
        url = f"{FOLDERS}/{tree.team}/grants"

        good = {"principal_type": "user", "principal_id": real.principal_id, "role": "team_reader"}
        invalid_bodies: list[dict[str, Any]] = [
            {},
            {k: v for k, v in good.items() if k != "role"},
            {k: v for k, v in good.items() if k != "principal_id"},
            {k: v for k, v in good.items() if k != "principal_type"},
            {**good, "role": None},
            {**good, "role": ""},
            {**good, "role": "superuser"},
            {**good, "principal_id": None},
            {**good, "principal_id": ""},
            {**good, "principal_id": "bad"},
            {**good, "principal_type": None},
            {**good, "principal_type": ""},
            # Rejected by the Literal here and by the bare-uuid shape once local groups exist.
            {**good, "principal_type": "local_group", "principal_id": f"local:{real.id}"},
            {**good, "folder_id": tree.team},
        ]
        for body in invalid_bodies:
            resp = await client.post(url, json=body, headers=auth(admin))
            assert resp.status_code == 422, (body, resp.text)
            assert slug_of(resp) == "validation-error", body
        assert grant_rows(sql, env, tree.team) == []

        # Well formed but unresolvable: one indistinguishable body each.
        unresolvable = [
            f"local:{uuid.uuid4()}",
            "local:not-a-uuid",
            "fake:bob",
            gone.principal_id,
            real.principal_id.upper().replace("LOCAL:", "local:"),
        ]
        bodies = []
        for principal_id in unresolvable:
            resp = await grant(client, admin, tree.team, principal_id, "team_reader")
            assert resp.status_code == 422, (principal_id, resp.text)
            assert slug_of(resp) == "unknown-principal", principal_id
            bodies.append(neutral(resp))
        assert all(body == bodies[0] for body in bodies)
        assert grant_rows(sql, env, tree.team) == []
        assert outbox(sql, env, "grant.created") == []

        # A folder without grants lists as an empty page.
        empty = await client.get(url, headers=auth(admin))
        assert empty.status_code == 200
        assert empty.json() == {"items": [], "next_cursor": None}


async def test_grants_list_only_their_folder_in_id_ordering_with_keyset_pages(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        user = await create_user(client, admin, "paged-user")
        created = []
        for role in ("team_reader", "team_editor", "consumer"):
            resp = await grant(client, admin, tree.team, user.principal_id, role)
            assert resp.status_code == 201, resp.text
            created.append(resp.json()["id"])
        # A grant on another folder never appears here.
        assert (
            await grant(client, admin, tree.org, user.principal_id, "org_reader")
        ).status_code == 201

        url = f"{FOLDERS}/{tree.team}/grants"
        page1 = await client.get(url, params={"limit": 2}, headers=auth(admin))
        assert page1.status_code == 200
        assert [g["id"] for g in page1.json()["items"]] == created[:2]
        cursor = page1.json()["next_cursor"]
        assert cursor is not None
        page2 = await client.get(url, params={"limit": 2, "cursor": cursor}, headers=auth(admin))
        assert [g["id"] for g in page2.json()["items"]] == created[2:]
        assert page2.json()["next_cursor"] is None
        assert created == sorted(created)
        everything = await client.get(url, headers=auth(admin))
        assert [g["id"] for g in everything.json()["items"]] == created
        bad = await client.get(url, params={"cursor": "not-a-cursor"}, headers=auth(admin))
        assert bad.status_code == 422


async def test_only_admins_see_grants_editors_get_403_and_strangers_404(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        grant_sql(env.db_url, "fake:te", "team_editor", tree.team)
        grant_sql(env.db_url, "fake:ta", "team_admin", tree.team)
        url = f"{FOLDERS}/{tree.team}/grants"
        assert (await client.get(url, headers=auth(fake_issuer.mint("te")))).status_code == 403
        assert (
            await client.get(url, headers=auth(fake_issuer.mint("stranger")))
        ).status_code == 404
        listed = await client.get(url, headers=auth(fake_issuer.mint("ta")))
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 2
        # The same split for a single grant by id.
        gid = listed.json()["items"][0]["id"]
        assert (
            await client.get(f"{GRANTS}/{gid}", headers=auth(fake_issuer.mint("te")))
        ).status_code == 403
        assert (
            await client.get(f"{GRANTS}/{gid}", headers=auth(fake_issuer.mint("stranger")))
        ).status_code == 404
        unknown = await client.get(f"{GRANTS}/{uuid.uuid4()}", headers=auth(admin))
        assert unknown.status_code == 404


async def test_revoking_twice_and_reposting_write_nothing_idempotency(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        user = await create_user(client, admin, "idem-user")
        created = await grant(client, admin, tree.team, user.principal_id, "consumer")
        assert created.status_code == 201
        assert (
            await grant(client, admin, tree.team, user.principal_id, "consumer")
        ).status_code == 409
        assert len(outbox(sql, env, "grant.created")) == 1

        gid = created.json()["id"]
        assert (await client.delete(f"{GRANTS}/{gid}", headers=auth(admin))).status_code == 204
        second = await client.delete(f"{GRANTS}/{gid}", headers=auth(admin))
        assert second.status_code == 404
        assert len(outbox(sql, env, "grant.revoked")) == 1
        assert grant_rows(sql, env, tree.team) == []


async def test_the_same_consumer_grant_twice_is_201_then_409_idempotency(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        user = await create_user(client, admin, "consumer-user")
        first = await grant(client, admin, tree.org, user.principal_id, "consumer")
        second = await grant(client, admin, tree.org, user.principal_id, "consumer")
        assert (first.status_code, second.status_code) == (201, 409)
        assert len(grant_rows(sql, env, tree.org)) == 1


async def test_two_concurrent_identical_consumer_grants_yield_one_201_concurrency(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        user = await create_user(client, admin, "racing-consumer")
        responses = await asyncio.gather(
            *(grant(client, admin, tree.project, user.principal_id, "consumer") for _ in range(2))
        )
        assert sorted(r.status_code for r in responses) == [201, 409]
        assert len(grant_rows(sql, env, tree.project)) == 1
        assert len(outbox(sql, env, "grant.created")) == 1


async def test_a_grant_in_an_archived_subtree_is_409_but_revocation_is_allowed(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        user = await create_user(client, admin, "archived-user")
        existing = await grant(client, admin, tree.team, user.principal_id, "team_reader")
        assert existing.status_code == 201
        archived = await client.post(f"{FOLDERS}/{tree.org}/archive", headers=auth(admin))
        assert archived.status_code == 200, archived.text

        resp = await grant(client, admin, tree.team, user.principal_id, "team_editor")
        assert resp.status_code == 409, resp.text
        assert slug_of(resp) == "folder-archived"
        assert [r["role"] for r in grant_rows(sql, env, tree.team)] == ["team_reader"]

        revoked = await client.delete(f"{GRANTS}/{existing.json()['id']}", headers=auth(admin))
        assert revoked.status_code == 204, revoked.text
        assert grant_rows(sql, env, tree.team) == []


async def test_a_block_write_racing_an_inherited_grant_is_serialised_concurrency(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    grant: Grant,
    grant_sql: GrantSql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        tree = await build_tree(client, admin, env, sql, folder_chain)
        user = await create_user(client, admin, "race-grantee")
        grant_sql(env.db_url, "fake:oa", "org_admin", tree.org)
        org_admin = fake_issuer.mint("oa")

        outcomes: list[str] = []
        for i in range(5):
            team = await make_folder(client, admin, tree.project, f"race-{i}")
            put, posted = await asyncio.gather(
                client.put(
                    f"{FOLDERS}/{team}/block-inheritance",
                    json={"enabled": True},
                    headers=auth(admin),
                ),
                grant(client, org_admin, team, user.principal_id, "team_reader"),
            )
            assert put.status_code == 200, put.text
            block_ids = [
                r["id"]
                for r in sql(
                    env.db_url,
                    "SELECT id FROM outbox WHERE action = 'folder.block_set' AND aggregate_id = %s",
                    team,
                )
            ]
            assert len(block_ids) == 1
            rows = grant_rows(sql, env, team)
            if posted.status_code == 201:
                grant_ids = [
                    r["id"]
                    for r in sql(
                        env.db_url,
                        "SELECT id FROM outbox WHERE action = 'grant.created' "
                        "AND after->>'folder_id' = %s",
                        team,
                    )
                ]
                assert len(grant_ids) == 1
                assert grant_ids[0] < block_ids[0]  # committed before the block
                assert len(rows) == 1
                outcomes.append("granted-first")
            else:
                # Refused after the block: at the gate (404) or by the in-transaction re-check
                # (403 grant-ceiling-exceeded). Never decided on pre-block roles and committed.
                assert posted.status_code in (404, 403), posted.text
                if posted.status_code == 403:
                    assert slug_of(posted) == "grant-ceiling-exceeded"
                assert rows == []
                outcomes.append("refused")
        assert len(outcomes) == 5
