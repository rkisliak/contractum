"""AUTH-09 tracer: bootstrap admin signs in with the one-time secret, rotates it, logs in again."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import jwt
import psycopg
import pytest
from argon2 import PasswordHasher
from dc_core.migrations import run_alembic
from dc_identity.bootstrap import ensure_bootstrap
from dc_identity.migrate import SCRIPT_LOCATION, VERSION_TABLE
from psycopg.rows import dict_row

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/identity/auth/login"
PASSWORD = "/api/v1/identity/auth/password"
NEW_PASSWORD = "correct horse battery"  # 21 characters  # noqa: S105


def _sql(db_url: str, query: str, *params: object) -> list[dict[str, Any]]:
    url = db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        cur = conn.execute(query, params)  # type: ignore[arg-type]
        return cur.fetchall() if cur.description else []


def _outbox(db_url: str) -> list[dict[str, Any]]:
    return _sql(
        db_url, "SELECT action, actor, before, after, aggregate_type FROM outbox ORDER BY id"
    )


async def _login(client: Any, username: str, password: str) -> Any:
    return await client.post(LOGIN, json={"username": username, "password": password})


async def _bootstrap_login(client: Any, secret: str) -> str:
    resp = await _login(client, "admin", secret)
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["access_token"]
    return token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_fresh_database_is_seeded_once_as_system_bootstrap(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        assert (await client.get("/readyz")).status_code == 200

    root = _sql(env.db_url, "SELECT id, kind, slug, path::text AS path, created_by FROM folder")
    assert [(r["kind"], r["slug"], r["path"], r["created_by"]) for r in root] == [
        ("root", "root", "root", "system:bootstrap")
    ]
    admin = _sql(env.db_url, "SELECT * FROM users")
    assert len(admin) == 1
    assert admin[0]["username"] == "admin"
    assert admin[0]["password_hash"] is None
    assert admin[0]["must_rotate"] is True
    grants = _sql(env.db_url, "SELECT * FROM role_assignments")
    assert [
        (g["principal_type"], g["principal_id"], g["role"], g["folder_id"]) for g in grants
    ] == [("user", f"local:{admin[0]['id']}", "global_admin", root[0]["id"])]
    state = _sql(env.db_url, "SELECT * FROM bootstrap_state")
    assert [(s["id"], s["consumed_at"]) for s in state] == [(1, None)]

    events = _outbox(env.db_url)
    assert [e["action"] for e in events] == ["system.bootstrapped"]
    assert events[0]["actor"]["id"] == "system:bootstrap"
    assert events[0]["actor"]["type"] == "system"
    assert events[0]["before"] is None
    assert events[0]["after"] == {
        "root_folder_id": str(root[0]["id"]),
        "admin_user_id": str(admin[0]["id"]),
        "bootstrap_enabled": True,
    }


async def test_bootstrap_secret_login_returns_restricted_token_without_refresh_token(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        resp = await _login(client, "admin", env.secret)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "scope"}
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 600
    assert body["scope"] == "password:rotate"
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert claims["scope"] == "password:rotate"
    assert claims["exp"] - claims["iat"] == 600
    assert claims["iss"] == env.settings.local_issuer
    assert claims["aud"] == "contractum"
    assert claims["ptype"] == "user"
    assert set(claims) == {
        "iss",
        "sub",
        "aud",
        "iat",
        "nbf",
        "exp",
        "jti",
        "ptype",
        "scope",
        "name",
    }
    assert jwt.get_unverified_header(body["access_token"])["alg"] == "RS256"


async def test_rotation_sets_argon2id_hash_and_writes_exactly_one_audit_event(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await _bootstrap_login(client, env.secret)
        resp = await client.post(
            PASSWORD, json={"new_password": NEW_PASSWORD}, headers=_bearer(token)
        )
    assert resp.status_code == 204
    assert resp.content == b""

    user = _sql(env.db_url, "SELECT * FROM users")[0]
    assert user["password_hash"].startswith("$argon2id$")
    assert user["must_rotate"] is False
    state = _sql(env.db_url, "SELECT * FROM bootstrap_state")[0]
    assert state["consumed_at"] is not None
    assert state["consumed_by"] == f"local:{user['id']}"

    events = _outbox(env.db_url)
    assert [e["action"] for e in events] == ["system.bootstrapped", "user.password_rotated"]
    rotated = events[1]
    assert rotated["aggregate_type"] == "user"
    assert rotated["before"] == {"must_rotate": True, "password_set": False, "active_sessions": 0}
    assert rotated["after"] == {"must_rotate": False, "password_set": True, "active_sessions": 0}
    assert rotated["actor"]["id"] == f"local:{user['id']}"


async def test_after_rotation_secret_is_dead_and_new_password_gets_a_full_token(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await _bootstrap_login(client, env.secret)
        await client.post(PASSWORD, json={"new_password": NEW_PASSWORD}, headers=_bearer(token))

        again = await _login(client, "admin", env.secret)
        assert again.status_code == 401
        assert again.json()["type"] == "urn:dc:problem:invalid-credentials"

        ok = await _login(client, "admin", NEW_PASSWORD)
        assert ok.status_code == 200
        body = ok.json()
        assert body["scope"] == "api"
        assert body["expires_in"] == 900
        principal = await env.app.state.verifier.verify(body["access_token"])
    user_id = _sql(env.db_url, "SELECT id FROM users")[0]["id"]
    assert principal.id == f"local:{user_id}"
    assert principal.restricted is False


async def test_restart_with_the_same_secret_file_changes_nothing(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await _bootstrap_login(client, env.secret)
        await client.post(PASSWORD, json={"new_password": NEW_PASSWORD}, headers=_bearer(token))
    before = (_outbox(env.db_url), _sql(env.db_url, "SELECT * FROM users"))

    restarted = env.rebuild()
    async with asgi_client(restarted) as client:
        await ensure_bootstrap(restarted.state.session_factory, env.settings)  # a second call
        assert (await _login(client, "admin", env.secret)).status_code == 401
        assert (await _login(client, "admin", NEW_PASSWORD)).status_code == 200

    assert (_outbox(env.db_url), _sql(env.db_url, "SELECT * FROM users")) == before
    for table in ("folder", "role_assignments", "bootstrap_state"):
        assert len(_sql(env.db_url, f"SELECT 1 FROM {table}")) == 1  # noqa: S608


@pytest.mark.parametrize(
    ("secret_kind", "reason"), [("missing", "missing"), ("short", "too_short")]
)
async def test_unavailable_secret_starts_identity_and_login_answers_503(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    capsys: pytest.CaptureFixture[str],
    secret_kind: str,
    reason: str,
) -> None:
    short = "s3cret-that-is-only-20"[:20]
    env = make_identity(secret=None if secret_kind == "missing" else short)
    async with asgi_client(env.app) as client:
        assert (await client.get("/readyz")).status_code == 200
        resp = await _login(client, "admin", short)
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["type"] == "urn:dc:problem:bootstrap-unavailable"
    assert short not in resp.text
    captured = capsys.readouterr()
    logs = captured.out + captured.err
    assert "bootstrap_secret_unavailable" in logs
    assert reason in logs
    assert short not in logs
    events = _outbox(env.db_url)
    assert events[0]["after"]["bootstrap_enabled"] is False


async def test_unknown_user_and_wrong_password_get_the_same_401_and_burn_an_argon2_verify(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    calls = 0
    original = env.app.state.passwords.dummy_verify

    async def counted() -> None:
        nonlocal calls
        calls += 1
        await original()

    env.app.state.passwords.dummy_verify = counted
    async with asgi_client(env.app) as client:
        token = await _bootstrap_login(client, env.secret)
        await client.post(PASSWORD, json={"new_password": NEW_PASSWORD}, headers=_bearer(token))
        unknown = await _login(client, "nobody", NEW_PASSWORD)
        wrong = await _login(client, "admin", "not the right password")
        assert calls == 1  # the unknown-user path ran dummy_verify
        assert unknown.status_code == wrong.status_code == 401

    def strip(resp: Any) -> dict[str, Any]:
        body = dict(resp.json())
        body.pop("request_id", None)
        return body

    assert strip(unknown) == strip(wrong)
    assert unknown.json()["type"] == "urn:dc:problem:invalid-credentials"
    assert unknown.headers["content-type"].startswith("application/problem+json")


async def test_rotation_rejects_short_username_and_secret_passwords_then_rejects_reuse(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity(bootstrap_admin_username="bootstrap-admin")
    async with asgi_client(env.app) as client:
        resp = await _login(client, "bootstrap-admin", env.secret)
        token = resp.json()["access_token"]
        for bad in ("elevenchars", "Bootstrap-Admin", env.secret):
            rejected = await client.post(
                PASSWORD, json={"new_password": bad}, headers=_bearer(token)
            )
            assert rejected.status_code == 422, bad
            body = rejected.json()
            assert body["type"] == "urn:dc:problem:password-policy"
            assert bad not in rejected.text
        # Nothing committed by the rejected attempts: bootstrap is still unconsumed.
        assert _sql(env.db_url, "SELECT consumed_at FROM bootstrap_state")[0]["consumed_at"] is None

        ok = await client.post(
            PASSWORD, json={"new_password": NEW_PASSWORD}, headers=_bearer(token)
        )
        assert ok.status_code == 204
        # The restricted token is still cryptographically valid; the DB state refuses it.
        replay = await client.post(
            PASSWORD, json={"new_password": "another good password"}, headers=_bearer(token)
        )
    assert replay.status_code == 409
    assert replay.json()["type"] == "urn:dc:problem:bootstrap-consumed"


async def test_full_session_rotation_requires_the_current_password(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await _bootstrap_login(client, env.secret)
        await client.post(PASSWORD, json={"new_password": NEW_PASSWORD}, headers=_bearer(token))
        full = (await _login(client, "admin", NEW_PASSWORD)).json()["access_token"]
        missing = await client.post(
            PASSWORD, json={"new_password": "yet another password"}, headers=_bearer(full)
        )
        wrong = await client.post(
            PASSWORD,
            json={"new_password": "yet another password", "current_password": "nope nope nope"},
            headers=_bearer(full),
        )
        ok = await client.post(
            PASSWORD,
            json={"new_password": "yet another password", "current_password": NEW_PASSWORD},
            headers=_bearer(full),
        )
        relogin = await _login(client, "admin", "yet another password")
    assert missing.status_code == wrong.status_code == 401
    assert ok.status_code == 204
    assert relogin.status_code == 200
    events = [e["action"] for e in _outbox(env.db_url)]
    assert events == ["system.bootstrapped", "user.password_rotated", "user.password_rotated"]


async def test_secrets_and_passwords_never_reach_logs_audit_rows_or_problem_bodies(
    make_identity: Callable[..., Any], asgi_client: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    env = make_identity()
    bodies: list[str] = []
    async with asgi_client(env.app) as client:
        token = await _bootstrap_login(client, env.secret)
        bodies.append((await _login(client, "admin", "wrong wrong wrong")).text)
        bodies.append(
            (
                await client.post(PASSWORD, json={"new_password": "short"}, headers=_bearer(token))
            ).text
        )
        await client.post(PASSWORD, json={"new_password": NEW_PASSWORD}, headers=_bearer(token))
        bodies.append((await _login(client, "admin", env.secret)).text)
    captured = capsys.readouterr()
    haystack = json.dumps(_outbox(env.db_url), default=str) + captured.out + captured.err
    haystack += "".join(bodies)
    hashes = [r["password_hash"] for r in _sql(env.db_url, "SELECT password_hash FROM users")]
    for needle in (env.secret, NEW_PASSWORD, "wrong wrong wrong", hashes[0], token):
        assert needle not in haystack


async def test_rehash_on_login_upgrades_the_hash_and_is_audited(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(NEW_PASSWORD)
    async with asgi_client(env.app) as client:
        token = await _bootstrap_login(client, env.secret)
        await client.post(
            PASSWORD, json={"new_password": "temporary password"}, headers=_bearer(token)
        )
        _sql(env.db_url, "UPDATE users SET password_hash = %s", weak)
        resp = await _login(client, "admin", NEW_PASSWORD)
    assert resp.status_code == 200
    upgraded = _sql(env.db_url, "SELECT password_hash FROM users")[0]["password_hash"]
    assert upgraded != weak
    assert "m=65536" in upgraded
    assert [e["action"] for e in _outbox(env.db_url)][-1] == "user.password_rehashed"


def test_migration_matches_the_models_and_downgrades_cleanly(
    make_identity: Callable[..., Any],
) -> None:
    env = make_identity()
    kwargs = {
        "script_location": SCRIPT_LOCATION,
        "version_table": VERSION_TABLE,
        "url": env.db_url,
    }
    run_alembic(**kwargs, command="check")  # type: ignore[arg-type]
    run_alembic(**kwargs, command="downgrade", revision="base")  # type: ignore[arg-type]
    tables = _sql(
        env.db_url,
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
    )
    assert [t["table_name"] for t in tables] == [VERSION_TABLE]


async def test_full_scope_rotation_does_not_consume_the_bootstrap_state(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    """WR-06: only the restricted bootstrap-secret rotation flips ``bootstrap_state``."""
    env = make_identity()
    hashed = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(NEW_PASSWORD)
    async with asgi_client(env.app) as client:  # the lifespan seeds the admin
        _sql(env.db_url, "UPDATE users SET password_hash = %s", hashed)  # a full login, unconsumed
        full = (await _login(client, "admin", NEW_PASSWORD)).json()["access_token"]
        rotated = await client.post(
            PASSWORD,
            json={"new_password": "yet another password", "current_password": NEW_PASSWORD},
            headers=_bearer(full),
        )
    assert rotated.status_code == 204
    state = _sql(env.db_url, "SELECT consumed_at, consumed_by FROM bootstrap_state")[0]
    assert state["consumed_at"] is None
    assert state["consumed_by"] is None


async def test_wrong_current_passwords_are_throttled_and_locked_before_hashing(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    """WR-06: brute-forcing ``current_password`` with a stolen token hits the login backoff."""
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await _bootstrap_login(client, env.secret)
        await client.post(PASSWORD, json={"new_password": NEW_PASSWORD}, headers=_bearer(token))
        full = (await _login(client, "admin", NEW_PASSWORD)).json()["access_token"]
        body = {"new_password": "yet another password", "current_password": "nope nope nope"}
        for _ in range(5):
            wrong = await client.post(PASSWORD, json=body, headers=_bearer(full))
            assert wrong.status_code == 401
        locked = await client.post(PASSWORD, json=body, headers=_bearer(full))
        assert locked.status_code == 429
        assert locked.json()["type"] == "urn:dc:problem:too-many-attempts"
        assert int(locked.headers["retry-after"]) >= 1
        # Even the correct current password is refused while the key is locked.
        right = await client.post(
            PASSWORD,
            json={"new_password": "yet another password", "current_password": NEW_PASSWORD},
            headers=_bearer(full),
        )
        assert right.status_code == 429
