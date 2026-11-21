"""Personal access tokens on identity (AUTH-06, D-66, D-67, D-68, D-71, D-72).

A user creates a PAT with a mandatory, capped expiry. The ``dcp_`` token is a bearer token that
identity resolves from its database on every request: it acts as its owner with the owner's live
access, never more, and is dead the moment it expires or is revoked. The secret exists only in the
create response; only its SHA-256 is stored and no audit event carries it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
PATS = f"{IDENTITY}/pats"
FOLDERS = f"{IDENTITY}/folders"
TOKEN_SHAPE = re.compile(r"^dcp_[0-9a-f]{16}_[A-Za-z0-9_-]{43}$")
Sql = Callable[..., list[dict[str, Any]]]
AdminFlow = Callable[[Any, Any], Awaitable[str]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
CreateUser = Callable[..., Awaitable[Any]]
Login = Callable[..., Awaitable[str]]
Grant = Callable[..., Awaitable[Any]]
GrantSql = Callable[..., None]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _slug(resp: Any) -> str:
    return str(resp.json()["type"]).rsplit(":", 1)[-1]


def _expiry(*, days: float = 30, minutes: float = 0) -> str:
    return (datetime.now(UTC) + timedelta(days=days, minutes=minutes)).isoformat()


async def _post_pat(
    client: Any, token: str, *, name: str = "p", expires_at: str | None = None
) -> Any:
    """POST /pats as ``token``; returns the raw response so tests can assert any status."""
    return await client.post(
        PATS,
        json={"name": name, "expires_at": expires_at or _expiry()},
        headers=_bearer(token),
    )


@dataclass
class World:
    env: Any
    client: Any
    admin: str
    root: str
    org: str
    project: str
    team: str
    alice: Any  # CreatedUser, holds team_reader on ``team``
    alice_jwt: str
    alice_grant_id: str
    sql: Sql
    issuer: FakeIssuer
    grant: Grant
    grant_sql: GrantSql
    make_pat: Callable[..., Awaitable[dict[str, Any]]]
    create_user: CreateUser
    login: Login
    folder_chain: FolderChain

    async def folder_chain_for(self, token: str, prefix: str) -> tuple[str, str, str]:
        return await self.folder_chain(self.client, token, self.root, prefix)

    async def new_pat(self, name: str = "p", *, days: float = 30) -> dict[str, Any]:
        return await self.make_pat(self.client, self.alice_jwt, name=name, days=days)

    async def pats(self, token: str, query: str = "") -> Any:
        return await self.client.get(f"{PATS}{query}", headers=_bearer(token))

    async def revoke(self, token: str, credential_id: str) -> Any:
        return await self.client.delete(f"{PATS}/{credential_id}", headers=_bearer(token))

    def credential_row(self) -> dict[str, Any]:
        return self.sql(self.env.db_url, "SELECT * FROM api_credentials ORDER BY id")[0]

    def outbox(self, action: str) -> list[dict[str, Any]]:
        return self.sql(
            self.env.db_url,
            "SELECT before, after, actor FROM outbox WHERE action = %s ORDER BY id",
            action,
        )

    async def folder(self, token: str, folder_id: str) -> Any:
        return await self.client.get(f"{FOLDERS}/{folder_id}", headers=_bearer(token))

    def admit_entra_user(self, alice: str, group: str) -> str:
        """Register ``group`` at ROOT, put ``alice`` in it, grant her Team reader; her JWT."""
        self.env.directory.add_group(group, "engineers")
        self.sql(
            self.env.db_url,
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
            "display_name, created_by, updated_by) VALUES ('group', %s, %s, %s, 'engineers', "
            "'test', 'test')",
            ENTRA_TENANT_ID,
            group,
            self.root,
        )
        self.env.directory.add_principal("user", alice, groups=(group,))
        self.grant_sql(
            self.env.db_url, f"entra:{ENTRA_TENANT_ID}:{alice}", "team_reader", self.team
        )
        return self.issuer.mint_entra(alice)


@pytest.fixture
def open_world(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant: Grant,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Any]:
    @asynccontextmanager
    async def _open(**overrides: object) -> AsyncIterator[World]:
        env = make_identity(**overrides)
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
            org, project, team = await folder_chain(client, admin, root, "pt")
            alice = await create_user(client, admin, "alice")
            granted = await grant(client, admin, team, alice.principal_id, "team_reader")
            assert granted.status_code == 201, granted.text
            yield World(
                env=env,
                client=client,
                admin=admin,
                root=root,
                org=org,
                project=project,
                team=team,
                alice=alice,
                alice_jwt=await login(client, alice.username, alice.password),
                alice_grant_id=granted.json()["id"],
                sql=sql,
                issuer=fake_issuer,
                grant=grant,
                grant_sql=grant_sql,
                make_pat=create_pat,
                create_user=create_user,
                login=login,
                folder_chain=folder_chain,
            )

    return _open


async def test_create_returns_the_token_once_and_stores_only_its_hash(
    open_world: Callable[..., Any],
) -> None:
    from dc_identity.models import ApiCredential

    async with open_world() as w:
        body = await w.new_pat("ci")
        token = body["token"]
        assert TOKEN_SHAPE.fullmatch(token), token
        _, public_id, secret = token.split("_", 2)
        rows = w.sql(w.env.db_url, "SELECT * FROM api_credentials")
        listing = await w.client.get(PATS, headers=_bearer(w.alice_jwt))
        events = w.sql(
            w.env.db_url,
            "SELECT action, actor::text AS actor, before::text AS before, after::text AS after "
            "FROM outbox WHERE starts_with(action, 'credential.') ORDER BY id",
        )
    assert body["name"] == "ci" and body["read_only"] is False
    assert len(rows) == 1
    row = rows[0]
    assert row["public_id"] == public_id
    assert row["secret_hash"] == hashlib.sha256(secret.encode()).hexdigest()
    assert row["kind"] == "pat" and row["revoked_at"] is None
    assert secret not in json.dumps({k: str(v) for k, v in row.items() if k != "secret_hash"})
    assert [e["action"] for e in events] == ["credential.created"]
    wire = json.dumps(events)
    assert token not in wire and secret not in wire
    assert public_id not in wire and row["secret_hash"] not in wire
    assert token not in listing.text and secret not in listing.text
    assert "public_id" not in ApiCredential.__audit_fields__
    assert "secret_hash" not in ApiCredential.__audit_fields__


async def test_a_pat_acts_as_its_owner_and_nothing_more(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        token = (await w.new_pat())["token"]
        on_team = await w.folder(token, w.team)
        on_project = await w.folder(token, w.project)
    assert on_team.status_code == 200, on_team.text
    assert on_project.status_code == 404, on_project.text


async def test_removing_the_owners_grant_removes_the_pat_access_on_the_next_request(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        token = (await w.new_pat())["token"]
        before = await w.folder(token, w.team)
        revoked = await w.client.delete(
            f"{IDENTITY}/grants/{w.alice_grant_id}", headers=_bearer(w.admin)
        )
        during = await w.folder(token, w.team)
        restored = await w.grant(w.client, w.admin, w.team, w.alice.principal_id, "team_reader")
        after = await w.folder(token, w.team)
    assert before.status_code == 200
    assert revoked.status_code == 204, revoked.text
    assert during.status_code == 404, during.text
    assert restored.status_code == 201, restored.text
    assert after.status_code == 200, after.text


async def test_a_wrong_secret_an_unknown_id_and_an_expired_pat_are_the_generic_401(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        token = (await w.new_pat())["token"]
        _, public_id, secret = token.split("_", 2)
        flipped = ("A" if secret[0] != "A" else "B") + secret[1:]
        wrong = await w.folder(f"dcp_{public_id}_{flipped}", w.team)
        unknown = await w.folder(f"dcp_{'0' * 16}_{secret}", w.team)
        w.sql(
            w.env.db_url,
            "UPDATE api_credentials SET created_at = now() - interval '1 day', "
            "expires_at = now() - interval '1 second'",
        )
        expired = await w.folder(token, w.team)
    for resp in (wrong, unknown, expired):
        assert resp.status_code == 401, resp.text
        assert _slug(resp) == "unauthenticated"
    # One generic body: nothing tells a wrong secret from an unknown id or an expired token.
    shapes = {
        (r.json()["type"], r.json()["title"], r.json()["detail"]) for r in (wrong, unknown, expired)
    }
    assert len(shapes) == 1


async def test_expiry_is_mandatory_and_capped(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        missing = await w.client.post(PATS, json={"name": "p"}, headers=_bearer(w.alice_jwt))
        yesterday = await _post_pat(w.client, w.alice_jwt, expires_at=_expiry(days=-1))
        too_far = await _post_pat(w.client, w.alice_jwt, expires_at=_expiry(days=366))
        naive = await _post_pat(
            w.client, w.alice_jwt, expires_at=(datetime.now() + timedelta(days=3)).isoformat()
        )
        inside = await _post_pat(w.client, w.alice_jwt, expires_at=_expiry(days=365, minutes=-1))
        extra = await w.client.post(
            PATS,
            json={"name": "p", "expires_at": _expiry(), "revoked_at": _expiry()},
            headers=_bearer(w.alice_jwt),
        )
    assert missing.status_code == 422 and _slug(missing) == "validation-error"
    assert yesterday.status_code == 422 and _slug(yesterday) == "expiry-out-of-range"
    assert too_far.status_code == 422 and _slug(too_far) == "expiry-out-of-range"
    assert naive.status_code == 422, naive.text
    assert inside.status_code == 201, inside.text
    assert extra.status_code == 422 and _slug(extra) == "validation-error"


async def test_two_creations_with_the_same_name_are_two_distinct_credentials(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        first = await w.new_pat("same")
        second = await w.new_pat("same")
    assert first["id"] != second["id"]
    assert first["token"] != second["token"]
    assert first["token"].split("_")[1] != second["token"].split("_")[1]


async def test_a_pat_is_never_created_without_a_token_or_by_a_pat(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        anonymous = await w.client.post(PATS, json={"name": "p", "expires_at": _expiry()})
        token = (await w.new_pat())["token"]
        by_pat = await _post_pat(w.client, token)
    assert anonymous.status_code == 401
    assert by_pat.status_code == 403, by_pat.text


async def test_an_entra_users_pat_follows_the_directory_and_registration(
    open_world: Callable[..., Any],
) -> None:
    alice, group = str(uuid.uuid4()), str(uuid.uuid4())
    async with open_world() as w:
        jwt = w.admit_entra_user(alice, group)
        created = await _post_pat(w.client, jwt, name="entra")
        assert created.status_code == 201, created.text
        token = created.json()["token"]
        working = await w.folder(token, w.team)
        w.env.directory.set_enabled(alice, False)
        disabled = await w.folder(token, w.team)
        w.env.directory.set_enabled(alice, True)
        w.env.directory.set_groups(alice, set())
        unregistered = await w.folder(token, w.team)
    assert working.status_code == 200, working.text
    assert disabled.status_code == 401, disabled.text
    assert unregistered.status_code == 403, unregistered.text
    assert _slug(unregistered) == "principal-not-registered"


async def test_a_live_pat_whose_owner_row_turned_inactive_is_refused_by_the_gate(
    open_world: Callable[..., Any],
) -> None:
    """Deactivation revokes PATs; even an owner row made inactive without that cascade is 401."""
    alice, group = str(uuid.uuid4()), str(uuid.uuid4())
    async with open_world() as w:
        local_pat = (await w.new_pat("local"))["token"]
        made = await _post_pat(w.client, w.admit_entra_user(alice, group), name="entra")
        assert made.status_code == 201, made.text
        entra_pat = made.json()["token"]
        before = [(await w.folder(t, w.team)).status_code for t in (local_pat, entra_pat)]
        w.sql(
            w.env.db_url, "UPDATE users SET is_active = false WHERE username = %s", w.alice.username
        )
        w.sql(w.env.db_url, "UPDATE entra_users SET is_active = false WHERE object_id = %s", alice)
        after = [await w.folder(t, w.team) for t in (local_pat, entra_pat)]
        live = w.sql(
            w.env.db_url, "SELECT count(*) AS n FROM api_credentials WHERE revoked_at IS NULL"
        )
    assert before == [200, 200]
    assert [(r.status_code, _slug(r)) for r in after] == [(401, "unauthenticated")] * 2
    assert live == [{"n": 2}]


async def test_a_service_principal_cannot_create_a_pat(open_world: Callable[..., Any]) -> None:
    sp = str(uuid.uuid4())
    async with open_world() as w:
        w.env.directory.add_principal("service_principal", sp)
        w.sql(
            w.env.db_url,
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
            "display_name, created_by, updated_by) VALUES ('service_principal', %s, %s, %s, "
            "'pipeline', 'test', 'test')",
            ENTRA_TENANT_ID,
            sp,
            w.root,
        )
        resp = await _post_pat(w.client, w.issuer.mint_entra(sp, token_type="app"))  # noqa: S106
    assert resp.status_code == 403, resp.text
    # pat:manage is interactive-only and a service principal is not interactive (D-79, WR-01).
    assert _slug(resp) == "interactive-session-required"


async def test_an_owner_with_no_pats_gets_an_empty_page(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        resp = await w.pats(w.alice_jwt)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "next_cursor": None, "active_count": 0, "max_active": 10}


async def test_the_list_is_in_creation_order_walks_by_cursor_and_never_shows_a_secret(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        made = [await w.new_pat(name) for name in ("one", "two", "three")]
        full = await w.pats(w.alice_jwt)
        walked: list[dict[str, Any]] = []
        query = "?limit=1"
        for _ in range(5):
            page = (await w.pats(w.alice_jwt, query)).json()
            walked.extend(page["items"])
            if page["next_cursor"] is None:
                break
            query = f"?limit=1&cursor={page['next_cursor']}"
    assert [i["id"] for i in full.json()["items"]] == [m["id"] for m in made]
    assert [i["id"] for i in walked] == [m["id"] for m in made]
    for item in full.json()["items"]:
        assert item["status"] == "active"
        assert item["last_used_at"] is None and item["revoked_at"] is None
        assert not {"token", "public_id", "secret_hash", "secret"} & item.keys()
    for m in made:
        assert m["token"] not in full.text


async def test_a_pat_list_shows_only_the_callers_own_tokens(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        await w.new_pat("alices")
        bob = await w.create_user(w.client, w.admin, "bob")
        bob_jwt = await w.login(w.client, bob.username, bob.password)
        mine = await w.pats(bob_jwt)
    assert mine.json() == {"items": [], "next_cursor": None, "active_count": 0, "max_active": 10}


async def test_revoking_ends_the_pat_on_the_next_request_and_is_audited_once(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        body = await w.new_pat()
        token = body["token"]
        working = await w.folder(token, w.team)
        revoked = await w.revoke(w.alice_jwt, body["id"])
        after = await w.folder(token, w.team)
        again = await w.revoke(w.alice_jwt, body["id"])
        events = w.outbox("credential.revoked")
        listed = (await w.pats(w.alice_jwt)).json()["items"]
    assert working.status_code == 200
    assert revoked.status_code == 204, revoked.text
    assert after.status_code == 401, after.text
    assert again.status_code == 204, again.text
    assert len(events) == 1
    assert events[0]["before"]["revoked_at"] is None
    assert events[0]["after"]["revoked_at"] is not None
    assert token not in json.dumps(events) and token.split("_", 2)[2] not in json.dumps(events)
    assert listed[0]["status"] == "revoked" and listed[0]["revoked_at"] is not None


async def test_another_users_pat_and_an_unknown_id_are_404(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        body = await w.new_pat()
        bob = await w.create_user(w.client, w.admin, "bob")
        bob_jwt = await w.login(w.client, bob.username, bob.password)
        other = await w.revoke(bob_jwt, body["id"])
        unknown = await w.revoke(bob_jwt, str(uuid.uuid4()))
        still = await w.folder(body["token"], w.team)
        events = w.outbox("credential.revoked")
    assert other.status_code == 404 and _slug(other) == "not-found"
    assert unknown.status_code == 404
    assert still.status_code == 200
    assert events == []


async def test_an_expired_pat_lists_as_expired(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        await w.new_pat()
        w.sql(
            w.env.db_url,
            "UPDATE api_credentials SET created_at = now() - interval '1 day', "
            "expires_at = now() - interval '1 second'",
        )
        listed = (await w.pats(w.alice_jwt)).json()["items"]
    assert [i["status"] for i in listed] == ["expired"]


async def test_last_used_at_is_written_only_by_the_batched_flush(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        body = await w.new_pat()
        token = body["token"]
        fresh = (await w.pats(w.alice_jwt)).json()["items"][0]
        before = w.credential_row()
        events_before = len(w.sql(w.env.db_url, "SELECT id FROM outbox"))
        for _ in range(3):
            assert (await w.folder(token, w.team)).status_code == 200
        unflushed = w.credential_row()
        recorder = w.env.app.state.credential_usage
        flushed = await recorder.flush()
        after = w.credential_row()
        events_after = len(w.sql(w.env.db_url, "SELECT id FROM outbox"))
        shown = (await w.pats(w.alice_jwt)).json()["items"][0]
        nothing = await recorder.flush()
        # A later flush with an older timestamp never moves last_used_at backwards.
        credential_id = uuid.UUID(body["id"])
        recorder.record(credential_id, after["last_used_at"] - timedelta(hours=1))
        await recorder.flush()
        monotonic = w.credential_row()
    assert fresh["last_used_at"] is None
    assert before["last_used_at"] is None and unflushed["last_used_at"] is None
    assert flushed == 1 and nothing == 0
    assert abs((datetime.now(UTC) - after["last_used_at"]).total_seconds()) < 30
    assert after["updated_at"] == before["updated_at"]
    assert after["updated_by"] == before["updated_by"]
    assert events_after == events_before
    assert shown["last_used_at"] is not None
    assert monotonic["last_used_at"] == after["last_used_at"]


async def test_the_lifespan_flushes_on_shutdown(open_world: Callable[..., Any]) -> None:
    async with open_world(credential_usage_flush_s=3600) as w:
        token = (await w.new_pat())["token"]
        assert (await w.folder(token, w.team)).status_code == 200
        assert w.credential_row()["last_used_at"] is None
    assert w.credential_row()["last_used_at"] is not None


async def test_the_lifespan_flushes_periodically(open_world: Callable[..., Any]) -> None:
    async with open_world(credential_usage_flush_s=0.05) as w:
        token = (await w.new_pat())["token"]
        assert (await w.folder(token, w.team)).status_code == 200
        for _ in range(100):
            if w.credential_row()["last_used_at"] is not None:
                break
            await asyncio.sleep(0.05)
        assert w.credential_row()["last_used_at"] is not None


# --- Plan 09: no PAT can manage credentials, grants, memberships, registrations, users or
# passwords (D-79). The refusal is 403 interactive-session-required, applied only after the target
# has been confirmed visible (D-35), and the owner's interactive JWT keeps working. ----------------

REFUSED = "interactive-session-required"
CHECK = "/internal/v1/authz/check"
FOLDER_SCOPE = "/internal/v1/authz/accessible-folders"


async def _admin_pat(w: World) -> str:
    return str((await w.make_pat(w.client, w.admin, name="admin-ci"))["token"])


async def _new_user_grant(w: World, token: str, username: str = "grantee") -> Any:
    user = await w.create_user(w.client, w.admin, username)
    return await w.grant(w.client, token, w.team, user.principal_id, "team_reader")


async def test_a_pat_cannot_create_a_pat(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        pat = await _admin_pat(w)
        resp = await _post_pat(w.client, pat)
        count = w.sql(w.env.db_url, "SELECT count(*) AS n FROM api_credentials")[0]["n"]
    assert resp.status_code == 403, resp.text
    assert _slug(resp) == REFUSED
    assert count == 1


async def test_a_pat_cannot_revoke_a_pat(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        pat = await _admin_pat(w)
        other = await w.new_pat("victim")
        resp = await w.revoke(pat, other["id"])
        still = await w.folder(other["token"], w.team)
    assert resp.status_code == 403, resp.text
    assert _slug(resp) == REFUSED
    assert still.status_code == 200


async def test_a_pat_cannot_create_a_grant(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        pat = await _admin_pat(w)
        resp = await _new_user_grant(w, pat)
        rows = w.sql(w.env.db_url, "SELECT count(*) AS n FROM role_assignments")[0]["n"]
        by_jwt = await _new_user_grant(w, w.admin, "grantee-two")
    assert resp.status_code == 403, resp.text
    assert _slug(resp) == REFUSED
    assert by_jwt.status_code == 201, by_jwt.text
    assert rows >= 2  # the bootstrap Global admin and alice, nothing from the refused call


async def test_a_pat_cannot_revoke_a_grant(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        pat = await _admin_pat(w)
        resp = await w.client.delete(f"{IDENTITY}/grants/{w.alice_grant_id}", headers=_bearer(pat))
        still = await w.folder(w.alice_jwt, w.team)
    assert resp.status_code == 403, resp.text
    assert _slug(resp) == REFUSED
    assert still.status_code == 200


async def test_a_pat_cannot_change_group_memberships(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        made = await w.client.post(
            f"{IDENTITY}/groups",
            json={"owner_folder_id": w.org, "name": "ops"},
            headers=_bearer(w.admin),
        )
        assert made.status_code == 201, made.text
        members = f"{IDENTITY}/groups/{made.json()['id']}/members"
        bob = await w.create_user(w.client, w.admin, "bob")
        pat = await _admin_pat(w)
        resp = await w.client.post(members, json={"username": bob.username}, headers=_bearer(pat))
        by_jwt = await w.client.post(
            members, json={"username": bob.username}, headers=_bearer(w.admin)
        )
    assert resp.status_code == 403, resp.text
    assert _slug(resp) == REFUSED
    assert by_jwt.status_code == 201, by_jwt.text


async def test_a_pat_cannot_register_an_entra_group(open_world: Callable[..., Any]) -> None:
    group = str(uuid.uuid4())
    async with open_world() as w:
        w.env.directory.add_group(group, "engineers")
        pat = await _admin_pat(w)
        body = {"owner_folder_id": w.root, "object_id": group}
        resp = await w.client.post(f"{IDENTITY}/entra/groups", json=body, headers=_bearer(pat))
        by_jwt = await w.client.post(
            f"{IDENTITY}/entra/groups", json=body, headers=_bearer(w.admin)
        )
    assert resp.status_code == 403, resp.text
    assert _slug(resp) == REFUSED
    assert by_jwt.status_code == 201, by_jwt.text


async def test_a_pat_cannot_create_a_user(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        pat = await _admin_pat(w)
        body = {"username": "mallory", "password": "Zx9-correct-horse-battery", "display_name": "M"}
        resp = await w.client.post(f"{IDENTITY}/users", json=body, headers=_bearer(pat))
        users = w.sql(w.env.db_url, "SELECT username FROM users WHERE username = 'mallory'")
        by_jwt = await w.client.post(f"{IDENTITY}/users", json=body, headers=_bearer(w.admin))
    assert resp.status_code == 403, resp.text
    assert _slug(resp) == REFUSED
    assert users == []
    assert by_jwt.status_code == 201, by_jwt.text


async def test_a_pat_cannot_rotate_the_owners_password(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        pat = await _admin_pat(w)
        before = w.sql(w.env.db_url, "SELECT password_hash FROM users WHERE username = 'admin'")
        resp = await w.client.post(
            f"{IDENTITY}/auth/password",
            json={"new_password": "Another-long-passphrase-42", "current_password": "x"},
            headers=_bearer(pat),
        )
        after = w.sql(w.env.db_url, "SELECT password_hash FROM users WHERE username = 'admin'")
    assert resp.status_code == 403, resp.text
    assert before == after


async def test_a_pat_keeps_its_read_access_to_the_owners_view(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        pat = await _admin_pat(w)
        listed = await w.pats(pat)
        folder = await w.folder(pat, w.team)
    assert listed.status_code == 200, listed.text
    assert folder.status_code == 200, folder.text


async def test_the_refusal_never_reveals_a_folder_the_owner_cannot_see(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        org_a, _project_a, _team_a = await w.folder_chain_for(w.admin, "oa")
        boss = await w.create_user(w.client, w.admin, "boss")
        granted = await w.grant(w.client, w.admin, org_a, boss.principal_id, "org_admin")
        assert granted.status_code == 201, granted.text
        boss_jwt = await w.login(w.client, boss.username, boss.password)
        boss_pat = str((await w.make_pat(w.client, boss_jwt, name="boss"))["token"])
        target = await w.create_user(w.client, w.admin, "target")
        body = {
            "principal_type": "user",
            "principal_id": target.principal_id,
            "role": "team_reader",
        }
        url = f"{FOLDERS}/{w.team}/grants"  # a team in the OTHER organization
        by_pat = await w.client.post(url, json=body, headers=_bearer(boss_pat))
        by_jwt = await w.client.post(url, json=body, headers=_bearer(boss_jwt))
        check = await w.client.post(
            CHECK,
            json={"permission": "grant:manage", "folder_id": w.team},
            headers=_bearer(boss_pat),
        )
    assert by_pat.status_code == 404, by_pat.text
    assert _slug(by_pat) == "not-found"
    assert (by_pat.status_code, _slug(by_pat)) == (by_jwt.status_code, _slug(by_jwt))
    assert check.status_code == 200, check.text
    assert check.json()["outcome"] == "not_found"


async def test_a_team_readers_pat_gets_the_status_its_jwt_gets_on_a_grant(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        pat = await w.new_pat()
        target = await w.create_user(w.client, w.admin, "target")
        body = {
            "principal_type": "user",
            "principal_id": target.principal_id,
            "role": "team_reader",
        }
        url = f"{FOLDERS}/{w.team}/grants"
        by_pat = await w.client.post(url, json=body, headers=_bearer(pat["token"]))
        by_jwt = await w.client.post(url, json=body, headers=_bearer(w.alice_jwt))
    assert by_jwt.status_code == 403 and _slug(by_jwt) == "forbidden"
    assert (by_pat.status_code, _slug(by_pat)) == (by_jwt.status_code, _slug(by_jwt))
    assert _slug(by_pat) != REFUSED


async def test_the_internal_check_refuses_only_a_decision_that_would_have_been_allow(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        pat = await _admin_pat(w)
        allowed_read = await w.client.post(
            CHECK, json={"permission": "folder:read", "folder_id": w.team}, headers=_bearer(pat)
        )
        manage = await w.client.post(
            CHECK, json={"permission": "grant:manage", "folder_id": w.team}, headers=_bearer(pat)
        )
        by_jwt = await w.client.post(
            CHECK,
            json={"permission": "grant:manage", "folder_id": w.team},
            headers=_bearer(w.admin),
        )
    assert allowed_read.json()["outcome"] == "allow"
    assert manage.status_code == 403 and _slug(manage) == REFUSED
    assert by_jwt.json()["outcome"] == "allow"


async def test_a_pat_has_an_empty_scope_for_an_interactive_only_permission(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        pat = await _admin_pat(w)
        scope = await w.client.post(
            FOLDER_SCOPE, json={"permission": "grant:manage"}, headers=_bearer(pat)
        )
        jwt_scope = await w.client.post(
            FOLDER_SCOPE, json={"permission": "grant:manage"}, headers=_bearer(w.admin)
        )
        read_scope = await w.client.post(
            FOLDER_SCOPE, json={"permission": "folder:read"}, headers=_bearer(pat)
        )
    assert scope.json() == {"all": False, "folder_ids": [], "excluded_subtrees": []}
    assert jwt_scope.json()["all"] is True
    assert read_scope.json()["all"] is True


# --- D-68, D-79: the Global admin oversees and revokes every PAT -------------------------------

ADMIN_PATS = f"{IDENTITY}/admin/pats"


async def test_the_global_admin_lists_every_pat_without_any_secret(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        mine = await w.new_pat("alices", days=30)
        ro = await w.make_pat(w.client, w.admin, name="admins")
        listing = await w.client.get(ADMIN_PATS, headers=_bearer(w.admin))
        alice_id = w.alice.principal_id
        owner_filtered = await w.client.get(
            ADMIN_PATS, params={"owner": alice_id}, headers=_bearer(w.admin)
        )
        await w.revoke(w.alice_jwt, mine["id"])
        revoked = await w.client.get(
            ADMIN_PATS, params={"status": "revoked"}, headers=_bearer(w.admin)
        )
        active = await w.client.get(
            ADMIN_PATS, params={"status": "active"}, headers=_bearer(w.admin)
        )
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert {i["name"] for i in items} == {"alices", "admins"}
    for item in items:
        assert set(item) == {
            "id",
            "owner_principal_id",
            "name",
            "read_only",
            "expires_at",
            "created_at",
            "revoked_at",
            "last_used_at",
            "status",
        }
        assert item["owner_principal_id"].startswith("local:")
    assert "dcp_" not in listing.text
    for body in (mine, ro):
        assert body["token"].split("_", 2)[2] not in listing.text
    assert [i["name"] for i in owner_filtered.json()["items"]] == ["alices"]
    assert [i["name"] for i in revoked.json()["items"]] == ["alices"]
    assert [i["name"] for i in active.json()["items"]] == ["admins"]


async def test_the_admin_listing_names_an_entra_owner_filters_on_it_and_walks_by_cursor(
    open_world: Callable[..., Any],
) -> None:
    alice, group = str(uuid.uuid4()), str(uuid.uuid4())
    entra_owner = f"entra:{ENTRA_TENANT_ID}:{alice}"
    async with open_world() as w:
        jwt = w.admit_entra_user(alice, group)
        for name in ("entra-one", "entra-two"):
            assert (await _post_pat(w.client, jwt, name=name)).status_code == 201
        await w.new_pat("local-one")
        by_owner = await w.client.get(
            ADMIN_PATS, params={"owner": entra_owner}, headers=_bearer(w.admin)
        )
        walked: list[dict[str, Any]] = []
        params = {"limit": "1"}
        for _ in range(5):
            page = await w.client.get(ADMIN_PATS, params=params, headers=_bearer(w.admin))
            assert page.status_code == 200, page.text
            walked.extend(page.json()["items"])
            if page.json()["next_cursor"] is None:
                break
            params = {"limit": "1", "cursor": page.json()["next_cursor"]}
    assert by_owner.status_code == 200, by_owner.text
    assert [(i["name"], i["owner_principal_id"]) for i in by_owner.json()["items"]] == [
        ("entra-one", entra_owner),
        ("entra-two", entra_owner),
    ]
    assert [i["name"] for i in walked] == ["entra-one", "entra-two", "local-one"]
    assert walked[2]["owner_principal_id"] == w.alice.principal_id


async def test_an_org_admin_cannot_list_or_revoke_any_pat(open_world: Callable[..., Any]) -> None:
    async with open_world() as w:
        body = await w.new_pat()
        boss = await w.create_user(w.client, w.admin, "boss")
        granted = await w.grant(w.client, w.admin, w.org, boss.principal_id, "org_admin")
        assert granted.status_code == 201, granted.text
        boss_jwt = await w.login(w.client, boss.username, boss.password)
        listing = await w.client.get(ADMIN_PATS, headers=_bearer(boss_jwt))
        revoke = await w.client.delete(f"{ADMIN_PATS}/{body['id']}", headers=_bearer(boss_jwt))
        still = await w.folder(body["token"], w.team)
    assert listing.status_code == 403, listing.text
    assert revoke.status_code == 403, revoke.text
    assert still.status_code == 200


async def test_the_global_admin_revokes_any_pat_once_and_it_is_audited(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        body = await w.new_pat()
        working = await w.folder(body["token"], w.team)
        revoked = await w.client.delete(f"{ADMIN_PATS}/{body['id']}", headers=_bearer(w.admin))
        after = await w.folder(body["token"], w.team)
        again = await w.client.delete(f"{ADMIN_PATS}/{body['id']}", headers=_bearer(w.admin))
        unknown = await w.client.delete(f"{ADMIN_PATS}/{uuid.uuid4()}", headers=_bearer(w.admin))
        events = w.outbox("credential.revoked")
        admin_id = w.sql(w.env.db_url, "SELECT id FROM users WHERE username = 'admin'")[0]["id"]
    assert working.status_code == 200
    assert revoked.status_code == 204, revoked.text
    assert after.status_code == 401, after.text
    assert again.status_code == 204, again.text
    assert unknown.status_code == 404, unknown.text
    assert len(events) == 1
    assert str(admin_id) in json.dumps(events[0]["actor"])
    assert body["token"] not in json.dumps(events)


async def test_an_admin_pat_cannot_revoke_a_pat_it_needs_an_interactive_sign_in(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        body = await w.new_pat()
        admin_pat = (await w.make_pat(w.client, w.admin, name="admin-pat"))["token"]
        listed = await w.client.get(ADMIN_PATS, headers=_bearer(admin_pat))
        refused = await w.client.delete(f"{ADMIN_PATS}/{body['id']}", headers=_bearer(admin_pat))
        still = await w.folder(body["token"], w.team)
    assert listed.status_code == 200, listed.text  # pat:read-all is a read, not interactive-only
    assert refused.status_code == 403, refused.text
    assert _slug(refused) == "interactive-session-required"
    assert still.status_code == 200


# --- Quick 261120-lbz, D-03: at most `max_active_tokens` live PATs per owner ----------------------


def _limit(resp: Any) -> tuple[int, str, Any, Any]:
    body = resp.json()
    return resp.status_code, _slug(resp), body.get("limit"), body.get("active_count")


async def test_the_default_limit_is_ten_and_the_list_reports_the_count_and_the_limit(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        empty = await w.pats(w.alice_jwt)
        assert empty.json()["active_count"] == 0 and empty.json()["max_active"] == 10
        for i in range(10):
            assert (await _post_pat(w.client, w.alice_jwt, name=f"p{i}")).status_code == 201
        listing = await w.pats(w.alice_jwt, "?limit=2")
        assert listing.json()["active_count"] == 10 and listing.json()["max_active"] == 10
        assert len(listing.json()["items"]) == 2
        refused = await _post_pat(w.client, w.alice_jwt, name="eleven")
    assert _limit(refused) == (409, "token-limit-reached", 10, 10)


async def test_at_the_limit_a_pat_is_refused_with_no_row_and_no_event(
    open_world: Callable[..., Any],
) -> None:
    async with open_world(max_active_tokens=3) as w:
        for i in range(3):
            assert (await _post_pat(w.client, w.alice_jwt, name=f"p{i}")).status_code == 201
        rows = len(w.sql(w.env.db_url, "SELECT 1 FROM api_credentials"))
        events = len(w.outbox("credential.created"))
        refused = await _post_pat(w.client, w.alice_jwt, name="p3")
        assert _limit(refused) == (409, "token-limit-reached", 3, 3)
        assert refused.headers["content-type"] == "application/problem+json"
        assert len(w.sql(w.env.db_url, "SELECT 1 FROM api_credentials")) == rows
        assert len(w.outbox("credential.created")) == events
        # Another owner is unaffected.
        assert (await _post_pat(w.client, w.admin, name="admin")).status_code == 201


async def test_revoked_and_expired_pats_do_not_count(open_world: Callable[..., Any]) -> None:
    async with open_world(max_active_tokens=3) as w:
        created = [await w.new_pat(f"p{i}") for i in range(3)]
        assert (await _post_pat(w.client, w.alice_jwt, name="x")).status_code == 409
        assert (await w.revoke(w.alice_jwt, created[0]["id"])).status_code == 204
        w.sql(
            w.env.db_url,
            "UPDATE api_credentials SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 day' WHERE id = %s",
            created[1]["id"],
        )
        assert (await _post_pat(w.client, w.alice_jwt, name="y")).status_code == 201
        assert (await _post_pat(w.client, w.alice_jwt, name="z")).status_code == 201
        refused = await _post_pat(w.client, w.alice_jwt, name="w")
        listing = await w.pats(w.alice_jwt)
    assert _limit(refused) == (409, "token-limit-reached", 3, 3)
    assert listing.json()["active_count"] == 3


async def test_an_entra_user_counts_by_their_own_owner_row(open_world: Callable[..., Any]) -> None:
    alice, group = str(uuid.uuid4()), str(uuid.uuid4())
    async with open_world(max_active_tokens=1) as w:
        jwt = w.admit_entra_user(alice, group)
        assert (await _post_pat(w.client, jwt, name="one")).status_code == 201
        refused = await _post_pat(w.client, jwt, name="two")
        local = await _post_pat(w.client, w.alice_jwt, name="local")
    assert _limit(refused) == (409, "token-limit-reached", 1, 1)
    assert local.status_code == 201


async def test_two_concurrent_creations_at_two_of_three_make_exactly_one_token(
    open_world: Callable[..., Any],
) -> None:
    async with open_world(max_active_tokens=3) as w:
        for i in range(2):
            assert (await _post_pat(w.client, w.alice_jwt, name=f"p{i}")).status_code == 201
        results = await asyncio.gather(
            *[_post_pat(w.client, w.alice_jwt, name=f"race{i}") for i in range(2)]
        )
        statuses = sorted(r.status_code for r in results)
        live = w.sql(
            w.env.db_url,
            "SELECT count(*) AS n FROM api_credentials WHERE revoked_at IS NULL "
            "AND expires_at > now()",
        )[0]["n"]
    assert statuses == [201, 409], [r.text for r in results]
    assert {_slug(r) for r in results if r.status_code == 409} == {"token-limit-reached"}
    assert live == 3


async def test_a_stored_limit_below_the_count_keeps_existing_tokens_and_refuses_new_ones(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        created = [await w.new_pat(f"p{i}") for i in range(4)]
        saved = await w.client.put(
            f"{IDENTITY}/settings/security",
            json={"max_active_tokens": 2},
            headers={**_bearer(w.admin), "If-Match": '"security-r0"'},
        )
        assert saved.status_code == 200, saved.text
        refused = await _post_pat(w.client, w.alice_jwt, name="new")
        listing = await w.pats(w.alice_jwt)
        kept = await w.folder(created[0]["token"], w.team)
    assert _limit(refused) == (409, "token-limit-reached", 2, 4)
    assert listing.json()["active_count"] == 4 and listing.json()["max_active"] == 2
    assert kept.status_code == 200
