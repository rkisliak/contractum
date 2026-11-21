"""Active sessions: list, sign out and revoke (quick 261120-s0p, ASVS 7.4.5 and 7.5.2).

A local user lists their own live sessions (a refresh-token family each), signs one out, or signs
out every other session; a Global admin does the same for a local user. The current session is
found through the access token's jti, which login and refresh store on the family's row; the
raw User-Agent is never stored, only a fixed-vocabulary summary.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
ME_SESSIONS = f"{IDENTITY}/me/sessions"
LOGIN = f"{IDENTITY}/auth/login"
REFRESH = f"{IDENTITY}/auth/refresh"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]

CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _slug(resp: Any) -> str:
    return str(resp.json()["type"]).rsplit(":", 1)[-1]


async def _sign_in(client: Any, username: str, password: str, agent: str | None) -> dict[str, Any]:
    headers = {"User-Agent": agent} if agent is not None else {}
    resp = await client.post(
        LOGIN, json={"username": username, "password": password}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _refresh(client: Any, refresh_token: str) -> dict[str, Any]:
    resp = await client.post(REFRESH, json={"refresh_token": refresh_token})
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


class Stack:
    def __init__(self, env: Any, client: Any, admin: str, ana: Any) -> None:
        self.env, self.client, self.admin, self.ana = env, client, admin, ana


@pytest.fixture
def stack(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
) -> Callable[..., Any]:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _open(**overrides: Any) -> Any:
        env = make_identity(**overrides)
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            ana = await create_user(client, admin, "ana", display_name="Ana")
            yield Stack(env, client, admin, ana)

    return _open


# --- storage ----------------------------------------------------------------------------------


async def test_login_stores_the_summary_and_the_access_jti_never_the_raw_agent(
    stack: Callable[..., Any], sql: Sql
) -> None:
    async with stack() as s:
        body = await _sign_in(s.client, "ana", s.ana.password, CHROME_MAC)
        (row,) = sql(
            s.env.db_url,
            "SELECT client_summary, access_jti FROM refresh_tokens WHERE user_id = %s",
            s.ana.id,
        )
        assert row["client_summary"] == "Chrome on macOS"
        import jwt

        jti = jwt.decode(body["access_token"], options={"verify_signature": False})["jti"]
        assert row["access_jti"] == jti
        dump = str(sql(s.env.db_url, "SELECT * FROM refresh_tokens"))
        assert "Mozilla" not in dump and "AppleWebKit" not in dump


async def test_refresh_copies_the_summary_and_stores_the_new_access_jti(
    stack: Callable[..., Any], sql: Sql
) -> None:
    import jwt

    async with stack() as s:
        first = await _sign_in(s.client, "ana", s.ana.password, FIREFOX_LINUX)
        second = await _refresh(s.client, first["refresh_token"])
        rows = sql(
            s.env.db_url,
            "SELECT client_summary, access_jti, rotated_at FROM refresh_tokens "
            "WHERE user_id = %s ORDER BY created_at",
            s.ana.id,
        )
        assert [r["client_summary"] for r in rows] == ["Firefox on Linux"] * 2
        jti = jwt.decode(second["access_token"], options={"verify_signature": False})["jti"]
        assert rows[1]["access_jti"] == jti
        assert rows[1]["rotated_at"] is None


# --- the list ---------------------------------------------------------------------------------


async def test_the_list_has_one_item_per_family_sorted_by_last_activity_with_the_current_marked(
    stack: Callable[..., Any],
) -> None:
    async with stack() as s:
        a = await _sign_in(s.client, "ana", s.ana.password, CHROME_MAC)
        b = await _sign_in(s.client, "ana", s.ana.password, FIREFOX_LINUX)
        resp = await s.client.get(ME_SESSIONS, headers=_bearer(a["access_token"]))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        assert body["max_sessions"] == 5
        assert body["access_token_lifetime_s"] == s.env.settings.access_token_ttl_s
        items = body["items"]
        assert len(items) == 2
        for item in items:
            assert set(item) == {
                "id",
                "started_at",
                "last_active_at",
                "idle_expires_at",
                "expires_at",
                "client",
                "current",
            }
        # newest activity first: the Firefox sign-in came second
        assert [i["client"] for i in items] == ["Firefox on Linux", "Chrome on macOS"]
        assert [i["current"] for i in items] == [False, True]
        # the other token marks the other family
        resp_b = await s.client.get(ME_SESSIONS, headers=_bearer(b["access_token"]))
        assert [i["current"] for i in resp_b.json()["items"]] == [True, False]


async def test_an_access_token_issued_before_the_latest_refresh_is_still_current(
    stack: Callable[..., Any],
) -> None:
    async with stack() as s:
        first = await _sign_in(s.client, "ana", s.ana.password, CHROME_MAC)
        await _refresh(s.client, first["refresh_token"])
        resp = await s.client.get(ME_SESSIONS, headers=_bearer(first["access_token"]))
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["current"] is True


async def test_ended_families_are_absent_and_other_users_are_never_listed(
    stack: Callable[..., Any], create_user: Callable[..., Awaitable[Any]], sql: Sql
) -> None:
    async with stack() as s:
        live = await _sign_in(s.client, "ana", s.ana.password, CHROME_MAC)
        revoked = await _sign_in(s.client, "ana", s.ana.password, FIREFOX_LINUX)
        idle = await _sign_in(s.client, "ana", s.ana.password, "curl/8.5.0")
        capped = await _sign_in(s.client, "ana", s.ana.password, "python-httpx/0.28")
        rotated_only = await _sign_in(s.client, "ana", s.ana.password, "node")
        await _refresh(s.client, rotated_only["refresh_token"])
        bob = await create_user(s.client, s.admin, "bob", display_name="Bob")
        await _sign_in(s.client, "bob", bob.password, CHROME_MAC)
        sql(
            s.env.db_url,
            "UPDATE refresh_tokens SET revoked_at = now() WHERE access_jti = %s",
            _jti(revoked["access_token"]),
        )
        sql(
            s.env.db_url,
            "UPDATE refresh_tokens SET expires_at = now() - interval '1 minute' "
            "WHERE access_jti = %s",
            _jti(idle["access_token"]),
        )
        sql(
            s.env.db_url,
            "UPDATE refresh_tokens SET session_started_at = now() - interval '30 days' "
            "WHERE access_jti = %s",
            _jti(capped["access_token"]),
        )
        resp = await s.client.get(ME_SESSIONS, headers=_bearer(live["access_token"]))
        clients = sorted(i["client"] for i in resp.json()["items"])
        # live Chrome and the rotated-then-live Node family remain; bob's Chrome is not ours
        assert clients == ["API client (node)", "Chrome on macOS"]
        assert resp.json()["total"] == 2


def _jti(token: str) -> str:
    import jwt

    return str(jwt.decode(token, options={"verify_signature": False})["jti"])


async def test_a_reading_writes_no_event(stack: Callable[..., Any], sql: Sql) -> None:
    async with stack() as s:
        a = await _sign_in(s.client, "ana", s.ana.password, CHROME_MAC)
        before = len(sql(s.env.db_url, "SELECT id FROM outbox"))
        await s.client.get(ME_SESSIONS, headers=_bearer(a["access_token"]))
        assert len(sql(s.env.db_url, "SELECT id FROM outbox")) == before


# --- other principals and deny paths ------------------------------------------------------------


async def test_a_pat_lists_the_owners_sessions_with_none_current(
    stack: Callable[..., Any], create_pat: Callable[..., Awaitable[dict[str, Any]]]
) -> None:
    async with stack() as s:
        a = await _sign_in(s.client, "ana", s.ana.password, CHROME_MAC)
        pat = await create_pat(s.client, a["access_token"])
        resp = await s.client.get(ME_SESSIONS, headers=_bearer(pat["token"]))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        assert [i["current"] for i in body["items"]] == [False]


async def test_an_entra_principal_gets_an_empty_list(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
) -> None:
    env = make_identity()
    oid = str(uuid.uuid4())
    env.directory.add_principal("user", oid, tenant_id=ENTRA_TENANT_ID, groups=())
    sql(
        env.db_url,
        "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
        "display_name, is_active, directly_registered, created_by, updated_by) "
        "VALUES ('user', %s, %s, NULL, 'Entra Eve', true, true, 'test', 'test')",
        ENTRA_TENANT_ID,
        oid,
    )
    async with asgi_client(env.app) as client:
        await admin_token(client, env)
        token = fake_issuer.mint_entra(oid, name="Entra Eve")
        resp = await client.get(ME_SESSIONS, headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


async def test_no_token_is_401_and_a_restricted_bootstrap_token_is_403(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    bootstrap_token: Callable[[Any, Any], Awaitable[str]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        anonymous = await client.get(ME_SESSIONS)
        assert anonymous.status_code == 401, anonymous.text
        restricted = await bootstrap_token(client, env)
        gated = await client.get(ME_SESSIONS, headers=_bearer(restricted))
        assert gated.status_code == 403, gated.text
        assert _slug(gated) == "password-rotation-required"


# --- ending sessions --------------------------------------------------------------------------


def _events(sql: Sql, env: Any, *actions: str) -> list[dict[str, Any]]:
    wanted = actions or ("session.revoked", "user.sessions_revoked")
    return sql(
        env.db_url,
        "SELECT action, aggregate_type, aggregate_id, before, after, scope_path, "
        "actor->>'id' AS actor_id FROM outbox WHERE action = ANY(%s) ORDER BY id",
        list(wanted),
    )


def _family_of(sql: Sql, env: Any, token: str) -> str:
    rows = sql(
        env.db_url, "SELECT family_id FROM refresh_tokens WHERE access_jti = %s", _jti(token)
    )
    return str(rows[0]["family_id"])


async def _two_sessions(s: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    a = await _sign_in(s.client, "ana", s.ana.password, CHROME_MAC)
    b = await _sign_in(s.client, "ana", s.ana.password, FIREFOX_LINUX)
    return a, b


async def test_a_user_signs_one_session_out_with_one_event(
    stack: Callable[..., Any], sql: Sql
) -> None:
    async with stack() as s:
        a, b = await _two_sessions(s)
        family_b = _family_of(sql, s.env, b["access_token"])
        resp = await s.client.delete(
            f"{ME_SESSIONS}/{family_b}", headers=_bearer(a["access_token"])
        )
        assert resp.status_code == 204, resp.text
        events = _events(sql, s.env)
        assert len(events) == 1
        event = events[0]
        assert event["action"] == "session.revoked"
        assert event["aggregate_type"] == "session"
        assert str(event["aggregate_id"]) == family_b
        assert event["before"] == {"status": "active", "user_id": s.ana.id}
        assert event["after"] == {
            "status": "revoked",
            "user_id": s.ana.id,
            "reason": "user_revoked",
        }
        root = sql(s.env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
        assert [str(i) for i in event["scope_path"]] == [str(root)]
        # that family cannot refresh any more, the other still can
        gone = await s.client.post(REFRESH, json={"refresh_token": b["refresh_token"]})
        assert gone.status_code == 401
        still = await s.client.post(REFRESH, json={"refresh_token": a["refresh_token"]})
        assert still.status_code == 200
        # the caller's access token still works until it expires
        again = await s.client.get(ME_SESSIONS, headers=_bearer(a["access_token"]))
        assert again.status_code == 200
        assert again.json()["total"] == 1


async def test_a_session_that_already_ended_is_204_without_an_event_and_unknown_is_404(
    stack: Callable[..., Any],
    create_user: Callable[..., Awaitable[Any]],
    sql: Sql,
) -> None:
    async with stack() as s:
        a, b = await _two_sessions(s)
        family_b = _family_of(sql, s.env, b["access_token"])
        await s.client.delete(f"{ME_SESSIONS}/{family_b}", headers=_bearer(a["access_token"]))
        before = len(_events(sql, s.env))
        ended = await s.client.delete(
            f"{ME_SESSIONS}/{family_b}", headers=_bearer(a["access_token"])
        )
        assert ended.status_code == 204, ended.text
        assert len(_events(sql, s.env)) == before
        unknown = await s.client.delete(
            f"{ME_SESSIONS}/{uuid.uuid4()}", headers=_bearer(a["access_token"])
        )
        assert unknown.status_code == 404, unknown.text
        assert _slug(unknown) == "not-found"
        # another user's family is the same 404
        bob = await create_user(s.client, s.admin, "bob", display_name="Bob")
        bob_in = await _sign_in(s.client, "bob", bob.password, CHROME_MAC)
        family_bob = _family_of(sql, s.env, bob_in["access_token"])
        other = await s.client.delete(
            f"{ME_SESSIONS}/{family_bob}", headers=_bearer(a["access_token"])
        )
        assert other.status_code == 404, other.text
        assert _slug(other) == "not-found"
        # the detail never echoes the id (the problem's `instance` is the request path)
        assert str(family_bob) not in other.json()["detail"]
        assert len(_events(sql, s.env)) == before
        # bob's session is untouched
        bob_list = await s.client.get(ME_SESSIONS, headers=_bearer(bob_in["access_token"]))
        assert bob_list.json()["total"] == 1


async def test_sign_out_needs_an_interactive_session_and_a_token(
    stack: Callable[..., Any],
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
    sql: Sql,
) -> None:
    async with stack() as s:
        a, b = await _two_sessions(s)
        family_b = _family_of(sql, s.env, b["access_token"])
        pat = await create_pat(s.client, a["access_token"])
        by_pat = await s.client.delete(f"{ME_SESSIONS}/{family_b}", headers=_bearer(pat["token"]))
        assert by_pat.status_code == 403, by_pat.text
        assert _slug(by_pat) == "interactive-session-required"
        others_by_pat = await s.client.post(
            f"{ME_SESSIONS}/revoke-others", headers=_bearer(pat["token"])
        )
        assert others_by_pat.status_code == 403, others_by_pat.text
        assert _slug(others_by_pat) == "interactive-session-required"
        anonymous = await s.client.delete(f"{ME_SESSIONS}/{family_b}")
        assert anonymous.status_code == 401
        anonymous_others = await s.client.post(f"{ME_SESSIONS}/revoke-others")
        assert anonymous_others.status_code == 401
        assert _events(sql, s.env) == []
        listing = await s.client.get(ME_SESSIONS, headers=_bearer(a["access_token"]))
        assert listing.json()["total"] == 2


async def test_sign_out_everywhere_else_keeps_only_the_current_session(
    stack: Callable[..., Any], sql: Sql
) -> None:
    async with stack() as s:
        a = await _sign_in(s.client, "ana", s.ana.password, CHROME_MAC)
        b = await _sign_in(s.client, "ana", s.ana.password, FIREFOX_LINUX)
        c = await _sign_in(s.client, "ana", s.ana.password, "curl/8.5.0")
        families = {
            k: _family_of(sql, s.env, v["access_token"]) for k, v in {"b": b, "c": c}.items()
        }
        resp = await s.client.post(
            f"{ME_SESSIONS}/revoke-others", headers=_bearer(a["access_token"])
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"revoked": 2}
        (event,) = _events(sql, s.env)
        assert event["action"] == "user.sessions_revoked"
        assert event["aggregate_type"] == "user"
        assert str(event["aggregate_id"]) == s.ana.id
        assert event["before"] == {"active_sessions": 3}
        after = event["after"]
        assert after["active_sessions"] == 1
        assert after["reason"] == "others_revoked"
        assert sorted(after["revoked_session_ids"]) == sorted(families.values())
        listing = await s.client.get(ME_SESSIONS, headers=_bearer(a["access_token"]))
        assert [i["current"] for i in listing.json()["items"]] == [True]
        # nothing left to revoke: 200 without an event
        again = await s.client.post(
            f"{ME_SESSIONS}/revoke-others", headers=_bearer(a["access_token"])
        )
        assert again.status_code == 200
        assert again.json() == {"revoked": 0}
        assert len(_events(sql, s.env)) == 1


async def test_sign_out_everywhere_else_with_an_unidentifiable_session_is_409(
    stack: Callable[..., Any], sql: Sql
) -> None:
    async with stack() as s:
        a, _ = await _two_sessions(s)
        sql(s.env.db_url, "UPDATE refresh_tokens SET access_jti = NULL")
        resp = await s.client.post(
            f"{ME_SESSIONS}/revoke-others", headers=_bearer(a["access_token"])
        )
        assert resp.status_code == 409, resp.text
        assert _slug(resp) == "current-session-unknown"
        assert _events(sql, s.env) == []
        listing = await s.client.get(ME_SESSIONS, headers=_bearer(a["access_token"]))
        assert listing.json()["total"] == 2


async def test_an_entra_principal_signs_out_nothing(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
) -> None:
    env = make_identity()
    oid = str(uuid.uuid4())
    env.directory.add_principal("user", oid, tenant_id=ENTRA_TENANT_ID, groups=())
    sql(
        env.db_url,
        "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
        "display_name, is_active, directly_registered, created_by, updated_by) "
        "VALUES ('user', %s, %s, NULL, 'Entra Eve', true, true, 'test', 'test')",
        ENTRA_TENANT_ID,
        oid,
    )
    async with asgi_client(env.app) as client:
        await admin_token(client, env)
        token = fake_issuer.mint_entra(oid, name="Entra Eve")
        others = await client.post(f"{ME_SESSIONS}/revoke-others", headers=_bearer(token))
        assert others.status_code == 200, others.text
        assert others.json() == {"revoked": 0}
        one = await client.delete(f"{ME_SESSIONS}/{uuid.uuid4()}", headers=_bearer(token))
        assert one.status_code == 404, one.text
    assert _events(sql, env) == []


# --- the Global admin's view of a user's sessions ----------------------------------------------


def _user_sessions(user_id: str) -> str:
    return f"{IDENTITY}/users/{user_id}/sessions"


async def test_a_global_admin_lists_and_signs_out_a_users_sessions(
    stack: Callable[..., Any], sql: Sql
) -> None:
    async with stack() as s:
        a, b = await _two_sessions(s)
        resp = await s.client.get(_user_sessions(s.ana.id), headers=_bearer(s.admin))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        # the admin's own token is not one of ana's sessions: none is current
        assert [i["current"] for i in body["items"]] == [False, False]
        family_a = _family_of(sql, s.env, a["access_token"])
        revoked = await s.client.delete(
            f"{_user_sessions(s.ana.id)}/{family_a}", headers=_bearer(s.admin)
        )
        assert revoked.status_code == 204, revoked.text
        (event,) = _events(sql, s.env)
        assert event["action"] == "session.revoked"
        assert event["after"]["reason"] == "admin_revoked"
        assert str(event["actor_id"]).startswith("local:")
        assert str(event["actor_id"]) != f"local:{s.ana.id}"
        everything = await s.client.post(
            f"{_user_sessions(s.ana.id)}/revoke-all", headers=_bearer(s.admin)
        )
        assert everything.status_code == 200, everything.text
        assert everything.json() == {"revoked": 1}
        events = _events(sql, s.env)
        assert [e["action"] for e in events] == ["session.revoked", "user.sessions_revoked"]
        last = events[-1]
        assert last["before"] == {"active_sessions": 1}
        assert last["after"]["active_sessions"] == 0
        assert last["after"]["reason"] == "admin_revoked_all"
        empty = await s.client.post(
            f"{_user_sessions(s.ana.id)}/revoke-all", headers=_bearer(s.admin)
        )
        assert empty.status_code == 200
        assert empty.json() == {"revoked": 0}
        assert len(_events(sql, s.env)) == 2
        # the signed-out sessions cannot refresh
        for token in (a, b):
            assert (
                await s.client.post(REFRESH, json={"refresh_token": token["refresh_token"]})
            ).status_code == 401


async def test_the_admin_routes_refuse_everyone_but_a_global_admin(
    stack: Callable[..., Any],
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    grant_sql: Callable[..., None],
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
    sql: Sql,
) -> None:
    async with stack() as s:
        a, _ = await _two_sessions(s)
        family_a = _family_of(sql, s.env, a["access_token"])
        root = str(sql(s.env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        org, _, _ = await folder_chain(s.client, s.admin, root, "acme")
        olga = await create_user(s.client, s.admin, "olga", display_name="Olga")
        grant_sql(s.env.db_url, olga.principal_id, "org_admin", org)
        olga_token = await login(s.client, olga.username, olga.password)
        base = _user_sessions(s.ana.id)
        for method, path in (
            ("GET", base),
            ("DELETE", f"{base}/{family_a}"),
            ("POST", f"{base}/revoke-all"),
        ):
            org_admin = await s.client.request(method, path, headers=_bearer(olga_token))
            assert org_admin.status_code == 403, (method, org_admin.text)
            anonymous = await s.client.request(method, path)
            assert anonymous.status_code == 401, method
        admin_pat = await create_pat(s.client, s.admin)
        for method, path in (
            ("DELETE", f"{base}/{family_a}"),
            ("POST", f"{base}/revoke-all"),
        ):
            by_pat = await s.client.request(method, path, headers=_bearer(admin_pat["token"]))
            assert by_pat.status_code == 403, (method, by_pat.text)
            assert _slug(by_pat) == "interactive-session-required"
        assert _events(sql, s.env) == []
        listing = await s.client.get(ME_SESSIONS, headers=_bearer(a["access_token"]))
        assert listing.json()["total"] == 2


async def test_the_admin_routes_know_only_local_users(stack: Callable[..., Any], sql: Sql) -> None:
    async with stack() as s:
        for path, method in (
            (_user_sessions(str(uuid.uuid4())), "GET"),
            (f"{_user_sessions(str(uuid.uuid4()))}/{uuid.uuid4()}", "DELETE"),
            (f"{_user_sessions(str(uuid.uuid4()))}/revoke-all", "POST"),
        ):
            resp = await s.client.request(method, path, headers=_bearer(s.admin))
            assert resp.status_code == 404, (method, resp.text)
            assert _slug(resp) == "not-found"
        a, _ = await _two_sessions(s)
        wrong_user = await s.client.delete(
            f"{_user_sessions(s.ana.id)}/{uuid.uuid4()}", headers=_bearer(s.admin)
        )
        assert wrong_user.status_code == 404
        assert _events(sql, s.env) == []


async def test_no_event_carries_a_token_hash_jti_or_user_agent(
    stack: Callable[..., Any], sql: Sql
) -> None:
    async with stack() as s:
        a, b = await _two_sessions(s)
        family_b = _family_of(sql, s.env, b["access_token"])
        await s.client.delete(f"{ME_SESSIONS}/{family_b}", headers=_bearer(a["access_token"]))
        await s.client.post(f"{ME_SESSIONS}/revoke-others", headers=_bearer(a["access_token"]))
        await s.client.post(f"{_user_sessions(s.ana.id)}/revoke-all", headers=_bearer(s.admin))
        dump = str(_events(sql, s.env))
        for secret in (
            a["refresh_token"],
            b["refresh_token"],
            a["access_token"],
            _jti(a["access_token"]),
            _jti(b["access_token"]),
            "Mozilla",
            "Chrome on macOS",
        ):
            assert secret not in dump
