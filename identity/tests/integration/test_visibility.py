"""Identity-side isolation by id (RBAC-08, D-35): another organization is not observable."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from dc_auth.testing import FakeIssuer

pytestmark = pytest.mark.integration

FOLDERS = "/api/v1/identity/folders"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
GrantSql = Callable[..., None]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def anonymised(body: bytes, probed_id: str) -> bytes:
    """The 404 body with the probed id and the per-request ``request_id`` neutralised.

    The problem body carries ``instance`` (the request path, which holds the id the client itself
    sent) and a fresh ``request_id`` per request; nothing else in the body may differ between
    "exists in another org" and "never existed".
    """
    parsed = json.loads(body.replace(probed_id.encode(), b"<id>"))
    parsed.pop("request_id", None)
    return json.dumps(parsed, sort_keys=True).encode()


async def two_orgs(
    client: Any, admin: str, env: Any, sql: Sql, folder_chain: FolderChain
) -> tuple[str, tuple[str, str, str], tuple[str, str, str]]:
    root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
    a = await folder_chain(client, admin, root, "org-a")
    b = await folder_chain(client, admin, root, "org-b")
    return root, a, b


async def test_org_a_principal_cannot_observe_org_b_by_id_or_by_filter(
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
        _, a, b = await two_orgs(client, admin, env, sql, folder_chain)
        grant_sql(env.db_url, "fake:te", "team_editor", a[2])
        grant_sql(env.db_url, "fake:oa", "org_admin", a[0])
        for sub in ("te", "oa"):
            token = fake_issuer.mint(sub)
            for suffix in ("", "/effective-access"):
                unknown = str(uuid.uuid4())
                baseline = await client.get(f"{FOLDERS}/{unknown}{suffix}", headers=auth(token))
                assert baseline.status_code == 404
                for folder_id in (b[0], b[1], b[2]):
                    resp = await client.get(f"{FOLDERS}/{folder_id}{suffix}", headers=auth(token))
                    assert resp.status_code == 404, (sub, folder_id, suffix)
                    assert resp.headers["content-type"] == baseline.headers["content-type"]
                    assert anonymised(resp.content, folder_id) == anonymised(
                        baseline.content, unknown
                    )
            # A write attempt is also a plain 404: nothing about B is confirmed or denied.
            for folder_id in (b[0], b[2]):
                put = await client.put(
                    f"{FOLDERS}/{folder_id}/block-inheritance",
                    json={"enabled": True},
                    headers=auth(token),
                )
                assert put.status_code == 404, (sub, folder_id)
            listed = await client.get(f"{FOLDERS}?path=org-b", headers=auth(token))
            assert listed.status_code == 200
            assert listed.json() == {"items": [], "next_cursor": None}
            everything = (await client.get(FOLDERS, headers=auth(token))).json()["items"]
            assert not {i["id"] for i in everything} & set(b)
    assert sql(env.db_url, "SELECT count(*) AS n FROM folder WHERE block_inheritance")[0]["n"] == 0


async def test_a_descendant_only_grant_does_not_make_ancestors_readable(
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
        _, a, _ = await two_orgs(client, admin, env, sql, folder_chain)
        grant_sql(env.db_url, "fake:te", "team_editor", a[2])
        te = auth(fake_issuer.mint("te"))

        listed = await client.get(FOLDERS, headers=te)
        assert [i["id"] for i in listed.json()["items"]] == [a[2]]
        # The team's own path discloses the ancestor slugs (A5, accepted) ...
        assert listed.json()["items"][0]["path"] == "org-a/org-a-p/org-a-t1"
        # ... but the ancestors themselves stay unreadable.
        for ancestor in (a[0], a[1]):
            assert (await client.get(f"{FOLDERS}/{ancestor}", headers=te)).status_code == 404
        assert (await client.get(f"{FOLDERS}/{a[2]}", headers=te)).status_code == 200


async def test_a_root_consumer_loses_a_blocked_org_and_gets_it_back_on_clear(
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
        root, a, b = await two_orgs(client, admin, env, sql, folder_chain)
        grant_sql(env.db_url, "fake:cs", "consumer", root)
        grant_sql(env.db_url, "fake:orb", "org_reader", b[0])
        consumer = auth(fake_issuer.mint("cs"))
        org_reader_b = auth(fake_issuer.mint("orb"))

        async def listed_ids(headers: dict[str, str]) -> set[str]:
            items = (await client.get(FOLDERS, headers=headers)).json()["items"]
            return {i["id"] for i in items}

        assert set(b) <= await listed_ids(consumer)

        blocked = await client.put(
            f"{FOLDERS}/{b[0]}/block-inheritance", json={"enabled": True}, headers=auth(admin)
        )
        assert blocked.status_code == 200, blocked.text
        seen = await listed_ids(consumer)
        assert not seen & set(b)
        assert set(a) <= seen
        assert (await client.get(f"{FOLDERS}/{b[2]}", headers=consumer)).status_code == 404
        # D-29: a grant made at the blocked folder keeps its folders in the list.
        assert set(b) <= await listed_ids(org_reader_b)

        cleared = await client.put(
            f"{FOLDERS}/{b[0]}/block-inheritance", json={"enabled": False}, headers=auth(admin)
        )
        assert cleared.status_code == 200
        assert set(b) <= await listed_ids(consumer)
        assert (await client.get(f"{FOLDERS}/{b[2]}", headers=consumer)).status_code == 200


async def test_admins_rename_inside_their_subtree_only_oq4(
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
        _, a, _ = await two_orgs(client, admin, env, sql, folder_chain)
        sibling = await client.post(
            FOLDERS,
            json={"parent_id": a[1], "slug": "org-a-t2", "display_name": "T2"},
            headers=auth(admin),
        )
        assert sibling.status_code == 201, sibling.text
        t2 = sibling.json()["id"]
        grant_sql(env.db_url, "fake:oa", "org_admin", a[0])
        grant_sql(env.db_url, "fake:ta", "team_admin", a[2])
        oa = auth(fake_issuer.mint("oa"))
        ta = auth(fake_issuer.mint("ta"))

        renamed = await client.patch(
            f"{FOLDERS}/{a[2]}", json={"display_name": "By Org"}, headers=oa
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["display_name"] == "By Org"
        own = await client.patch(f"{FOLDERS}/{a[2]}", json={"display_name": "By Team"}, headers=ta)
        assert own.status_code == 200, own.text
        assert own.json()["display_name"] == "By Team"
        other = await client.patch(f"{FOLDERS}/{t2}", json={"display_name": "Nope"}, headers=ta)
        assert other.status_code == 404
    assert sql(env.db_url, "SELECT display_name FROM folder WHERE id = %s", uuid.UUID(t2)) == [
        {"display_name": "T2"}
    ]
