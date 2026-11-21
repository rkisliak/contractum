"""Block-inheritance flag (D-29, RBAC-04): who may set it, what it cuts, audit, idempotency."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from dc_auth.roles import permissions_of
from dc_auth.testing import FakeIssuer

pytestmark = pytest.mark.integration

FOLDERS = "/api/v1/identity/folders"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
GrantSql = Callable[..., None]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def block_url(folder_id: str) -> str:
    return f"{FOLDERS}/{folder_id}/block-inheritance"


@dataclass(frozen=True)
class Tree:
    root: str
    org: str
    project: str
    t1: str
    t2: str


async def build_tree(
    client: Any, token: str, env: Any, sql: Sql, folder_chain: FolderChain, prefix: str = "a"
) -> Tree:
    root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
    org, project, t1 = await folder_chain(client, token, root, prefix)
    resp = await client.post(
        FOLDERS,
        json={"parent_id": project, "slug": f"{prefix}-t2", "display_name": "T2"},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return Tree(root, org, project, t1, resp.json()["id"])


def events(sql: Sql, env: Any, *actions: str) -> list[dict[str, Any]]:
    return sql(
        env.db_url,
        "SELECT id, action, before, after FROM outbox WHERE action = ANY(%s) ORDER BY id",
        list(actions),
    )


async def status_of(client: Any, token: str, folder_id: str) -> int:
    resp = await client.get(f"{FOLDERS}/{folder_id}", headers=auth(token))
    return int(resp.status_code)


async def test_a_block_cuts_a_root_reader_but_keeps_grants_at_or_below_the_block_adjacency(
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
        t = await build_tree(client, admin, env, sql, folder_chain)
        grant_sql(env.db_url, "fake:gr", "global_reader", t.root)
        grant_sql(env.db_url, "fake:or", "org_reader", t.org)
        grant_sql(env.db_url, "fake:tr", "team_reader", t.t1)
        gr = fake_issuer.mint("gr")
        org_reader = fake_issuer.mint("or")
        tr = fake_issuer.mint("tr")

        for token in (gr, org_reader, tr):
            assert await status_of(client, token, t.t1) == 200
        for token in (gr, org_reader):
            assert await status_of(client, token, t.org) == 200

        resp = await client.put(block_url(t.org), json={"enabled": True}, headers=auth(admin))
        assert resp.status_code == 200, resp.text
        assert resp.json()["block_inheritance"] is True
        rows = events(sql, env, "folder.block_set")
        assert len(rows) == 1
        before, after = rows[0]["before"], rows[0]["after"]
        changed = {k for k in after if before.get(k) != after.get(k)}
        assert changed == {"block_inheritance"}
        assert before["block_inheritance"] is False
        assert after["block_inheritance"] is True

        # D-29: a grant held strictly above the blocked org is cut on the very next request.
        assert await status_of(client, gr, t.org) == 404
        assert await status_of(client, gr, t.t1) == 404
        # A grant made at the blocked org keeps applying at it and below.
        for folder in (t.org, t.t1, t.t2):
            assert await status_of(client, org_reader, folder) == 200
        # A grant below the block keeps applying.
        assert await status_of(client, tr, t.t1) == 200
        # D-28: the global admin is never cut.
        assert await status_of(client, admin, t.t1) == 200

        cleared = await client.put(block_url(t.org), json={"enabled": False}, headers=auth(admin))
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["block_inheritance"] is False
        assert len(events(sql, env, "folder.block_cleared")) == 1
        assert await status_of(client, gr, t.org) == 200
        assert await status_of(client, gr, t.t1) == 200


async def test_who_may_set_the_flag_is_decided_on_the_parent(
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
        t = await build_tree(client, admin, env, sql, folder_chain)
        grant_sql(env.db_url, "fake:oa", "org_admin", t.org)
        grant_sql(env.db_url, "fake:pa", "project_admin", t.project)
        grant_sql(env.db_url, "fake:ta", "team_admin", t.t1)
        oa, pa, ta = (fake_issuer.mint(s) for s in ("oa", "pa", "ta"))
        stranger = fake_issuer.mint("nobody")
        on = {"enabled": True}

        # An Org admin cannot flag its own Organization (evaluated at ROOT) but can flag a team.
        forbidden = await client.put(block_url(t.org), json=on, headers=auth(oa))
        assert forbidden.status_code == 403
        assert forbidden.json()["type"] == "urn:dc:problem:forbidden"
        assert (await client.put(block_url(t.t1), json=on, headers=auth(oa))).status_code == 200
        assert (
            await client.put(block_url(t.t1), json={"enabled": False}, headers=auth(oa))
        ).status_code == 200
        # A Project admin flags a team in its project.
        assert (await client.put(block_url(t.t1), json=on, headers=auth(pa))).status_code == 200
        # A Team admin can read its team but may not flag it.
        assert (await client.put(block_url(t.t1), json=on, headers=auth(ta))).status_code == 403
        # A stranger cannot even see the folder.
        assert (
            await client.put(block_url(t.t1), json=on, headers=auth(stranger))
        ).status_code == 404
        # Only a Global admin can flag an Organization.
        assert (await client.put(block_url(t.org), json=on, headers=auth(admin))).status_code == 200


async def test_the_flag_is_only_for_organizations_and_teams_and_never_in_archived_subtrees(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        t = await build_tree(client, admin, env, sql, folder_chain)
        on = {"enabled": True}

        for folder in (t.project, t.root):
            resp = await client.put(block_url(folder), json=on, headers=auth(admin))
            assert resp.status_code == 422, (folder, resp.text)
            assert resp.json()["type"] == "urn:dc:problem:block-inheritance-not-allowed"

        for body in ({"enabled": None}, {}, {"enabled": True, "extra": 1}):
            resp = await client.put(block_url(t.t1), json=body, headers=auth(admin))
            assert resp.status_code == 422, (body, resp.text)
            assert resp.json()["type"] == "urn:dc:problem:validation-error"

        anonymous = await client.put(block_url(t.t1), json=on)
        assert anonymous.status_code == 401

        archived = await client.post(f"{FOLDERS}/{t.org}/archive", headers=auth(admin))
        assert archived.status_code == 200, archived.text
        resp = await client.put(block_url(t.t1), json=on, headers=auth(admin))
        assert resp.status_code == 409
        assert resp.json()["type"] == "urn:dc:problem:folder-archived"
    assert events(sql, env, "folder.block_set", "folder.block_cleared") == []


async def test_setting_the_current_value_is_a_200_no_op_idempotency(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        t = await build_tree(client, admin, env, sql, folder_chain)

        first = await client.put(block_url(t.t1), json={"enabled": True}, headers=auth(admin))
        again = await client.put(block_url(t.t1), json={"enabled": True}, headers=auth(admin))
        assert first.status_code == again.status_code == 200
        assert again.json() == first.json()
        assert len(events(sql, env, "folder.block_set")) == 1

        unset = await client.put(block_url(t.t2), json={"enabled": False}, headers=auth(admin))
        assert unset.status_code == 200
        assert unset.json()["block_inheritance"] is False
        assert events(sql, env, "folder.block_cleared") == []


async def test_effective_access_shows_the_callers_roles_and_where_inheritance_is_cut(
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
        t = await build_tree(client, admin, env, sql, folder_chain)
        grant_sql(env.db_url, "fake:mx", "global_reader", t.root)
        grant_sql(env.db_url, "fake:mx", "team_reader", t.t1)
        mx = fake_issuer.mint("mx")
        url = f"{FOLDERS}/{t.t1}/effective-access"

        blocked = await client.put(block_url(t.org), json={"enabled": True}, headers=auth(admin))
        assert blocked.status_code == 200
        resp = await client.get(url, headers=auth(mx))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["folder_id"] == t.t1
        assert body["roles"] == ["team_reader"]
        assert body["permissions"] == sorted(permissions_of(["team_reader"]))
        # The cut sits at the Organization, which this caller cannot read (the root grant is cut
        # there): the fact is reported, the folder id is not (D-35).
        assert body["inheritance_blocked"] is True
        assert body["inheritance_blocked_at"] is None
        assert await status_of(client, mx, t.org) == 404

        await client.put(block_url(t.org), json={"enabled": False}, headers=auth(admin))
        resp = await client.get(url, headers=auth(mx))
        body = resp.json()
        assert body["roles"] == ["global_reader", "team_reader"]
        assert body["permissions"] == sorted(permissions_of(["global_reader", "team_reader"]))
        assert body["inheritance_blocked"] is False
        assert body["inheritance_blocked_at"] is None

        # A caller who cannot read the folder gets the same 404 as for an unknown id.
        stranger = fake_issuer.mint("nobody")
        hidden = await client.get(url, headers=auth(stranger))
        assert hidden.status_code == 404
        unknown = await client.get(
            f"{FOLDERS}/{uuid.uuid4()}/effective-access", headers=auth(stranger)
        )
        assert unknown.status_code == 404

        top = await client.get(f"{FOLDERS}/{t.t1}/effective-access", headers=auth(admin))
        assert top.status_code == 200
        assert top.json()["roles"] == ["global_admin"]
        assert top.json()["inheritance_blocked_at"] is None
        assert top.json()["inheritance_blocked"] is False


async def test_block_writes_wait_on_the_authorization_write_lock_concurrency(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
) -> None:
    env = make_identity()
    url = env.db_url.replace("postgresql+psycopg://", "postgresql://")
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        t = await build_tree(client, admin, env, sql, folder_chain)

        async with await psycopg.AsyncConnection.connect(url) as holder:
            # The holder owns the lock before the PUT starts, so the PUT is the one that waits.
            await holder.execute(
                "SELECT pg_advisory_xact_lock(hashtext('dc-identity-authz-write'))"
            )
            committed = asyncio.Event()

            async def hold_until_the_put_waits() -> None:
                deadline = asyncio.get_running_loop().time() + 10
                while True:
                    cur = await holder.execute(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                        " AND database = (SELECT oid FROM pg_database"
                        " WHERE datname = current_database())"
                    )
                    row = await cur.fetchone()
                    assert row is not None
                    if row[0] >= 1:
                        break
                    assert asyncio.get_running_loop().time() < deadline, "PUT never waited"
                    await asyncio.sleep(0.02)
                flag = sql(env.db_url, "SELECT block_inheritance FROM folder WHERE id = %s", t.t1)
                assert flag == [{"block_inheritance": False}]
                assert events(sql, env, "folder.block_set") == []
                await holder.commit()
                committed.set()

            async def put() -> Any:
                resp = await client.put(
                    block_url(t.t1), json={"enabled": True}, headers=auth(admin)
                )
                # The PUT can only have returned after the holder committed.
                assert committed.is_set()
                return resp

            resp, _ = await asyncio.gather(put(), hold_until_the_put_waits())

        assert resp.status_code == 200, resp.text
        assert resp.json()["block_inheritance"] is True
        assert len(events(sql, env, "folder.block_set")) == 1


async def test_concurrent_puts_leave_the_later_write_and_one_event_per_change_concurrency(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        t = await build_tree(client, admin, env, sql, folder_chain)

        on, off = await asyncio.gather(
            client.put(block_url(t.t2), json={"enabled": True}, headers=auth(admin)),
            client.put(block_url(t.t2), json={"enabled": False}, headers=auth(admin)),
        )
        assert on.status_code == off.status_code == 200, (on.text, off.text)

        rows = sql(
            env.db_url,
            "SELECT action, after FROM outbox WHERE action = ANY(%s) AND after->>'id' = %s"
            " ORDER BY id",
            ["folder.block_set", "folder.block_cleared"],
            t.t2,
        )
        actions = [row["action"] for row in rows]
        assert actions in (
            ["folder.block_set"],
            ["folder.block_set", "folder.block_cleared"],
        ), actions
        final = sql(env.db_url, "SELECT block_inheritance FROM folder WHERE id = %s", t.t2)
        assert final[0]["block_inheritance"] is rows[-1]["after"]["block_inheritance"]


async def test_effective_access_names_the_blocking_folder_only_to_a_caller_who_can_read_it(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
) -> None:
    """IN-01 (D-35): the cut id is a folder id, so it follows the caller's read access to it."""
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        t = await build_tree(client, admin, env, sql, folder_chain)
        # oa holds the Organization admin role at the blocked Organization, pe only a grant below.
        grant_sql(env.db_url, "fake:oa", "org_admin", t.org)
        grant_sql(env.db_url, "fake:pe", "project_editor", t.project)
        oa = fake_issuer.mint("oa")
        pe = fake_issuer.mint("pe")
        assert (
            await client.put(block_url(t.org), json={"enabled": True}, headers=auth(admin))
        ).status_code == 200
        url = f"{FOLDERS}/{t.t1}/effective-access"

        readable = (await client.get(url, headers=auth(oa))).json()
        assert readable["inheritance_blocked"] is True
        assert readable["inheritance_blocked_at"] == t.org

        unreadable = await client.get(url, headers=auth(pe))
        assert unreadable.status_code == 200, unreadable.text
        assert await status_of(client, pe, t.org) == 404
        assert unreadable.json()["inheritance_blocked"] is True
        assert unreadable.json()["inheritance_blocked_at"] is None
        assert t.org not in unreadable.text


async def test_block_flag_response_is_minimal_for_a_caller_who_cannot_read_the_folder(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
) -> None:
    """IN-01 (D-35): the PUT answers ALLOW on the parent, but the body must not out-read GET."""
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        t = await build_tree(client, admin, env, sql, folder_chain)
        grant_sql(env.db_url, "fake:oa", "org_admin", t.org)
        oa = fake_issuer.mint("oa")

        # While T1 blocks, the Organization admin's inherited grant no longer reaches it.
        assert (
            await client.put(block_url(t.t1), json={"enabled": True}, headers=auth(admin))
        ).status_code == 200
        assert await status_of(client, oa, t.t1) == 404

        for enabled in (True, False):
            resp = await client.put(block_url(t.t1), json={"enabled": enabled}, headers=auth(oa))
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"id": t.t1, "block_inheritance": enabled}
            if enabled:
                assert await status_of(client, oa, t.t1) == 404

        # Once the flag is cleared the caller reads the folder again and gets the full body.
        assert await status_of(client, oa, t.t1) == 200
        full = await client.put(block_url(t.t1), json={"enabled": False}, headers=auth(oa))
        assert full.status_code == 200
        assert full.json()["slug"] == "a-t1"
        assert full.json()["block_inheritance"] is False
        # the audit event was still written for the real change
        assert len(events(sql, env, "folder.block_cleared")) == 1
