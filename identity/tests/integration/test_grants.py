"""Grant and revoke roles for local users (RBAC-03): the end-to-end tracer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

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


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def root_id(sql: Sql, env: Any) -> str:
    return str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])


async def test_a_global_admin_grants_team_reader_the_user_reads_the_team_revoke_removes_it(
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
        org, project, team = await folder_chain(client, admin, root_id(sql, env), "g")
        user = await create_user(client, admin, "grantee")
        token = await login(client, user.username, user.password)

        # Nothing until granted.
        assert (await client.get(f"{FOLDERS}/{team}", headers=auth(token))).status_code == 404

        created = await grant(client, admin, team, user.principal_id, "team_reader")
        assert created.status_code == 201, created.text
        body = created.json()
        assert created.headers["location"] == f"{GRANTS}/{body['id']}"
        assert body["folder_id"] == team
        assert body["principal_type"] == "user"
        assert body["principal_id"] == user.principal_id
        assert body["role"] == "team_reader"
        assert body["created_by"]

        # Effective on the very next request, at the granted folder itself (adjacency).
        read = await client.get(f"{FOLDERS}/{team}", headers=auth(token))
        assert read.status_code == 200, read.text
        access = await client.get(f"{FOLDERS}/{team}/effective-access", headers=auth(token))
        assert access.json()["roles"] == ["team_reader"]
        # Not above the grant.
        assert (await client.get(f"{FOLDERS}/{project}", headers=auth(token))).status_code == 404

        listed = await client.get(f"{FOLDERS}/{team}/grants", headers=auth(admin))
        assert listed.status_code == 200
        assert [g["id"] for g in listed.json()["items"]] == [body["id"]]
        assert listed.json()["next_cursor"] is None
        one = await client.get(f"{GRANTS}/{body['id']}", headers=auth(admin))
        assert one.status_code == 200
        assert one.json() == body

        events = sql(
            env.db_url,
            "SELECT action, aggregate_type, before, after, scope_path FROM outbox "
            "WHERE action LIKE 'grant.%%' ORDER BY id",
        )
        assert [e["action"] for e in events] == ["grant.created"]
        assert events[0]["aggregate_type"] == "grant"
        assert events[0]["before"] is None
        after = events[0]["after"]
        assert after == {
            "id": body["id"],
            "folder_id": team,
            "principal_type": "user",
            "principal_id": user.principal_id,
            "role": "team_reader",
        }
        assert [str(p) for p in events[0]["scope_path"]] == [root_id(sql, env), org, project, team]

        revoked = await client.delete(f"{GRANTS}/{body['id']}", headers=auth(admin))
        assert revoked.status_code == 204, revoked.text
        assert revoked.content == b""
        # Gone on the very next request (no cache).
        assert (await client.get(f"{FOLDERS}/{team}", headers=auth(token))).status_code == 404
        assert (await client.get(f"{GRANTS}/{body['id']}", headers=auth(admin))).status_code == 404

        events = sql(
            env.db_url,
            "SELECT action, before, after FROM outbox WHERE action LIKE 'grant.%%' ORDER BY id",
        )
        assert [e["action"] for e in events] == ["grant.created", "grant.revoked"]
        assert events[1]["before"] == after
        assert events[1]["after"] is None


async def test_a_team_editor_grant_lets_the_pdp_allow_contract_create_at_the_team(
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
        _, _, team = await folder_chain(client, admin, root_id(sql, env), "e")
        user = await create_user(client, admin, "editor1")
        token = await login(client, user.username, user.password)
        assert (
            await grant(client, admin, team, user.principal_id, "team_editor")
        ).status_code == 201

        access = await client.get(f"{FOLDERS}/{team}/effective-access", headers=auth(token))
        assert access.status_code == 200
        assert "contract:create" in access.json()["permissions"]
        assert "grant:manage" not in access.json()["permissions"]


async def test_grants_require_a_token_and_the_gate_answers_before_the_write(
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
        _, _, team = await folder_chain(client, admin, root_id(sql, env), "n")
        user = await create_user(client, admin, "nobody1")
        token = await login(client, user.username, user.password)
        body = {"principal_type": "user", "principal_id": user.principal_id, "role": "team_reader"}

        assert (await client.post(f"{FOLDERS}/{team}/grants", json=body)).status_code == 401
        assert (await client.get(f"{FOLDERS}/{team}/grants")).status_code == 401
        # A stranger gets 404 for the folder's grants and for a grant id alike.
        assert (
            await client.get(f"{FOLDERS}/{team}/grants", headers=auth(token))
        ).status_code == 404
        created = await grant(client, admin, team, user.principal_id, "team_reader")
        grant_id = created.json()["id"]
        # The grantee is a reader only: sees the folder, not the grants, may not manage.
        assert (
            await client.get(f"{FOLDERS}/{team}/grants", headers=auth(token))
        ).status_code == 403
        assert (await client.get(f"{GRANTS}/{grant_id}", headers=auth(token))).status_code == 403
        assert (await client.delete(f"{GRANTS}/{grant_id}", headers=auth(token))).status_code == 403
        denied = await grant(client, token, team, user.principal_id, "consumer")
        assert denied.status_code == 403
        assert (
            len(sql(env.db_url, "SELECT id FROM role_assignments WHERE folder_id = %s", team)) == 1
        )


async def test_an_org_admin_who_cannot_list_users_grants_to_a_local_user_by_username(
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
        org, _project, team = await folder_chain(client, admin, root_id(sql, env), "u")
        org_admin_user = await create_user(client, admin, "olga")
        assert (
            await grant(client, admin, org, org_admin_user.principal_id, "org_admin")
        ).status_code == 201
        olga = await login(client, org_admin_user.username, org_admin_user.password)
        bob = await create_user(client, admin, "bob")

        # The reason for the username form: an Org admin may not list users.
        listed = await client.get(f"{IDENTITY}/users", headers=auth(olga))
        assert listed.status_code == 403, listed.text

        created = await client.post(
            f"{FOLDERS}/{team}/grants",
            json={
                "principal_type": "user",
                "username": bob.username.upper(),
                "role": "team_editor",
            },
            headers=auth(olga),
        )
        assert created.status_code == 201, created.text
        assert created.json()["principal_id"] == bob.principal_id
        assert created.json()["principal_type"] == "user"

        unknown = await client.post(
            f"{FOLDERS}/{team}/grants",
            json={"principal_type": "user", "username": "nobody-here", "role": "team_reader"},
            headers=auth(olga),
        )
        assert unknown.status_code == 422, unknown.text
        assert unknown.json()["type"] == "urn:dc:problem:unknown-principal"

        for body in (
            {"principal_type": "local_group", "username": bob.username, "role": "team_reader"},
            {
                "principal_type": "user",
                "username": bob.username,
                "principal_id": bob.principal_id,
                "role": "team_reader",
            },
            {"principal_type": "user", "role": "team_reader"},
        ):
            refused = await client.post(f"{FOLDERS}/{team}/grants", json=body, headers=auth(olga))
            assert refused.status_code == 422, (body, refused.text)
            assert refused.json()["type"] == "urn:dc:problem:validation-error", body
