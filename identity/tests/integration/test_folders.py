"""Folder tree: create Organization -> Project -> Team, read by id and by slug path (RBAC-01)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from dc_identity.folders import ltree_to_slug_path, slug_path_to_ltree

pytestmark = pytest.mark.integration

FOLDERS = "/api/v1/identity/folders"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def root_id(sql: Sql, env: Any) -> uuid.UUID:
    row = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]
    value: uuid.UUID = row["id"]
    return value


async def create(
    client: Any, token: str, parent: uuid.UUID | str, slug: str, name: str | None = None
) -> Any:
    return await client.post(
        FOLDERS,
        json={"parent_id": str(parent), "slug": slug, "display_name": name or slug.title()},
        headers=auth(token),
    )


async def test_admin_builds_the_chain_and_reads_it_back_by_id_and_by_slug_path(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = root_id(sql, env)

        org = await client.post(
            FOLDERS,
            json={"parent_id": str(root), "slug": "acme", "display_name": "ACME"},
            headers=auth(token),
        )
        assert org.status_code == 201, org.text
        org_body = org.json()
        assert org_body["kind"] == "organization"
        assert org.headers["location"] == f"/api/v1/identity/folders/{org_body['id']}"

        project = await create(client, token, org_body["id"], "payments", "Payments")
        assert project.status_code == 201, project.text
        assert project.json()["kind"] == "project"

        team = await create(client, token, project.json()["id"], "ingest", "Ingest")
        assert team.status_code == 201, team.text
        team_body = team.json()
        assert team_body["kind"] == "team"
        assert team_body["path"] == "acme/payments/ingest"

        by_id = await client.get(f"{FOLDERS}/{team_body['id']}", headers=auth(token))
        assert by_id.status_code == 200
        got = by_id.json()
        assert got == team_body
        admin_user = sql(env.db_url, "SELECT id FROM users")[0]["id"]
        assert got["created_by"] == f"local:{admin_user}"
        assert got["created_at"] == got["updated_at"]
        assert got["archived"] is False

        by_path = await client.get(f"{FOLDERS}?path=acme/payments/ingest", headers=auth(token))
        assert by_path.status_code == 200
        assert by_path.json() == {"items": [team_body], "next_cursor": None}

        events = sql(
            env.db_url,
            "SELECT aggregate_id, scope_path, actor FROM outbox WHERE action = 'folder.created'"
            " ORDER BY id",
        )
        assert len(events) == 3
        chain: list[uuid.UUID] = [root]
        for ev, body in zip(events, (org_body, project.json(), team_body), strict=True):
            chain = [*chain, uuid.UUID(body["id"])]
            assert ev["aggregate_id"] == body["id"]
            assert ev["scope_path"] == chain
            assert ev["actor"]["id"] == f"local:{admin_user}"


async def test_hyphen_slugs_are_stored_unchanged_and_resolve_by_path_and_ltree_subtree(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        org = (await create(client, token, root_id(sql, env), "acme", "ACME")).json()
        project = await create(client, token, org["id"], "payments-eu", "Payments EU")
        assert project.status_code == 201, project.text
        team = await create(client, token, project.json()["id"], "ingest-v2", "Ingest v2")
        assert team.status_code == 201, team.text
        assert team.json()["path"] == "acme/payments-eu/ingest-v2"

        rows = sql(
            env.db_url,
            "SELECT slug, path::text AS path FROM folder WHERE slug IN ('payments-eu', 'ingest-v2')"
            " ORDER BY nlevel(path)",
        )
        assert rows == [
            {"slug": "payments-eu", "path": "root.acme.payments-eu"},
            {"slug": "ingest-v2", "path": "root.acme.payments-eu.ingest-v2"},
        ]

        found = await client.get(f"{FOLDERS}?path=acme/payments-eu/ingest-v2", headers=auth(token))
        assert found.status_code == 200
        assert [item["id"] for item in found.json()["items"]] == [team.json()["id"]]

        subtree = sql(
            env.db_url,
            "SELECT count(*) AS n FROM folder WHERE path <@ CAST(%s AS ltree)",
            "root.acme.payments-eu",
        )
        assert subtree[0]["n"] == 2

    assert slug_path_to_ltree("acme/payments-eu") == "root.acme.payments-eu"
    assert ltree_to_slug_path("root.acme.payments-eu") == "acme/payments-eu"
    assert ltree_to_slug_path("root") == ""


@pytest.mark.parametrize("slug", ["-eu", "eu-", "pay--eu", "Pay-EU", "a" * 64, "pay_eu", ""])
async def test_invalid_slugs_are_a_422_before_any_database_write(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    slug: str,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        resp = await create(client, token, root_id(sql, env), slug)
    assert resp.status_code == 422, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert sql(env.db_url, "SELECT count(*) AS n FROM folder")[0]["n"] == 1


@pytest.mark.parametrize("path", ["", "acme//x", "/acme", "acme/-x", "acme/Pay", "acme/a b"])
async def test_invalid_slug_paths_are_a_422(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    path: str,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        resp = await client.get(FOLDERS, params={"path": path}, headers=auth(token))
    assert resp.status_code == 422, resp.text
    assert resp.json()["type"] == "urn:dc:problem:validation-error"


async def test_kind_follows_depth_and_a_team_cannot_have_children(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        org = (await create(client, token, root_id(sql, env), "acme")).json()
        project = (await create(client, token, org["id"], "payments")).json()
        team = (await create(client, token, project["id"], "ingest")).json()
        child = await create(client, token, team["id"], "nope")
        missing = await create(client, token, uuid.uuid4(), "ghost")
    assert child.status_code == 422, child.text
    assert child.json()["detail"] == "Team folders cannot contain folders"
    assert missing.status_code == 404
    assert sql(env.db_url, "SELECT count(*) AS n FROM folder")[0]["n"] == 4


async def test_list_is_keyset_paginated_without_a_total_and_rejects_a_bad_cursor(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = root_id(sql, env)
        for slug in ("one", "two", "three"):
            assert (await create(client, token, root, slug)).status_code == 201
        first = await client.get(
            FOLDERS, params={"parent_id": str(root), "limit": 2}, headers=auth(token)
        )
        body = first.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None
        assert "total" not in body
        second = await client.get(
            FOLDERS,
            params={"parent_id": str(root), "limit": 2, "cursor": body["next_cursor"]},
            headers=auth(token),
        )
        assert len(second.json()["items"]) == 1
        assert second.json()["next_cursor"] is None
        seen = [i["slug"] for i in body["items"] + second.json()["items"]]
        assert sorted(seen) == ["one", "three", "two"]
        bad = await client.get(FOLDERS, params={"cursor": "not-a-cursor"}, headers=auth(token))
    assert bad.status_code == 422


SPOOF = "Team\u202eevil"


async def test_name_rule_refuses_spoofing_characters_on_create_and_rename(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = root_id(sql, env)
        refused = await create(client, token, root, "spoof", SPOOF)
        assert refused.status_code == 422
        body = refused.json()
        assert body["type"] == "urn:dc:problem:validation-error"
        assert body["errors"][0]["pointer"] == "/body/display_name"
        assert "U+202E" in body["errors"][0]["detail"]
        assert "\u202e" not in refused.text
        emoji = await create(client, token, root, "emoji", "Ops \U0001f600")
        assert emoji.status_code == 422
        assert "U+1F600" in emoji.json()["errors"][0]["detail"]
        assert sql(env.db_url, "SELECT count(*) AS n FROM folder")[0]["n"] == 1

        made = await create(client, token, root, "ops", "  Ops  ")
        assert made.status_code == 201, made.text
        assert made.json()["display_name"] == "Ops"
        url = f"{FOLDERS}/{made.json()['id']}"
        renamed = await client.patch(url, json={"display_name": SPOOF}, headers=auth(token))
        assert renamed.status_code == 422
        assert renamed.json()["errors"][0]["pointer"] == "/body/display_name"
        assert "U+202E" in renamed.json()["errors"][0]["detail"]
        assert "\u202e" not in renamed.text
        ok = await client.patch(url, json={"display_name": "数据 Team"}, headers=auth(token))
        assert ok.status_code == 200
        assert ok.json()["display_name"] == "数据 Team"


async def test_a_stored_legacy_name_is_still_readable(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = root_id(sql, env)
        made = await create(client, token, root, "legacy", "Legacy")
        assert made.status_code == 201
        sql(
            env.db_url,
            "UPDATE folder SET display_name = %s WHERE id = %s",
            SPOOF,
            uuid.UUID(made.json()["id"]),
        )
        got = await client.get(f"{FOLDERS}/{made.json()['id']}", headers=auth(token))
    assert got.status_code == 200
    assert got.json()["display_name"] == SPOOF
