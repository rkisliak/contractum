"""Local service accounts for pipelines (AUTH-07, D-69, D-70, D-71, D-72, D-78, D-79).

A service account is owned like a local group (ROOT or an Organization), has the principal id
``sa:<uuid>``, holds at most two active keys with a mandatory expiry, and authenticates with a key
directly as a bearer token. Creating or renaming one and minting or revoking its keys need an
interactive sign-in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from dc_auth.authorizer import Outcome
from dc_auth.opaque import RemoteOpaqueResolver
from dc_auth.remote import RemoteAuthorizer
from pydantic import SecretStr

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
SERVICE_ACCOUNTS = f"{IDENTITY}/service-accounts"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
CreateUser = Callable[..., Awaitable[Any]]
Login = Callable[..., Awaitable[str]]
GrantSql = Callable[..., None]
CreatePat = Callable[..., Awaitable[dict[str, Any]]]
CreateServiceAccount = Callable[..., Awaitable[dict[str, Any]]]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def slug_of(resp: Any) -> str:
    return str(resp.json()["type"]).rsplit(":", 1)[-1]


def events(sql: Sql, env: Any, like: str) -> list[dict[str, Any]]:
    return sql(
        env.db_url,
        "SELECT action, aggregate_type, aggregate_id, actor, before, after, scope_path "
        "FROM outbox WHERE action LIKE %s ORDER BY id",
        like,
    )


@dataclass
class World:
    env: Any
    client: Any
    admin: str
    root: str
    org_a: str
    project_a: str
    team_a: str
    org_b: str
    oa: str  # Organization admin of A (token)
    ob: str  # Organization admin of B (token)
    sql: Sql


@pytest.fixture
def open_world(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant_sql: GrantSql,
) -> Callable[..., Any]:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _open(**overrides: object) -> Any:
        env = make_identity(**overrides)
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
            org_a, project_a, team_a = await folder_chain(client, admin, root, "sa-a")
            org_b, _, _ = await folder_chain(client, admin, root, "sa-b")
            tokens: dict[str, str] = {}
            for name, org in (("oa", org_a), ("ob", org_b)):
                user = await create_user(client, admin, f"{name}-admin")
                grant_sql(env.db_url, user.principal_id, "org_admin", org)
                tokens[name] = await login(client, user.username, user.password)
            yield World(
                env=env,
                client=client,
                admin=admin,
                root=root,
                org_a=org_a,
                project_a=project_a,
                team_a=team_a,
                org_b=org_b,
                oa=tokens["oa"],
                ob=tokens["ob"],
                sql=sql,
            )

    return _open


async def test_a_global_admin_creates_a_service_account_with_an_sa_principal_id(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        resp = await w.client.post(
            SERVICE_ACCOUNTS,
            json={"owner_folder_id": w.root, "name": "ingest-bot"},
            headers=auth(w.admin),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["principal_id"] == f"sa:{body['id']}"
        assert body["owner_folder_id"] == w.root
        assert body["name"] == "ingest-bot"
        assert body["is_active"] is True
        assert resp.headers["location"] == f"{SERVICE_ACCOUNTS}/{body['id']}"
        created = events(w.sql, w.env, "service_account.created")
        assert len(created) == 1
        assert created[0]["aggregate_id"] == body["id"]
        assert created[0]["after"]["name"] == "ingest-bot"
        listed = await w.client.get(SERVICE_ACCOUNTS, headers=auth(w.admin))
        assert [i["id"] for i in listed.json()["items"]] == [body["id"]]


async def test_an_org_admin_creates_only_inside_its_own_organization_and_never_on_a_project(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        own = await w.client.post(
            SERVICE_ACCOUNTS,
            json={"owner_folder_id": w.org_a, "name": "a-bot"},
            headers=auth(w.oa),
        )
        assert own.status_code == 201, own.text
        other = await w.client.post(
            SERVICE_ACCOUNTS,
            json={"owner_folder_id": w.org_b, "name": "a-bot"},
            headers=auth(w.oa),
        )
        assert other.status_code == 404, other.text
        on_project = await w.client.post(
            SERVICE_ACCOUNTS,
            json={"owner_folder_id": w.project_a, "name": "p-bot"},
            headers=auth(w.oa),
        )
        assert on_project.status_code == 422, on_project.text
        assert slug_of(on_project) == "owner-not-allowed"
        on_team = await w.client.post(
            SERVICE_ACCOUNTS,
            json={"owner_folder_id": w.team_a, "name": "t-bot"},
            headers=auth(w.admin),
        )
        assert on_team.status_code == 422
        assert slug_of(on_team) == "owner-not-allowed"
        assert len(events(w.sql, w.env, "service_account.created")) == 1


async def test_a_name_is_unique_per_owner_case_insensitively(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world() as w:
        await create_service_account(w.client, w.oa, w.org_a, "ingest-bot")
        dup = await w.client.post(
            SERVICE_ACCOUNTS,
            json={"owner_folder_id": w.org_a, "name": "INGEST-BOT"},
            headers=auth(w.oa),
        )
        assert dup.status_code == 409, dup.text
        assert slug_of(dup) == "conflict"
        elsewhere = await w.client.post(
            SERVICE_ACCOUNTS,
            json={"owner_folder_id": w.org_b, "name": "INGEST-BOT"},
            headers=auth(w.ob),
        )
        assert elsewhere.status_code == 201, elsewhere.text
        assert len(events(w.sql, w.env, "service_account.created")) == 2


async def test_renaming_to_the_same_name_is_a_no_op_and_to_a_taken_name_is_409(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world() as w:
        bot = await create_service_account(w.client, w.oa, w.org_a, "ingest-bot")
        await create_service_account(w.client, w.oa, w.org_a, "export-bot")
        same = await w.client.patch(
            f"{SERVICE_ACCOUNTS}/{bot['id']}", json={"name": "ingest-bot"}, headers=auth(w.oa)
        )
        taken = await w.client.patch(
            f"{SERVICE_ACCOUNTS}/{bot['id']}", json={"name": "EXPORT-BOT"}, headers=auth(w.oa)
        )
        stored = w.sql(w.env.db_url, "SELECT name FROM service_accounts WHERE id = %s", bot["id"])
    assert same.status_code == 200, same.text
    assert same.json()["name"] == "ingest-bot"
    assert taken.status_code == 409, taken.text
    assert slug_of(taken) == "conflict"
    assert stored == [{"name": "ingest-bot"}]
    assert events(w.sql, w.env, "service_account.renamed") == []


async def test_the_listing_filters_on_an_owner_folder_and_walks_by_cursor(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world() as w:
        in_a = [
            (await create_service_account(w.client, w.admin, w.org_a, name))["id"]
            for name in ("a-one", "a-two", "a-three")
        ]
        await create_service_account(w.client, w.admin, w.org_b, "b-one")
        walked: list[str] = []
        params = {"owner_folder_id": w.org_a, "limit": "2"}
        for _ in range(4):
            page = await w.client.get(SERVICE_ACCOUNTS, params=params, headers=auth(w.admin))
            assert page.status_code == 200, page.text
            walked.extend(item["id"] for item in page.json()["items"])
            if page.json()["next_cursor"] is None:
                break
            params = {**params, "cursor": page.json()["next_cursor"]}
    assert walked == in_a


async def test_listing_reading_and_renaming_follow_the_owner_s_visibility(
    open_world: Callable[..., Any],
    create_service_account: CreateServiceAccount,
    create_user: CreateUser,
    login: Login,
    grant_sql: GrantSql,
) -> None:
    async with open_world() as w:
        sa_a = await create_service_account(w.client, w.oa, w.org_a, "a-bot")
        sa_b = await create_service_account(w.client, w.ob, w.org_b, "b-bot")

        seen_by_oa = await w.client.get(SERVICE_ACCOUNTS, headers=auth(w.oa))
        assert [i["id"] for i in seen_by_oa.json()["items"]] == [sa_a["id"]]
        seen_by_admin = await w.client.get(SERVICE_ACCOUNTS, headers=auth(w.admin))
        assert {i["id"] for i in seen_by_admin.json()["items"]} == {sa_a["id"], sa_b["id"]}

        nobody = await create_user(w.client, w.admin, "nobody")
        nobody_token = await login(w.client, nobody.username, nobody.password)
        empty = await w.client.get(SERVICE_ACCOUNTS, headers=auth(nobody_token))
        assert empty.status_code == 200
        assert empty.json() == {"items": [], "next_cursor": None}

        own = await w.client.get(f"{SERVICE_ACCOUNTS}/{sa_a['id']}", headers=auth(w.oa))
        assert own.status_code == 200 and own.json()["name"] == "a-bot"
        foreign = await w.client.get(f"{SERVICE_ACCOUNTS}/{sa_b['id']}", headers=auth(w.oa))
        assert foreign.status_code == 404
        unknown = await w.client.get(
            f"{SERVICE_ACCOUNTS}/00000000-0000-4000-8000-000000000000", headers=auth(w.oa)
        )
        assert unknown.status_code == 404
        assert foreign.json()["detail"] == unknown.json()["detail"]

        renamed = await w.client.patch(
            f"{SERVICE_ACCOUNTS}/{sa_a['id']}", json={"name": "a-robot"}, headers=auth(w.oa)
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "a-robot"
        rename_events = events(w.sql, w.env, "service_account.renamed")
        assert len(rename_events) == 1
        assert rename_events[0]["before"]["name"] == "a-bot"
        assert rename_events[0]["after"]["name"] == "a-robot"

        cross = await w.client.patch(
            f"{SERVICE_ACCOUNTS}/{sa_b['id']}", json={"name": "x"}, headers=auth(w.oa)
        )
        assert cross.status_code == 404

        # A Team editor can neither see nor rename the account.
        editor = await create_user(w.client, w.admin, "editor")
        grant_sql(w.env.db_url, editor.principal_id, "team_editor", w.team_a)
        editor_token = await login(w.client, editor.username, editor.password)
        denied = await w.client.patch(
            f"{SERVICE_ACCOUNTS}/{sa_a['id']}", json={"name": "z"}, headers=auth(editor_token)
        )
        assert denied.status_code in (403, 404)
        assert (
            w.sql(w.env.db_url, "SELECT name FROM service_accounts WHERE id = %s", sa_a["id"])[0][
                "name"
            ]
            == "a-robot"
        )


async def test_every_service_account_route_needs_a_token(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.admin, w.root, "bot")
        assert (await w.client.get(SERVICE_ACCOUNTS)).status_code == 401
        assert (await w.client.get(f"{SERVICE_ACCOUNTS}/{sa['id']}")).status_code == 401
        post = await w.client.post(SERVICE_ACCOUNTS, json={"owner_folder_id": w.root, "name": "x"})
        assert post.status_code == 401
        patch = await w.client.patch(f"{SERVICE_ACCOUNTS}/{sa['id']}", json={"name": "x"})
        assert patch.status_code == 401


async def test_a_pat_reads_but_can_neither_create_nor_rename_a_service_account(
    open_world: Callable[..., Any],
    create_service_account: CreateServiceAccount,
    create_pat: CreatePat,
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.admin, w.root, "bot")
        pat = (await create_pat(w.client, w.admin))["token"]
        created = await w.client.post(
            SERVICE_ACCOUNTS,
            json={"owner_folder_id": w.root, "name": "from-pat"},
            headers=auth(pat),
        )
        assert created.status_code == 403, created.text
        assert slug_of(created) == "interactive-session-required"
        renamed = await w.client.patch(
            f"{SERVICE_ACCOUNTS}/{sa['id']}", json={"name": "renamed"}, headers=auth(pat)
        )
        assert renamed.status_code == 403, renamed.text
        assert slug_of(renamed) == "interactive-session-required"
        listed = await w.client.get(SERVICE_ACCOUNTS, headers=auth(pat))
        assert listed.status_code == 200
        assert [i["id"] for i in listed.json()["items"]] == [sa["id"]]
        read = await w.client.get(f"{SERVICE_ACCOUNTS}/{sa['id']}", headers=auth(pat))
        assert read.status_code == 200
        assert len(events(w.sql, w.env, "service_account.%")) == 1


# --- Task 2: two rotating keys, authentication on every service ---------------------------------


def _expiry(days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


async def _key(w: World, sa_id: str, name: str, days: float = 30, token: str | None = None) -> Any:
    return await w.client.post(
        f"{SERVICE_ACCOUNTS}/{sa_id}/keys",
        json={"name": name, "expires_at": _expiry(days)},
        headers=auth(token or w.admin),
    )


def _live_keys(w: World, sa_id: str) -> int:
    rows = w.sql(
        w.env.db_url,
        "SELECT count(*) AS n FROM api_credentials WHERE service_account_id = %s "
        "AND revoked_at IS NULL AND expires_at > now()",
        sa_id,
    )
    return int(rows[0]["n"])


async def test_a_key_is_shown_once_and_never_in_the_audit_event(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.oa, w.org_a, "bot")
        resp = await _key(w, sa["id"], "k1", token=w.oa)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["token"].startswith("dcp_")
        assert set(body) == {"id", "name", "expires_at", "created_at", "token"}
        created = events(w.sql, w.env, "credential.created")
        assert len(created) == 1
        dump = str(created[0])
        secret = body["token"].split("_", 2)[2]
        public_id = body["token"].split("_", 2)[1]
        for needle in (body["token"], secret, public_id):
            assert needle not in dump
        assert "secret_hash" not in dump
        assert created[0]["after"]["service_account_id"] == sa["id"]
        assert created[0]["after"]["kind"] == "service_account_key"
        assert [str(i) for i in created[0]["scope_path"]] == [w.root, w.org_a]


async def test_a_third_active_key_is_refused_and_a_revoked_or_expired_key_frees_a_slot(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.admin, w.root, "bot")
        first = await _key(w, sa["id"], "k1")
        second = await _key(w, sa["id"], "k2")
        assert first.status_code == 201 and second.status_code == 201
        third = await _key(w, sa["id"], "k3")
        assert third.status_code == 409, third.text
        assert slug_of(third) == "credential-limit-reached"
        revoked = await w.client.delete(
            f"{SERVICE_ACCOUNTS}/{sa['id']}/keys/{first.json()['id']}", headers=auth(w.admin)
        )
        assert revoked.status_code == 204
        assert (await _key(w, sa["id"], "k3")).status_code == 201
        assert (await _key(w, sa["id"], "k4")).status_code == 409
        # An expired key frees its slot without being revoked.
        w.sql(
            w.env.db_url,
            "UPDATE api_credentials SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 day' WHERE id = %s",
            second.json()["id"],
        )
        assert (await _key(w, sa["id"], "k5")).status_code == 201
        assert _live_keys(w, sa["id"]) == 2


async def test_five_concurrent_key_creations_yield_exactly_two_active_keys(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.admin, w.root, "racy-bot")
        results = await asyncio.gather(*[_key(w, sa["id"], f"k{i}") for i in range(5)])
        statuses = sorted(r.status_code for r in results)
        assert statuses == [201, 201, 409, 409, 409], [r.text for r in results]
        assert {slug_of(r) for r in results if r.status_code == 409} == {"credential-limit-reached"}
        assert _live_keys(w, sa["id"]) == 2


async def test_a_key_expiry_is_mandatory_and_capped(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.admin, w.root, "bot")
        too_far = await _key(w, sa["id"], "k", days=366)
        assert too_far.status_code == 422 and slug_of(too_far) == "expiry-out-of-range"
        past = await _key(w, sa["id"], "k", days=-1)
        assert past.status_code == 422 and slug_of(past) == "expiry-out-of-range"
        missing = await w.client.post(
            f"{SERVICE_ACCOUNTS}/{sa['id']}/keys", json={"name": "k"}, headers=auth(w.admin)
        )
        assert missing.status_code == 422 and slug_of(missing) == "validation-error"
        assert _live_keys(w, sa["id"]) == 0


async def test_listing_keys_flags_expiring_soon_and_shows_last_use_but_no_secret(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.admin, w.root, "bot")
        soon = (await _key(w, sa["id"], "soon", days=10)).json()
        later = (await _key(w, sa["id"], "later", days=30)).json()
        listed = await w.client.get(f"{SERVICE_ACCOUNTS}/{sa['id']}/keys", headers=auth(w.admin))
        assert listed.status_code == 200, listed.text
        items = {i["name"]: i for i in listed.json()["items"]}
        assert set(items) == {"soon", "later"}
        assert items["soon"]["status"] == "active" and items["soon"]["expiring_soon"] is True
        assert items["later"]["status"] == "active" and items["later"]["expiring_soon"] is False
        for item in items.values():
            assert set(item) == {
                "id",
                "name",
                "expires_at",
                "created_at",
                "revoked_at",
                "last_used_at",
                "status",
                "expiring_soon",
            }
            assert item["last_used_at"] is None
        # Use the first key, flush the batch, and only that key shows a last use.
        w.sql(
            w.env.db_url,
            "INSERT INTO role_assignments (folder_id, principal_type, principal_id, role, "
            "created_by, updated_by) VALUES (%s, 'service_account', %s, 'consumer', 't', 't')",
            w.team_a,
            sa["principal_id"],
        )
        used = await w.client.get(f"{IDENTITY}/folders/{w.team_a}", headers=auth(soon["token"]))
        assert used.status_code == 200, used.text
        await w.env.app.state.credential_usage.flush()
        again = await w.client.get(f"{SERVICE_ACCOUNTS}/{sa['id']}/keys", headers=auth(w.admin))
        by_name = {i["name"]: i for i in again.json()["items"]}
        assert by_name["soon"]["last_used_at"] is not None
        assert by_name["later"]["last_used_at"] is None
        assert later["id"] == by_name["later"]["id"]


async def test_rotation_a_key_authenticates_everywhere_and_a_revoked_key_stops_at_once(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.oa, w.org_a, "pipeline")
        w.sql(
            w.env.db_url,
            "INSERT INTO role_assignments (folder_id, principal_type, principal_id, role, "
            "created_by, updated_by) VALUES (%s, 'service_account', %s, 'consumer', 't', 't')",
            w.team_a,
            sa["principal_id"],
        )
        key1 = (await _key(w, sa["id"], "k1", token=w.oa)).json()
        key2 = (await _key(w, sa["id"], "k2", token=w.oa)).json()
        team = f"{IDENTITY}/folders/{w.team_a}"
        assert (await w.client.get(team, headers=auth(key1["token"]))).status_code == 200
        assert (await w.client.get(team, headers=auth(key2["token"]))).status_code == 200

        # Another service resolves the key through identity and is allowed what the grant allows.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.env.app)) as http:
            principal = await RemoteOpaqueResolver(
                http, base_url="http://identity", timeout_s=2.0
            ).resolve(key1["token"])
            assert principal.id == sa["principal_id"]
            assert principal.type == "service_account"
            assert principal.via == "api_key"
            authz = RemoteAuthorizer(
                http,
                base_url="http://identity",
                bearer=SecretStr(key1["token"]),
                timeout_s=2.0,
            )
            allowed = await authz.check(principal, "contract:read-published", uuid_of(w.team_a))
            denied = await authz.check(principal, "contract:create", uuid_of(w.team_a))
        assert allowed.outcome is Outcome.ALLOW
        assert denied.outcome is not Outcome.ALLOW

        revoked = await w.client.delete(
            f"{SERVICE_ACCOUNTS}/{sa['id']}/keys/{key1['id']}", headers=auth(w.oa)
        )
        assert revoked.status_code == 204
        assert (await w.client.get(team, headers=auth(key1["token"]))).status_code == 401
        assert (await w.client.get(team, headers=auth(key2["token"]))).status_code == 200
        before = len(events(w.sql, w.env, "credential.revoked"))
        assert before == 1
        again = await w.client.delete(
            f"{SERVICE_ACCOUNTS}/{sa['id']}/keys/{key1['id']}", headers=auth(w.oa)
        )
        assert again.status_code == 204
        assert len(events(w.sql, w.env, "credential.revoked")) == before

        other = await create_service_account(w.client, w.admin, w.root, "other")
        other_key = (await _key(w, other["id"], "ok")).json()
        wrong = await w.client.delete(
            f"{SERVICE_ACCOUNTS}/{sa['id']}/keys/{other_key['id']}", headers=auth(w.oa)
        )
        assert wrong.status_code == 404


async def test_a_deactivated_account_s_key_is_refused_and_a_pat_cannot_mint_keys(
    open_world: Callable[..., Any],
    create_service_account: CreateServiceAccount,
    create_pat: CreatePat,
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.admin, w.root, "bot")
        w.sql(
            w.env.db_url,
            "INSERT INTO role_assignments (folder_id, principal_type, principal_id, role, "
            "created_by, updated_by) VALUES (%s, 'service_account', %s, 'consumer', 't', 't')",
            w.team_a,
            sa["principal_id"],
        )
        key = (await _key(w, sa["id"], "k1")).json()
        team = f"{IDENTITY}/folders/{w.team_a}"
        assert (await w.client.get(team, headers=auth(key["token"]))).status_code == 200

        pat = (await create_pat(w.client, w.admin))["token"]
        by_pat = await _key(w, sa["id"], "k2", token=pat)
        assert by_pat.status_code == 403, by_pat.text
        assert slug_of(by_pat) == "interactive-session-required"
        by_key = await _key(w, sa["id"], "k2", token=key["token"])
        assert by_key.status_code in (403, 404)
        assert by_key.status_code != 201
        assert _live_keys(w, sa["id"]) == 1

        w.sql(w.env.db_url, "UPDATE service_accounts SET is_active = false WHERE id = %s", sa["id"])
        assert (await w.client.get(team, headers=auth(key["token"]))).status_code == 401
        inactive = await _key(w, sa["id"], "k3")
        assert inactive.status_code == 409, inactive.text
        assert slug_of(inactive) == "conflict"


def uuid_of(value: str) -> Any:
    import uuid

    return uuid.UUID(value)


# --- Plan 12: service accounts receive access like a user (AUTH-07, D-69, D-33) -----------------

Grant = Callable[..., Awaitable[Any]]
CreateGroup = Callable[..., Awaitable[str]]
GROUPS = f"{IDENTITY}/groups"


def neutral(resp: Any) -> dict[str, Any]:
    """A problem body without the per-request id and path, for byte-for-byte comparison."""
    body = dict(resp.json())
    body.pop("request_id", None)
    body.pop("instance", None)
    return body


async def _add_sa(w: World, token: str, group_id: str, principal_id: str) -> Any:
    return await w.client.post(
        f"{GROUPS}/{group_id}/members",
        json={"principal_id": principal_id},
        headers=auth(token),
    )


async def _reads_team(w: World, key: str) -> int:
    resp = await w.client.get(f"{IDENTITY}/folders/{w.team_a}", headers=auth(key))
    return int(resp.status_code)


async def test_a_service_account_in_a_group_holds_the_group_s_role_and_is_listed(
    open_world: Callable[..., Any],
    create_service_account: CreateServiceAccount,
    create_group: CreateGroup,
    grant: Grant,
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.oa, w.org_a, "grouped")
        key = (await _key(w, sa["id"], "k1", token=w.oa)).json()["token"]
        group = await create_group(w.client, w.oa, w.org_a, "Pipelines")
        assert (
            await grant(w.client, w.oa, w.team_a, group, "consumer", "local_group")
        ).status_code == 201
        assert await _reads_team(w, key) == 404

        added = await _add_sa(w, w.oa, group, sa["principal_id"])
        assert added.status_code == 201, added.text
        body = added.json()
        assert body["member_type"] == "service_account"
        assert body["member_id"] == sa["principal_id"]
        assert await _reads_team(w, key) == 200

        listed = await w.client.get(f"{GROUPS}/{group}/members", headers=auth(w.oa))
        assert [(m["member_type"], m["member_id"]) for m in listed.json()["items"]] == [
            ("service_account", sa["principal_id"])
        ]
        assert len(events(w.sql, w.env, "group.member_added")) == 1

        removed = await w.client.delete(
            f"{GROUPS}/{group}/members/{sa['principal_id']}", headers=auth(w.oa)
        )
        assert removed.status_code == 204, removed.text
        assert await _reads_team(w, key) == 404


async def test_a_service_account_is_granted_directly_and_revocation_applies_at_once(
    open_world: Callable[..., Any],
    create_service_account: CreateServiceAccount,
    grant: Grant,
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.oa, w.org_a, "direct")
        key = (await _key(w, sa["id"], "k1", token=w.oa)).json()["token"]
        made = await grant(
            w.client, w.oa, w.team_a, sa["principal_id"], "consumer", "service_account"
        )
        assert made.status_code == 201, made.text
        assert made.json()["principal_type"] == "service_account"
        assert made.json()["principal_id"] == sa["principal_id"]
        assert await _reads_team(w, key) == 200
        revoked = await w.client.delete(
            f"{IDENTITY}/grants/{made.json()['id']}", headers=auth(w.oa)
        )
        assert revoked.status_code == 204, revoked.text
        assert await _reads_team(w, key) == 404

        malformed = await grant(
            w.client, w.oa, w.team_a, "sa:NOT-A-UUID", "consumer", "service_account"
        )
        assert malformed.status_code == 422
        assert slug_of(malformed) == "validation-error"


async def test_an_org_owned_account_stays_in_its_org_except_for_a_global_admin(
    open_world: Callable[..., Any],
    create_service_account: CreateServiceAccount,
    grant: Grant,
    create_user: CreateUser,
    login: Login,
    grant_sql: GrantSql,
) -> None:
    async with open_world() as w:
        owned = await create_service_account(w.client, w.oa, w.org_a, "a-bot")
        rooted = await create_service_account(w.client, w.admin, w.root, "root-bot")
        for folder in (w.org_b,):
            # An Org admin of both organizations (not global) can read the account at its owner,
            # so the out-of-scope folder is a 422.
            both = await create_user(w.client, w.admin, "both-sa")
            grant_sql(w.env.db_url, both.principal_id, "org_admin", w.org_a)
            grant_sql(w.env.db_url, both.principal_id, "org_admin", w.org_b)
            both_token = await login(w.client, both.username, both.password)
            by_two_org_admin = await grant(
                w.client, both_token, folder, owned["principal_id"], "org_reader", "service_account"
            )
            assert by_two_org_admin.status_code == 422, by_two_org_admin.text
            assert slug_of(by_two_org_admin) == "principal-scope-mismatch"
            by_other_org_admin = await grant(
                w.client, w.ob, folder, owned["principal_id"], "org_reader", "service_account"
            )
            assert by_other_org_admin.status_code == 404, by_other_org_admin.text
            # a ROOT-owned account is grantable anywhere
            assert (
                await grant(
                    w.client, w.ob, folder, rooted["principal_id"], "org_reader", "service_account"
                )
            ).status_code == 201
        inside = await grant(
            w.client, w.oa, w.team_a, owned["principal_id"], "consumer", "service_account"
        )
        assert inside.status_code == 201, inside.text
        # A Global admin is not confined by the owner (quick 261119).
        by_global = await grant(
            w.client, w.admin, w.org_b, owned["principal_id"], "org_reader", "service_account"
        )
        assert by_global.status_code == 201, by_global.text


async def test_an_organization_owned_account_joins_only_groups_of_its_own_organization(
    open_world: Callable[..., Any],
    create_service_account: CreateServiceAccount,
    create_group: CreateGroup,
) -> None:
    async with open_world() as w:
        owned = await create_service_account(w.client, w.oa, w.org_a, "a-bot")
        rooted = await create_service_account(w.client, w.admin, w.root, "root-bot")
        root_group = await create_group(w.client, w.admin, w.root, "Everyone")
        b_group = await create_group(w.client, w.ob, w.org_b, "B pipelines")
        a_group = await create_group(w.client, w.oa, w.org_a, "A pipelines")

        for group in (root_group, b_group):
            refused = await _add_sa(w, w.admin, group, owned["principal_id"])
            assert refused.status_code == 422, refused.text
            assert slug_of(refused) == "principal-scope-mismatch"
        hidden = await _add_sa(w, w.ob, b_group, owned["principal_id"])
        assert hidden.status_code == 404, hidden.text
        assert (await _add_sa(w, w.oa, a_group, owned["principal_id"])).status_code == 201
        for group in (root_group, b_group, a_group):
            assert (await _add_sa(w, w.admin, group, rooted["principal_id"])).status_code == 201
        rows = w.sql(
            w.env.db_url,
            "SELECT count(*) AS n FROM group_members WHERE member_id = %s",
            owned["principal_id"],
        )
        assert rows == [{"n": 1}]


async def test_an_inactive_or_unknown_service_account_is_the_same_unknown_principal(
    open_world: Callable[..., Any],
    create_service_account: CreateServiceAccount,
    create_group: CreateGroup,
    grant: Grant,
) -> None:
    import uuid

    async with open_world() as w:
        inactive = await create_service_account(w.client, w.oa, w.org_a, "gone")
        w.sql(
            w.env.db_url,
            "UPDATE service_accounts SET is_active = false WHERE id = %s",
            inactive["id"],
        )
        group = await create_group(w.client, w.oa, w.org_a, "G")
        unknown = f"sa:{uuid.uuid4()}"
        answers = []
        for principal_id in (inactive["principal_id"], unknown):
            as_grant = await grant(
                w.client, w.oa, w.team_a, principal_id, "consumer", "service_account"
            )
            as_member = await _add_sa(w, w.oa, group, principal_id)
            assert as_grant.status_code == 422, as_grant.text
            assert as_member.status_code == 422, as_member.text
            assert slug_of(as_grant) == slug_of(as_member) == "unknown-principal"
            answers.extend([neutral(as_grant), neutral(as_member)])
        assert all(a == answers[0] for a in answers)


async def test_adding_a_service_account_obeys_the_membership_ceiling(
    open_world: Callable[..., Any],
    create_service_account: CreateServiceAccount,
    create_group: CreateGroup,
    grant: Grant,
    grant_sql: GrantSql,
) -> None:
    async with open_world() as w:
        sa = await create_service_account(w.client, w.oa, w.org_a, "ceil")
        group = await create_group(w.client, w.oa, w.org_a, "Straddler")
        assert (
            await grant(w.client, w.admin, w.team_a, group, "team_editor", "local_group")
        ).status_code == 201
        # The API never lets an A-owned group hold a role in B, so the state is built by SQL.
        grant_sql(w.env.db_url, group, "project_admin", w.org_b, "local_group")
        refused = await _add_sa(w, w.oa, group, sa["principal_id"])
        assert refused.status_code == 403, refused.text
        assert slug_of(refused) == "grant-ceiling-exceeded"
        assert (await _add_sa(w, w.admin, group, sa["principal_id"])).status_code == 201


# --- Quick 261120-lbz, D-03: the effective token limit also bounds a service account's keys ------


async def test_a_limit_of_one_refuses_the_second_key_with_token_limit_reached(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world(max_active_tokens=1) as w:
        sa = await create_service_account(w.client, w.admin, w.root, "one-key-bot")
        assert (await _key(w, sa["id"], "k1")).status_code == 201
        refused = await _key(w, sa["id"], "k2")
        assert refused.status_code == 409
        assert slug_of(refused) == "token-limit-reached"
        assert refused.json()["limit"] == 1 and refused.json()["active_count"] == 1
        assert _live_keys(w, sa["id"]) == 1


async def test_with_a_high_limit_the_third_key_is_still_credential_limit_reached(
    open_world: Callable[..., Any], create_service_account: CreateServiceAccount
) -> None:
    async with open_world(max_active_tokens=10) as w:
        sa = await create_service_account(w.client, w.admin, w.root, "two-key-bot")
        assert (await _key(w, sa["id"], "k1")).status_code == 201
        assert (await _key(w, sa["id"], "k2")).status_code == 201
        third = await _key(w, sa["id"], "k3")
        assert third.status_code == 409
        assert slug_of(third) == "credential-limit-reached"
        assert "limit" not in third.json()
