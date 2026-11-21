"""Deleting a folder and the deletion check (quick 261119-0v1, D-1 to D-6 and the purge amendment).

Identity asks contracts and validation what a folder still holds through ``FolderUsagePort``; these
tests inject a fake that records the caller's bearer, so no other service is needed. The real
contracts and validation routes are proven in their own integration tests and in the gateway e2e.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dc_auth.testing import ENTRA_TENANT_ID
from dc_identity.folder_usage import RemoteFolderUsage, RemotePurge, unavailable

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
PATS = f"{IDENTITY}/pats"
AUTHZ_CHECK = "/internal/v1/authz/check"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class FakeUsage:
    """A usage port that answers a fixed state and records every call."""

    usage: RemoteFolderUsage = field(default_factory=RemoteFolderUsage)
    fail: Exception | None = None
    purge_result: RemotePurge = field(default_factory=RemotePurge)
    counted: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    purged: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    async def counts(self, folder_id: uuid.UUID, bearer: str) -> RemoteFolderUsage:
        self.counted.append((folder_id, bearer))
        if self.fail is not None:
            raise self.fail
        return self.usage

    async def purge(self, folder_id: uuid.UUID, bearer: str) -> RemotePurge:
        self.purged.append((folder_id, bearer))
        if self.fail is not None:
            raise self.fail
        return self.purge_result


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


def count(sql: Sql, env: Any, query: str, *params: object) -> int:
    return int(sql(env.db_url, query, *params)[0]["n"])


def events(sql: Sql, env: Any, action: str) -> list[dict[str, Any]]:
    return sql(env.db_url, "SELECT * FROM outbox WHERE action = %s ORDER BY id", action)


async def test_a_global_admin_deletes_an_empty_team_with_its_grants_and_one_event(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    grant_sql: Callable[..., None],
    sql: Sql,
) -> None:
    port = FakeUsage(
        usage=RemoteFolderUsage(deleted_contracts=2, unused_rule_sets=1),
        purge_result=RemotePurge(deleted_contracts=2, contract_versions=5, rule_sets=1),
    )
    env = make_identity(folder_usage=port)
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        team = c["team"]["id"]
        for name, role in (("t-editor", "team_editor"), ("t-admin", "team_admin")):
            user = await create_user(client, token, name)
            grant_sql(env.db_url, user.principal_id, role, team)
        assert (
            count(
                sql,
                env,
                "SELECT count(*) AS n FROM role_assignments WHERE folder_id = %s",
                uuid.UUID(team),
            )
            == 2
        )

        resp = await client.delete(f"{FOLDERS}/{team}", headers=auth(token))
        assert resp.status_code == 204, resp.text
        assert resp.content == b""

        # The folder and both grants are gone in the same transaction.
        assert (
            count(sql, env, "SELECT count(*) AS n FROM folder WHERE id = %s", uuid.UUID(team)) == 0
        )
        assert (
            count(
                sql,
                env,
                "SELECT count(*) AS n FROM role_assignments WHERE folder_id = %s",
                uuid.UUID(team),
            )
            == 0
        )
        # Exactly one folder.deleted event with the allow-listed snapshot and what went with it.
        rows = events(sql, env, "folder.deleted")
        assert len(rows) == 1
        row = rows[0]
        assert row["aggregate_type"] == "folder"
        assert row["aggregate_id"] == team
        assert [str(s) for s in row["scope_path"]] == [
            c["root"]["id"],
            c["org"]["id"],
            c["project"]["id"],
            team,
        ]
        before = row["before"]
        assert before["id"] == team
        assert before["kind"] == "team"
        assert before["slug_path"] == "acme/payments/ingest"
        assert [a["kind"] for a in before["ancestors"]] == ["root", "organization", "project"]
        assert len(before["removed_grants"]) == 2
        assert {g["role"] for g in before["removed_grants"]} == {"team_editor", "team_admin"}
        assert before["purged"]["deleted_contracts"] == 2
        assert before["purged"]["rule_sets"] == 1
        assert row["after"] is None

        # The caller's own bearer went to both calls: counts first, then the purge.
        assert port.counted == [(uuid.UUID(team), token)]
        assert port.purged == [(uuid.UUID(team), token)]

        # The folder no longer resolves for any permission check, and its slug is reusable.
        for permission in ("contract:create", "template:create", "ruleset:create"):
            check = await client.post(
                AUTHZ_CHECK,
                json={"permission": permission, "folder_id": team},
                headers=auth(token),
            )
            assert check.status_code == 200, check.text
            assert check.json()["outcome"] != "allow", permission
        assert (await client.get(f"{FOLDERS}/{team}", headers=auth(token))).status_code == 404
        again = await create(client, token, c["project"]["id"], "ingest")
        assert again["id"] != team


async def test_organization_and_project_folders_can_be_deleted_too(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity(folder_usage=FakeUsage())
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        for level in ("team", "project", "org"):
            resp = await client.delete(f"{FOLDERS}/{c[level]['id']}", headers=auth(token))
            assert resp.status_code == 204, (level, resp.text)
    assert count(sql, env, "SELECT count(*) AS n FROM folder") == 1
    assert len(events(sql, env, "folder.deleted")) == 3


async def test_a_folder_with_live_content_is_409_with_blockers_in_kind_order_and_nothing_changes(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    grant_sql: Callable[..., None],
    create_user: Callable[..., Awaitable[Any]],
    sql: Sql,
) -> None:
    port = FakeUsage(
        usage=RemoteFolderUsage(
            contracts=2, templates=1, subscriber_cards=1, rule_sets=3, deleted_contracts=1
        )
    )
    env = make_identity(folder_usage=port)
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        team = c["team"]["id"]
        user = await create_user(client, token, "keeper")
        grant_sql(env.db_url, user.principal_id, "team_editor", team)
        resp = await client.delete(f"{FOLDERS}/{team}", headers=auth(token))
        assert resp.status_code == 409, resp.text
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["type"] == "urn:dc:problem:folder-not-empty"
        assert body["blockers"] == [
            {"kind": "contracts", "count": 2},
            {"kind": "templates", "count": 1},
            {"kind": "rule_sets", "count": 3},
            {"kind": "subscriber_cards", "count": 1},
        ]
        # Nothing was purged or deleted: the folder, its grant and the audit trail are untouched.
        assert port.purged == []
        assert (
            count(sql, env, "SELECT count(*) AS n FROM folder WHERE id = %s", uuid.UUID(team)) == 1
        )
        assert (
            count(
                sql,
                env,
                "SELECT count(*) AS n FROM role_assignments WHERE folder_id = %s",
                uuid.UUID(team),
            )
            == 1
        )
    assert events(sql, env, "folder.deleted") == []


async def test_subfolders_block_even_when_archived(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity(folder_usage=FakeUsage())
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        project = c["project"]["id"]
        blocked = await client.delete(f"{FOLDERS}/{project}", headers=auth(token))
        assert blocked.status_code == 409
        assert blocked.json()["blockers"] == [{"kind": "subfolders", "count": 1}]
        archived = await client.post(f"{FOLDERS}/{c['team']['id']}/archive", headers=auth(token))
        assert archived.status_code == 200, archived.text
        still = await client.delete(f"{FOLDERS}/{project}", headers=auth(token))
        assert still.status_code == 409
        assert still.json()["blockers"] == [{"kind": "subfolders", "count": 1}]
    assert (
        count(sql, env, "SELECT count(*) AS n FROM folder WHERE id = %s", uuid.UUID(project)) == 1
    )


async def test_owned_groups_service_accounts_and_entra_registrations_block(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
) -> None:
    env = make_identity(folder_usage=FakeUsage())
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
        org = await create(client, token, root, "owner")
        url = f"{FOLDERS}/{org['id']}"

        group = await client.post(
            f"{IDENTITY}/groups",
            json={"owner_folder_id": org["id"], "name": "Owned"},
            headers=auth(token),
        )
        assert group.status_code == 201, group.text
        blocked = await client.delete(url, headers=auth(token))
        assert blocked.json()["blockers"] == [{"kind": "groups", "count": 1}]

        account = await client.post(
            f"{IDENTITY}/service-accounts",
            json={"owner_folder_id": org["id"], "name": "owned-bot"},
            headers=auth(token),
        )
        assert account.status_code == 201, account.text
        blocked = await client.delete(url, headers=auth(token))
        assert blocked.json()["blockers"] == [
            {"kind": "groups", "count": 1},
            {"kind": "service_accounts", "count": 1},
        ]

        oid = str(uuid.uuid4())
        env.directory.add_group(oid, "Owned Entra", tenant_id=ENTRA_TENANT_ID)
        registration = await client.post(
            f"{IDENTITY}/entra/groups",
            json={"owner_folder_id": org["id"], "object_id": oid},
            headers=auth(token),
        )
        assert registration.status_code == 201, registration.text
        blocked = await client.delete(url, headers=auth(token))
        assert blocked.json()["blockers"] == [
            {"kind": "groups", "count": 1},
            {"kind": "service_accounts", "count": 1},
            {"kind": "entra_registrations", "count": 1},
        ]
        assert (
            count(sql, env, "SELECT count(*) AS n FROM folder WHERE id = %s", uuid.UUID(org["id"]))
            == 1
        )


async def test_a_usage_failure_is_503_and_nothing_is_deleted_or_purged(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    port = FakeUsage(fail=unavailable())
    env = make_identity(folder_usage=port)
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        team = c["team"]["id"]
        resp = await client.delete(f"{FOLDERS}/{team}", headers=auth(token))
        assert resp.status_code == 503, resp.text
        assert resp.json()["type"] == "urn:dc:problem:service-unavailable"
        check = await client.get(f"{FOLDERS}/{team}/deletion-check", headers=auth(token))
        assert check.status_code == 503
    assert port.purged == []
    assert count(sql, env, "SELECT count(*) AS n FROM folder WHERE id = %s", uuid.UUID(team)) == 1


async def test_root_is_422_and_the_port_is_never_called(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    port = FakeUsage()
    env = make_identity(folder_usage=port)
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
        for call in (client.delete(f"{FOLDERS}/{root}", headers=auth(token)),):
            resp = await call
            assert resp.status_code == 422, resp.text
            assert resp.json()["type"] == "urn:dc:problem:validation-error"
        check = await client.get(f"{FOLDERS}/{root}/deletion-check", headers=auth(token))
        assert check.status_code == 422
    assert port.counted == [] and port.purged == []
    assert count(sql, env, "SELECT count(*) AS n FROM folder WHERE kind = 'root'") == 1


async def test_refusals_anonymous_team_admin_and_a_stranger(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    grant_sql: Callable[..., None],
    sql: Sql,
) -> None:
    port = FakeUsage()
    env = make_identity(folder_usage=port)
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        team = c["team"]["id"]
        url = f"{FOLDERS}/{team}"
        team_admin = await create_user(client, token, "own-team-admin")
        grant_sql(env.db_url, team_admin.principal_id, "team_admin", team)
        stranger = await create_user(client, token, "stranger")
        admin_jwt = await login(client, team_admin.username, team_admin.password)
        stranger_jwt = await login(client, stranger.username, stranger.password)

        assert (await client.delete(url)).status_code == 401
        assert (await client.get(f"{url}/deletion-check")).status_code == 401
        # A team admin reads the team but lacks folder:delete: 403 (not 404).
        for call in (
            client.delete(url, headers=auth(admin_jwt)),
            client.get(f"{url}/deletion-check", headers=auth(admin_jwt)),
        ):
            assert (await call).status_code == 403
        # No grant at all: the folder is invisible, the uniform 404.
        for call in (
            client.delete(url, headers=auth(stranger_jwt)),
            client.get(f"{url}/deletion-check", headers=auth(stranger_jwt)),
        ):
            assert (await call).status_code == 404
        # A read-only PAT of the Global admin is refused: delete is a write.
        pat = await client.post(
            PATS,
            json={
                "name": "ro",
                "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                "read_only": True,
            },
            headers=auth(token),
        )
        assert pat.status_code == 201, pat.text
        read_only = pat.json()["token"]
        refused = await client.delete(url, headers=auth(read_only))
        assert refused.status_code == 403, refused.text
    assert port.counted == [] and port.purged == []
    assert count(sql, env, "SELECT count(*) AS n FROM folder WHERE id = %s", uuid.UUID(team)) == 1


async def test_deletion_check_matches_the_delete_and_writes_nothing(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    port = FakeUsage()
    env = make_identity(folder_usage=port)
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        team = c["team"]["id"]
        outbox_before = count(sql, env, "SELECT count(*) AS n FROM outbox")

        empty = await client.get(f"{FOLDERS}/{team}/deletion-check", headers=auth(token))
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"folder_id": team, "deletable": True, "blockers": [], "purges": []}

        port.usage = RemoteFolderUsage(
            contracts=1,
            rule_sets=1,
            deleted_contracts=4,
            archived_subscriber_cards=2,
            unused_rule_sets=3,
        )
        project = c["project"]["id"]
        busy = await client.get(f"{FOLDERS}/{project}/deletion-check", headers=auth(token))
        assert busy.status_code == 200
        body = busy.json()
        assert body["deletable"] is False
        assert body["blockers"] == [
            {"kind": "subfolders", "count": 1},
            {"kind": "contracts", "count": 1},
            {"kind": "rule_sets", "count": 1},
        ]
        assert body["purges"] == [
            {"kind": "deleted_contracts", "count": 4},
            {"kind": "archived_subscriber_cards", "count": 2},
            {"kind": "unused_rule_sets", "count": 3},
        ]
        refused = await client.delete(f"{FOLDERS}/{project}", headers=auth(token))
        assert refused.status_code == 409
        assert refused.json()["blockers"] == body["blockers"]
        # The check and the refused delete wrote nothing at all.
        assert count(sql, env, "SELECT count(*) AS n FROM outbox") == outbox_before
        assert port.purged == []


async def test_an_archived_ancestor_blocks_but_an_archived_empty_folder_can_go(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity(folder_usage=FakeUsage())
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        c = await chain(client, token, sql, env)
        team, project = c["team"]["id"], c["project"]["id"]
        # The folder itself archived: deleting a retired folder is the cleanup path.
        assert (
            await client.post(f"{FOLDERS}/{team}/archive", headers=auth(token))
        ).status_code == 200
        ok = await client.get(f"{FOLDERS}/{team}/deletion-check", headers=auth(token))
        assert ok.json()["deletable"] is True
        # A folder above it archived: blocked, and DELETE says folder-archived.
        assert (
            await client.post(f"{FOLDERS}/{project}/archive", headers=auth(token))
        ).status_code == 200
        under = await client.get(f"{FOLDERS}/{team}/deletion-check", headers=auth(token))
        assert under.status_code == 200, under.text
        assert under.json()["blockers"] == [{"kind": "archived_ancestor", "count": 1}]
        refused = await client.delete(f"{FOLDERS}/{team}", headers=auth(token))
        assert refused.status_code == 409
        assert refused.json()["type"] == "urn:dc:problem:folder-archived"
        assert (
            await client.post(f"{FOLDERS}/{project}/unarchive", headers=auth(token))
        ).status_code == 200
        deleted = await client.delete(f"{FOLDERS}/{team}", headers=auth(token))
        assert deleted.status_code == 204, deleted.text
