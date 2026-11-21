"""Folder guards: move and ROOT-delete triggers, foreign keys and deny semantics (D-08, D-35)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from dc_auth.testing import FakeIssuer

pytestmark = pytest.mark.integration

FOLDERS = "/api/v1/identity/folders"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _build(client: Any, token: str, sql: Sql, env: Any) -> tuple[uuid.UUID, dict[str, Any]]:
    root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
    made: list[dict[str, Any]] = []
    parent: Any = root
    for slug in ("acme", "payments", "ingest"):
        resp = await client.post(
            FOLDERS,
            json={"parent_id": str(parent), "slug": slug, "display_name": slug},
            headers=auth(token),
        )
        assert resp.status_code == 201, resp.text
        made.append(resp.json())
        parent = resp.json()["id"]
    return root, {"org": made[0], "project": made[1], "team": made[2]}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE folder SET parent_id = %(other)s WHERE id = %(id)s",
        "UPDATE folder SET slug = 'renamed' WHERE id = %(id)s",
        "UPDATE folder SET path = 'root.renamed' WHERE id = %(id)s",
        "UPDATE folder SET kind = 'project' WHERE id = %(id)s",
    ],
)
async def test_direct_sql_cannot_move_reslug_or_rekind_a_folder(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    statement: str,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root, c = await _build(client, token, sql, env)
    params = {"id": uuid.UUID(c["team"]["id"]), "other": root}
    url = env.db_url.replace("postgresql+psycopg://", "postgresql://")
    with (
        psycopg.connect(url, autocommit=True) as conn,
        pytest.raises(psycopg.errors.RaiseException) as caught,
    ):
        conn.execute(statement, params)  # type: ignore[arg-type]
    assert "folders cannot be" in str(caught.value)
    row = sql(
        env.db_url, "SELECT slug, path::text AS path, kind FROM folder WHERE id = %s", params["id"]
    )
    assert row == [{"slug": "ingest", "path": "root.acme.payments.ingest", "kind": "team"}]


async def test_direct_sql_cannot_delete_root_but_the_foreign_keys_guard_every_other_delete(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    grant_sql: Callable[..., None],
    sql: Sql,
) -> None:
    """Quick 261119-0v1: only ROOT is refused by the trigger; the FKs refuse a non-empty folder."""
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root, c = await _build(client, token, sql, env)
        user = await create_user(client, token, "granted")
        grant_sql(env.db_url, user.principal_id, "team_editor", c["team"]["id"])
        group = await client.post(
            "/api/v1/identity/groups",
            json={"owner_folder_id": c["org"]["id"], "name": "Owned"},
            headers=auth(token),
        )
        assert group.status_code == 201, group.text
    url = env.db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException) as caught:
            conn.execute("DELETE FROM folder WHERE id = %s", (root,))
        assert "the ROOT folder cannot be deleted" in str(caught.value)
        # A child, an owned group and a grant each keep their folder.
        for folder in (c["project"]["id"], c["org"]["id"], c["team"]["id"]):
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                conn.execute("DELETE FROM folder WHERE id = %s", (uuid.UUID(folder),))
    assert sql(env.db_url, "SELECT count(*) AS n FROM folder")[0]["n"] == 4


async def test_display_name_and_archive_updates_are_not_blocked_by_the_triggers(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        _, c = await _build(client, token, sql, env)
    rows = sql(
        env.db_url,
        "UPDATE folder SET display_name = 'Direct', archived_at = now() WHERE id = %s"
        " RETURNING display_name",
        uuid.UUID(c["team"]["id"]),
    )
    assert rows == [{"display_name": "Direct"}]


async def test_non_admin_gets_404_by_id_and_an_empty_page_from_lists(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root, c = await _build(client, token, sql, env)
        other = auth(fake_issuer.mint("mallory"))
        folder_id = c["org"]["id"]

        got = await client.get(f"{FOLDERS}/{folder_id}", headers=other)
        assert got.status_code == 404
        assert got.headers["content-type"].startswith("application/problem+json")
        assert got.json()["type"] == "urn:dc:problem:not-found"

        for url in (FOLDERS, f"{FOLDERS}?path=acme", f"{FOLDERS}?parent_id={root}"):
            listed = await client.get(url, headers=other)
            # D-35: path and parent_id are filters, so an invisible match is an empty page.
            assert listed.status_code == 200, (url, listed.text)
            assert listed.headers["content-type"].startswith("application/json")
            assert listed.json() == {"items": [], "next_cursor": None}

        created = await client.post(
            FOLDERS,
            json={"parent_id": str(root), "slug": "evil", "display_name": "Evil"},
            headers=other,
        )
        assert created.status_code == 404
        for method, url, body in (
            ("PATCH", f"{FOLDERS}/{folder_id}", {"display_name": "x"}),
            ("POST", f"{FOLDERS}/{folder_id}/archive", None),
            ("POST", f"{FOLDERS}/{folder_id}/unarchive", None),
        ):
            resp = await client.request(method, url, json=body, headers=other)
            assert resp.status_code == 404, (method, url, resp.text)

        anonymous = await client.get(FOLDERS)
        assert anonymous.status_code == 401
    assert sql(env.db_url, "SELECT count(*) AS n FROM folder WHERE slug = 'evil'")[0]["n"] == 0


async def test_there_is_no_move_route(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        _, c = await _build(client, token, sql, env)
        url = f"{FOLDERS}/{c['team']['id']}"
        put = await client.put(url, json={"parent_id": c["org"]["id"]}, headers=auth(token))
        assert put.status_code == 405
    assert sql(env.db_url, "SELECT count(*) AS n FROM folder")[0]["n"] == 4
