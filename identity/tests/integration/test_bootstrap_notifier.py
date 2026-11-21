"""D-105 tracer: identity provisions the contracts notifier from a file and the PDP lets only it
(and Global admins) send mail.

The credential file holds ``<public id>:<SHA-256 of the secret>``, so identity verifies the key
and never holds the plaintext. These tests generate a pair, write the file, start identity and
prove the rows, the one audit event, the PDP decision and that nothing sensitive is logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
import structlog
from dc_identity.bootstrap import ensure_notifier, read_notifier_credential

pytestmark = pytest.mark.integration

CHECK = "/internal/v1/authz/check"
NOTIFIER_NAME = "contracts-notifier"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]


@dataclass(frozen=True)
class Pair:
    public_id: str
    secret: str

    @property
    def secret_hash(self) -> str:
        return hashlib.sha256(self.secret.encode()).hexdigest()

    @property
    def line(self) -> str:
        return f"{self.public_id}:{self.secret_hash}"

    @property
    def token(self) -> str:
        return f"dcp_{self.public_id}_{self.secret}"


def new_pair() -> Pair:
    return Pair(public_id=secrets.token_hex(8), secret=secrets.token_urlsafe(32))


def write_line(path: Path, line: str) -> None:
    path.write_text(line + "\n", encoding="utf-8")


def write_pair(path: Path, pair: Pair) -> None:
    write_line(path, pair.line)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _outcome(client: Any, token: str, permission: str = "mail:send") -> str:
    response = await client.post(CHECK, json={"permission": permission}, headers=_bearer(token))
    assert response.status_code == 200, response.text
    return str(response.json()["outcome"])


def _events(sql: Sql, db_url: str) -> list[dict[str, Any]]:
    return sql(
        db_url,
        "SELECT action, actor, before, after FROM outbox "
        "WHERE action = 'system.notifier_provisioned' ORDER BY id",
    )


def _account(sql: Sql, db_url: str) -> list[dict[str, Any]]:
    return sql(
        db_url,
        "SELECT a.id, a.is_active, a.owner_folder_id, f.kind AS owner_kind "
        "FROM service_accounts a JOIN folder f ON f.id = a.owner_folder_id WHERE a.name = %s",
        NOTIFIER_NAME,
    )


def _keys(sql: Sql, db_url: str) -> list[dict[str, Any]]:
    return sql(
        db_url,
        "SELECT id, public_id, secret_hash, kind, name, expires_at, revoked_at "
        "FROM api_credentials ORDER BY created_at, id",
    )


def _grants(sql: Sql, db_url: str) -> list[dict[str, Any]]:
    return sql(
        db_url,
        "SELECT folder_id, principal_type, principal_id, role FROM role_assignments "
        "WHERE role = 'notifier'",
    )


@pytest.fixture
def credential_file(tmp_path: Path) -> Path:
    return tmp_path / "notifier_credential"


async def test_identity_provisions_the_notifier_and_the_pdp_allows_only_it_and_global_admins(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    grant_sql: Callable[..., None],
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    sql: Sql,
    credential_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pair = new_pair()
    write_pair(credential_file, pair)
    env = make_identity(notifier_credential_file=credential_file)

    async with asgi_client(env.app) as client:
        root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
        accounts = _account(sql, env.db_url)
        assert len(accounts) == 1
        assert accounts[0]["owner_kind"] == "root"
        assert accounts[0]["owner_folder_id"] == root
        assert accounts[0]["is_active"] is True

        keys = _keys(sql, env.db_url)
        assert len(keys) == 1
        assert keys[0]["public_id"] == pair.public_id
        assert keys[0]["secret_hash"] == pair.secret_hash
        assert keys[0]["kind"] == "service_account_key"
        assert keys[0]["revoked_at"] is None

        assert [
            (g["folder_id"], g["principal_type"], g["principal_id"], g["role"])
            for g in _grants(sql, env.db_url)
        ] == [(root, "service_account", f"sa:{accounts[0]['id']}", "notifier")]

        events = _events(sql, env.db_url)
        assert len(events) == 1
        assert events[0]["actor"]["id"] == "system:bootstrap"
        assert events[0]["after"]["role"] == "notifier"
        assert events[0]["after"]["service_account_id"] == str(accounts[0]["id"])
        assert events[0]["after"]["credential_id"] == str(keys[0]["id"])
        assert events[0]["after"]["revoked_credential_ids"] == []
        assert events[0]["after"]["renewed_credential_id"] is None
        payload = json.dumps(events[0], default=str)
        for sensitive in (pair.public_id, pair.secret_hash, pair.secret):
            assert sensitive not in payload

        # The PDP: the notifier key may send mail at ROOT.
        assert await _outcome(client, pair.token) == "allow"
        # ... and may not do anything else.
        assert await _outcome(client, pair.token, "audit:read") == "forbidden"
        assert await _outcome(client, pair.token, "user:create") == "forbidden"

        admin = await admin_token(client, env)
        assert await _outcome(client, admin) == "allow"

        org, _project, team = await folder_chain(client, admin, root, "mail")
        consumer = await create_user(client, admin, "carol")
        team_admin = await create_user(client, admin, "tom")
        org_admin = await create_user(client, admin, "olga")
        grant_sql(env.db_url, consumer.principal_id, "consumer", team)
        grant_sql(env.db_url, team_admin.principal_id, "team_admin", team)
        grant_sql(env.db_url, org_admin.principal_id, "org_admin", org)
        for user in (consumer, team_admin, org_admin):
            token = await login(client, user.username, user.password)
            assert await _outcome(client, token) == "forbidden", user.username

    captured = capsys.readouterr()
    logs = captured.out + captured.err
    for sensitive in (pair.public_id, pair.secret_hash, pair.secret):
        assert sensitive not in logs


async def test_a_restart_that_changes_nothing_writes_no_rows_and_no_event(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    sql: Sql,
    credential_file: Path,
) -> None:
    pair = new_pair()
    write_pair(credential_file, pair)
    env = make_identity(notifier_credential_file=credential_file)

    async with asgi_client(env.app) as client:
        del client
        before = (_keys(sql, env.db_url), _grants(sql, env.db_url), _account(sql, env.db_url))
        await ensure_notifier(env.app.state.session_factory, env.settings)
        await ensure_notifier(env.app.state.session_factory, env.settings)
        after = (_keys(sql, env.db_url), _grants(sql, env.db_url), _account(sql, env.db_url))

    assert before == after
    assert len(_events(sql, env.db_url)) == 1


async def test_an_absent_credential_file_never_stops_identity_and_provisions_nothing(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    sql: Sql,
    credential_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = make_identity(notifier_credential_file=credential_file)  # the file is never written
    async with asgi_client(env.app) as client:
        assert (await client.get("/readyz")).status_code == 200

    assert _account(sql, env.db_url) == []
    assert _events(sql, env.db_url) == []
    logs = capsys.readouterr()
    text = logs.out + logs.err
    assert "notifier_credential_unavailable" in text
    assert "missing" in text


async def test_a_malformed_credential_file_provisions_nothing_and_logs_only_the_reason(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    sql: Sql,
    credential_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_line(credential_file, "nothex:short")
    env = make_identity(notifier_credential_file=credential_file)
    async with asgi_client(env.app) as client:
        assert (await client.get("/readyz")).status_code == 200

    assert _account(sql, env.db_url) == []
    assert _keys(sql, env.db_url) == []
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "notifier_credential_unavailable" in text
    assert "malformed" in text
    assert "nothex" not in text
    assert "short" not in text


@pytest.mark.parametrize(
    "content",
    [
        "",
        "no-separator",
        "0123456789abcdef",
        "0123456789ABCDEF:" + "a" * 64,  # upper case public id
        "0123456789abcdef:" + "A" * 64,  # upper case hash
        "0123456789abcdef:" + "a" * 63,
        "0123456789abcde:" + "a" * 64,
        "0123456789abcdef:" + "a" * 64 + ":extra",
    ],
)
def test_read_notifier_credential_rejects_every_malformed_shape(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "c"
    path.write_text(content, encoding="utf-8")
    assert read_notifier_credential(path) == (None, "malformed")


def test_read_notifier_credential_reports_missing_and_unreadable(tmp_path: Path) -> None:
    assert read_notifier_credential(tmp_path / "absent") == (None, "missing")
    path = tmp_path / "binary"
    path.write_bytes(b"\xff\xfe\x00bad")
    assert read_notifier_credential(path) == (None, "unreadable")
    good = new_pair()
    path.write_text(good.line + "\n", encoding="utf-8")
    assert read_notifier_credential(path) == ((good.public_id, good.secret_hash), None)


async def test_the_role_check_accepts_notifier_and_still_rejects_an_unknown_role(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    grant_sql: Callable[..., None],
    sql: Sql,
    credential_file: Path,
) -> None:
    env = make_identity(notifier_credential_file=credential_file)
    async with asgi_client(env.app):
        root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
    grant_sql(env.db_url, "local:probe", "notifier", root)
    with pytest.raises(psycopg.errors.CheckViolation):
        grant_sql(env.db_url, "local:probe", "not-a-role", root)


# --- Task 2: rotation, conflicts, renewal and the revoked key -----------------------------------

IDENTITY = "/api/v1/identity"
SERVICE_ACCOUNTS = f"{IDENTITY}/service-accounts"


def _all_logs(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def _key(sql: Sql, db_url: str, public_id: str) -> dict[str, Any]:
    rows = sql(
        db_url,
        "SELECT id, public_id, expires_at, revoked_at, created_at, "
        "expires_at > now() + interval '364 days' AS far_ahead "
        "FROM api_credentials WHERE public_id = %s",
        public_id,
    )
    assert len(rows) == 1
    return rows[0]


async def test_rotating_the_file_inserts_the_new_key_and_revokes_the_old_one(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    sql: Sql,
    credential_file: Path,
) -> None:
    first, second = new_pair(), new_pair()
    write_pair(credential_file, first)
    env = make_identity(notifier_credential_file=credential_file)

    async with asgi_client(env.app) as client:
        first_id = _key(sql, env.db_url, first.public_id)["id"]
        assert await _outcome(client, first.token) == "allow"

        write_pair(credential_file, second)
        await ensure_notifier(env.app.state.session_factory, env.settings)

        # The new key resolves; the old one is refused by the PDP (401).
        assert await _outcome(client, second.token) == "allow"
        refused = await client.post(
            CHECK, json={"permission": "mail:send"}, headers=_bearer(first.token)
        )
        assert refused.status_code == 401

        old = _key(sql, env.db_url, first.public_id)
        assert old["revoked_at"] is not None
        assert _key(sql, env.db_url, second.public_id)["revoked_at"] is None
        events = _events(sql, env.db_url)
        assert len(events) == 2
        assert events[1]["after"]["revoked_credential_ids"] == [str(first_id)]
        assert events[1]["after"]["renewed_credential_id"] is None
        payload = json.dumps(events, default=str)
        for sensitive in (first.public_id, second.public_id, first.secret_hash, second.secret_hash):
            assert sensitive not in payload

        # A third run with the same file changes nothing.
        await ensure_notifier(env.app.state.session_factory, env.settings)
        assert len(_events(sql, env.db_url)) == 2
        assert len(_keys(sql, env.db_url)) == 2
        assert len(_grants(sql, env.db_url)) == 1
        assert len(_account(sql, env.db_url)) == 1


async def test_a_public_id_owned_by_another_credential_is_refused_and_changes_nothing(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
    sql: Sql,
    credential_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = new_pair()
    write_pair(credential_file, first)
    env = make_identity(notifier_credential_file=credential_file)

    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        pat = await create_pat(client, admin)
        pat_public_id = str(pat["token"]).split("_")[1]
        write_line(credential_file, f"{pat_public_id}:{'b' * 64}")
        before = _keys(sql, env.db_url)
        events_before = len(_events(sql, env.db_url))

        await ensure_notifier(env.app.state.session_factory, env.settings)

        assert _keys(sql, env.db_url) == before
        assert len(_events(sql, env.db_url)) == events_before
        assert _key(sql, env.db_url, pat_public_id)["revoked_at"] is None
        assert _key(sql, env.db_url, first.public_id)["revoked_at"] is None
    logs = _all_logs(capsys)
    assert "notifier_credential_conflict" in logs
    assert pat_public_id not in logs
    assert "b" * 64 not in logs


async def test_a_deactivated_notifier_account_is_never_reactivated_and_gets_no_key(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    credential_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, second = new_pair(), new_pair()
    write_pair(credential_file, first)
    env = make_identity(notifier_credential_file=credential_file)

    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        account_id = _account(sql, env.db_url)[0]["id"]
        resp = await client.post(
            f"{SERVICE_ACCOUNTS}/{account_id}/deactivate", headers=_bearer(admin)
        )
        assert resp.status_code == 200, resp.text
        write_pair(credential_file, second)
        keys_before = _keys(sql, env.db_url)
        events_before = len(_events(sql, env.db_url))

        await ensure_notifier(env.app.state.session_factory, env.settings)

        assert _account(sql, env.db_url)[0]["is_active"] is False
        assert _keys(sql, env.db_url) == keys_before
        assert len(_events(sql, env.db_url)) == events_before
    assert "notifier_account_inactive" in _all_logs(capsys)


async def test_only_a_global_admin_grants_notifier_and_only_at_root(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    grant: Callable[..., Awaitable[Any]],
    grant_sql: Callable[..., None],
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    sql: Sql,
    credential_file: Path,
) -> None:
    env = make_identity(notifier_credential_file=credential_file)
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        org, _project, team = await folder_chain(client, admin, root, "ng")
        olga = await create_user(client, admin, "olga")
        target = await create_user(client, admin, "target")
        grant_sql(env.db_url, olga.principal_id, "org_admin", org)
        olga_token = await login(client, olga.username, olga.password)

        # An Org admin cannot grant it anywhere it could see; the role is bound to ROOT first.
        at_org = await grant(client, olga_token, org, target.principal_id, "notifier")
        assert at_org.status_code in (403, 422), at_org.text
        at_root = await grant(client, olga_token, root, target.principal_id, "notifier")
        assert at_root.status_code in (403, 404), at_root.text

        # A Global admin is refused below ROOT by the level binding ...
        at_team = await grant(client, admin, team, target.principal_id, "notifier")
        assert at_team.status_code == 422, at_team.text
        assert str(at_team.json()["type"]).endswith("role-level-mismatch")
        # ... and may grant it at ROOT.
        at_root_admin = await grant(client, admin, root, target.principal_id, "notifier")
        assert at_root_admin.status_code == 201, at_root_admin.text
    assert _grants(sql, env.db_url)[0]["principal_id"] == target.principal_id


async def test_an_expired_notifier_key_is_renewed_in_place_and_authenticates_again(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    sql: Sql,
    credential_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pair = new_pair()
    write_pair(credential_file, pair)
    env = make_identity(notifier_credential_file=credential_file)

    async with asgi_client(env.app) as client:
        key = _key(sql, env.db_url, pair.public_id)
        sql(
            env.db_url,
            "UPDATE api_credentials SET created_at = now() - interval '400 days', "
            "expires_at = now() - interval '1 day' WHERE public_id = %s",
            pair.public_id,
        )
        refused = await client.post(
            CHECK, json={"permission": "mail:send"}, headers=_bearer(pair.token)
        )
        assert refused.status_code == 401

        await ensure_notifier(env.app.state.session_factory, env.settings)

        renewed = _key(sql, env.db_url, pair.public_id)
        assert renewed["id"] == key["id"]
        assert renewed["far_ahead"] is True
        assert renewed["revoked_at"] is None
        assert await _outcome(client, pair.token) == "allow"
        events = _events(sql, env.db_url)
        assert len(events) == 2
        assert events[1]["after"]["renewed_credential_id"] == str(key["id"])
        assert events[1]["after"]["revoked_credential_ids"] == []
        payload = json.dumps(events[1], default=str)
        for sensitive in (pair.public_id, pair.secret_hash):
            assert sensitive not in payload

        # A second run changes nothing.
        await ensure_notifier(env.app.state.session_factory, env.settings)
        assert len(_events(sql, env.db_url)) == 2
        assert _key(sql, env.db_url, pair.public_id)["expires_at"] == renewed["expires_at"]

    logs = _all_logs(capsys)
    assert "notifier_credential_renewed" in logs
    assert "expired" in logs
    assert pair.public_id not in logs
    assert pair.secret_hash not in logs


async def test_a_key_inside_the_warning_window_is_renewed_and_a_healthy_one_is_not(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    sql: Sql,
    credential_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pair = new_pair()
    write_pair(credential_file, pair)
    env = make_identity(notifier_credential_file=credential_file)

    async with asgi_client(env.app) as client:
        del client
        # Healthy: 100 days left is outside the 14 day window, so nothing happens.
        sql(
            env.db_url,
            "UPDATE api_credentials SET expires_at = now() + interval '100 days' "
            "WHERE public_id = %s",
            pair.public_id,
        )
        healthy = _key(sql, env.db_url, pair.public_id)
        capsys.readouterr()
        await ensure_notifier(env.app.state.session_factory, env.settings)
        assert _key(sql, env.db_url, pair.public_id)["expires_at"] == healthy["expires_at"]
        assert len(_events(sql, env.db_url)) == 1
        assert "notifier_credential_renewed" not in _all_logs(capsys)

        # Expiring: 3 days left is inside the window.
        sql(
            env.db_url,
            "UPDATE api_credentials SET expires_at = now() + interval '3 days' "
            "WHERE public_id = %s",
            pair.public_id,
        )
        await ensure_notifier(env.app.state.session_factory, env.settings)
        assert _key(sql, env.db_url, pair.public_id)["far_ahead"] is True
        events = _events(sql, env.db_url)
        assert len(events) == 2
        assert events[1]["after"]["renewed_credential_id"] == str(healthy["id"])
    logs = _all_logs(capsys)
    assert "notifier_credential_renewed" in logs
    assert "expiring" in logs


async def test_a_revoked_notifier_key_is_never_revived_until_a_new_pair_is_written(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    credential_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, second = new_pair(), new_pair()
    write_pair(credential_file, first)
    env = make_identity(notifier_credential_file=credential_file)

    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        account_id = _account(sql, env.db_url)[0]["id"]
        key = _key(sql, env.db_url, first.public_id)
        revoked = await client.delete(
            f"{SERVICE_ACCOUNTS}/{account_id}/keys/{key['id']}", headers=_bearer(admin)
        )
        assert revoked.status_code == 204, revoked.text
        before = _key(sql, env.db_url, first.public_id)
        rows_before = _keys(sql, env.db_url)
        events_before = len(_events(sql, env.db_url))
        capsys.readouterr()

        await ensure_notifier(env.app.state.session_factory, env.settings)

        after = _key(sql, env.db_url, first.public_id)
        assert after["revoked_at"] is not None
        assert after["revoked_at"] == before["revoked_at"]
        assert after["expires_at"] == before["expires_at"]
        assert _keys(sql, env.db_url) == rows_before
        assert len(_events(sql, env.db_url)) == events_before
        assert "notifier_credential_revoked" in _all_logs(capsys)

        # The recovery path: a new pair and a restart.
        write_pair(credential_file, second)
        await ensure_notifier(env.app.state.session_factory, env.settings)
        assert await _outcome(client, second.token) == "allow"
        assert len(_events(sql, env.db_url)) == events_before + 1


async def test_a_running_identity_renews_the_key_without_a_restart(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    sql: Sql,
    credential_file: Path,
) -> None:
    # WR-05: a process that never restarts must not let the notifier key lapse.
    pair = new_pair()
    write_pair(credential_file, pair)
    env = make_identity(notifier_credential_file=credential_file, notifier_renewal_interval_s=0.2)

    async with asgi_client(env.app) as client:
        del client
        sql(
            env.db_url,
            "UPDATE api_credentials SET expires_at = now() + interval '3 days' "
            "WHERE public_id = %s",
            pair.public_id,
        )
        for _ in range(100):
            if _key(sql, env.db_url, pair.public_id)["far_ahead"] is True:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("the running identity never renewed the notifier key")
        assert len(_events(sql, env.db_url)) == 2


async def test_without_a_root_folder_the_notifier_is_not_provisioned(
    make_identity: Callable[..., Any], sql: Sql, credential_file: Path
) -> None:
    """``ensure_bootstrap`` creates ROOT first; alone on an empty database this changes nothing."""
    write_pair(credential_file, new_pair())
    env = make_identity(notifier_credential_file=credential_file)  # lifespan never runs
    factory = env.app.state.session_factory
    try:
        with structlog.testing.capture_logs() as logs:
            await ensure_notifier(factory, env.settings)
    finally:
        await factory.kw["bind"].dispose()
    assert [(e["event"], e.get("reason")) for e in logs] == [
        ("notifier_credential_unavailable", "no_root")
    ]
    assert _account(sql, env.db_url) == []
    assert _keys(sql, env.db_url) == []
    assert _events(sql, env.db_url) == []
