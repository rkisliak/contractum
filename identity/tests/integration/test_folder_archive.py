"""Folder lifecycle: rename, archive/unarchive subtree semantics, idempotency, concurrency."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

pytestmark = pytest.mark.integration

FOLDERS = "/api/v1/identity/folders"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def create(client: Any, token: str, parent: Any, slug: str) -> dict[str, Any]:
    resp = await client.post(
        FOLDERS,
        json={"parent_id": str(parent), "slug": slug, "display_name": slug.title()},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def chain(client: Any, token: str, sql: Sql, env: Any) -> dict[str, dict[str, Any]]:
    root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
    org = await create(client, token, root, "acme")
    project = await create(client, token, org["id"], "payments")
    team = await create(client, token, project["id"], "ingest")
    return {"root": {"id": str(root)}, "org": org, "project": project, "team": team}


def count_events(sql: Sql, env: Any, action: str) -> int:
    rows = sql(env.db_url, "SELECT count(*) AS n FROM outbox WHERE action = %s", action)
    return int(rows[0]["n"])


async def test_rename_changes_display_name_only_and_writes_one_event(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        url = f"{FOLDERS}/{c['project']['id']}"
        resp = await client.patch(url, json={"display_name": "Payments EU"}, headers=auth(token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["display_name"] == "Payments EU"
        assert body["slug"] == "payments"
        assert body["path"] == "acme/payments"
        assert count_events(sql, env, "folder.renamed") == 1
        event = sql(env.db_url, "SELECT before, after FROM outbox WHERE action = 'folder.renamed'")[
            0
        ]
        assert event["before"]["display_name"] == "Payments"
        assert event["after"]["display_name"] == "Payments EU"

        same = await client.patch(url, json={"display_name": "Payments EU"}, headers=auth(token))
        assert same.status_code == 200
        assert count_events(sql, env, "folder.renamed") == 1

        for payload in (
            {"slug": "other"},
            {"parent_id": c["org"]["id"]},
            {"display_name": None},
            {},
        ):
            bad = await client.patch(url, json=payload, headers=auth(token))
            assert bad.status_code == 422, (payload, bad.text)
            assert bad.headers["content-type"].startswith("application/problem+json")
        assert count_events(sql, env, "folder.renamed") == 1


async def test_archive_makes_the_subtree_read_only_but_readable_and_excluded_from_lists(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        archived = await client.post(f"{FOLDERS}/{c['project']['id']}/archive", headers=auth(token))
        assert archived.status_code == 200, archived.text
        assert archived.json()["archived"] is True
        assert archived.json()["archived_at"] is not None
        assert count_events(sql, env, "folder.archived") == 1

        # Creates under the archived project, and under its (effectively archived) team.
        for parent in (c["project"]["id"], c["team"]["id"]):
            blocked = await client.post(
                FOLDERS,
                json={"parent_id": parent, "slug": "late", "display_name": "Late"},
                headers=auth(token),
            )
            assert blocked.status_code == 409, blocked.text
            assert blocked.json()["type"] == "urn:dc:problem:folder-archived"
        renamed = await client.patch(
            f"{FOLDERS}/{c['team']['id']}", json={"display_name": "X"}, headers=auth(token)
        )
        assert renamed.status_code == 409
        assert renamed.json()["type"] == "urn:dc:problem:folder-archived"

        team = await client.get(f"{FOLDERS}/{c['team']['id']}", headers=auth(token))
        assert team.status_code == 200
        assert team.json()["archived"] is True  # effective: an ancestor is archived

        listed = await client.get(
            FOLDERS, params={"parent_id": c["org"]["id"]}, headers=auth(token)
        )
        assert listed.json()["items"] == []
        everything = await client.get(FOLDERS, headers=auth(token))
        assert {i["slug"] for i in everything.json()["items"]} == {"root", "acme"}
        with_archived = await client.get(
            FOLDERS,
            params={"parent_id": c["org"]["id"], "include_archived": "true"},
            headers=auth(token),
        )
        assert [i["id"] for i in with_archived.json()["items"]] == [c["project"]["id"]]
        by_path = await client.get(
            FOLDERS, params={"path": "acme/payments/ingest"}, headers=auth(token)
        )
        assert by_path.json()["items"] == []


async def test_descendants_of_an_archived_folder_cannot_be_archived_or_unarchived(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        assert (
            await client.post(f"{FOLDERS}/{c['project']['id']}/archive", headers=auth(token))
        ).status_code == 200
        for verb in ("archive", "unarchive"):
            resp = await client.post(f"{FOLDERS}/{c['team']['id']}/{verb}", headers=auth(token))
            assert resp.status_code == 409, resp.text
            assert resp.json()["type"] == "urn:dc:problem:folder-archived"

        back = await client.post(f"{FOLDERS}/{c['project']['id']}/unarchive", headers=auth(token))
        assert back.status_code == 200
        assert back.json()["archived"] is False
        assert back.json()["archived_at"] is None
        assert count_events(sql, env, "folder.unarchived") == 1
        # Writable again.
        assert (await create(client, token, c["project"]["id"], "later"))["kind"] == "team"


async def test_root_cannot_be_archived(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
        resp = await client.post(f"{FOLDERS}/{root}/archive", headers=auth(token))
    assert resp.status_code == 422, resp.text
    assert count_events(sql, env, "folder.archived") == 0


async def test_archive_stamps_the_admin_and_keeps_created_by_unchanged(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        resp = await client.post(f"{FOLDERS}/{c['org']['id']}/archive", headers=auth(token))
    body = resp.json()
    admin = sql(env.db_url, "SELECT id FROM users")[0]["id"]
    assert body["created_by"] == f"local:{admin}"
    assert body["updated_by"] == f"local:{admin}"
    assert body["created_at"] == c["org"]["created_at"]
    assert body["updated_at"] > body["created_at"]
    row = sql(env.db_url, "SELECT archived_by FROM folder WHERE id = %s", uuid.UUID(c["org"]["id"]))
    assert row[0]["archived_by"] == f"local:{admin}"
    event = sql(env.db_url, "SELECT before, after FROM outbox WHERE action = 'folder.archived'")[0]
    assert event["before"]["archived_at"] is None
    assert event["after"]["archived_at"] is not None


async def test_idempotency_duplicate_create_archive_twice_and_unarchive_active(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        payload = {"parent_id": c["org"]["id"], "slug": "payments", "display_name": "Again"}
        again = await client.post(FOLDERS, json=payload, headers=auth(token))
        assert again.status_code == 409, again.text
        assert again.json()["type"] == "urn:dc:problem:conflict"
        assert (
            sql(env.db_url, "SELECT count(*) AS n FROM folder WHERE slug = 'payments'")[0]["n"] == 1
        )
        assert count_events(sql, env, "folder.created") == 3

        url = f"{FOLDERS}/{c['project']['id']}"
        assert (await client.post(f"{url}/archive", headers=auth(token))).status_code == 200
        second = await client.post(f"{url}/archive", headers=auth(token))
        assert second.status_code == 200
        assert second.json()["archived"] is True
        assert count_events(sql, env, "folder.archived") == 1

        assert (await client.post(f"{url}/unarchive", headers=auth(token))).status_code == 200
        active = await client.post(f"{url}/unarchive", headers=auth(token))
        assert active.status_code == 200
        assert active.json()["archived"] is False
        assert count_events(sql, env, "folder.unarchived") == 1


async def test_concurrency_two_identical_creates_yield_one_201_one_409_and_one_row(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
        payload = {"parent_id": str(root), "slug": "race", "display_name": "Race"}
        results = await asyncio.gather(
            *(client.post(FOLDERS, json=payload, headers=auth(token)) for _ in range(2))
        )
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    assert sql(env.db_url, "SELECT count(*) AS n FROM folder WHERE slug = 'race'")[0]["n"] == 1
    assert count_events(sql, env, "folder.created") == 1
