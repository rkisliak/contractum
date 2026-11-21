"""Local user creation by a Global admin (RBAC-10, D-26)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

pytestmark = pytest.mark.integration

USERS = "/api/v1/identity/users"
FOLDERS = "/api/v1/identity/folders"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]

PASSPHRASE = "correct horse battery staple 42"  # noqa: S105


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def root_id(sql: Sql, env: Any) -> str:
    return str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])


async def test_admin_creates_a_user_who_logs_in_and_sees_nothing_until_granted(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    login: Callable[..., Awaitable[str]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        before = len(sql(env.db_url, "SELECT id FROM outbox"))

        resp = await client.post(
            USERS,
            json={
                "username": "Alice.Data",
                "password": PASSPHRASE,
                "display_name": "Alice",
                "email": "alice@example.com",
            },
            headers=auth(token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert resp.headers["location"] == f"{USERS}/{body['id']}"
        assert body["username"] == "alice.data"
        assert body["principal_id"] == f"local:{body['id']}"
        assert body["display_name"] == "Alice"
        assert body["email"] == "alice@example.com"
        assert body["is_active"] is True
        assert body["must_rotate"] is False
        assert "password" not in body
        assert "password_hash" not in body

        # Stored only as an Argon2id hash.
        row = sql(env.db_url, "SELECT password_hash FROM users WHERE username = 'alice.data'")[0]
        assert row["password_hash"].startswith("$argon2id$")
        assert PASSPHRASE not in row["password_hash"]

        # Exactly one new audit event, with derived flags and no secret or address.
        events = sql(
            env.db_url,
            "SELECT action, aggregate_type, before, after FROM outbox ORDER BY id OFFSET %s",
            before,
        )
        assert [e["action"] for e in events] == ["user.created"]
        after = events[0]["after"]
        assert events[0]["before"] is None
        assert after["email_set"] is True
        assert after["password_set"] is True
        assert after["username"] == "alice.data"
        assert after["must_rotate"] is False
        assert after["is_active"] is True
        assert not {"password", "password_hash", "email"} & set(after)
        assert PASSPHRASE not in str(events[0])
        assert "alice@example.com" not in str(events[0])

        # The new user can log in immediately and has no access (D-26).
        alice = await login(client, "alice.data", PASSPHRASE)
        folders = await client.get(FOLDERS, headers=auth(alice))
        assert folders.status_code == 200
        assert folders.json() == {"items": [], "next_cursor": None}
        root = await client.get(f"{FOLDERS}/{root_id(sql, env)}", headers=auth(alice))
        assert root.status_code == 404

        # The admin can read the user back by id and by exact username.
        got = await client.get(f"{USERS}/{body['id']}", headers=auth(token))
        assert got.status_code == 200
        assert got.json() == body
        found = await client.get(f"{USERS}?username=alice.data", headers=auth(token))
        assert found.status_code == 200
        assert [item["id"] for item in found.json()["items"]] == [body["id"]]


async def test_helpers_create_and_log_in_a_user_without_email(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        user = await create_user(client, token, "bob-the-builder")
        assert len(user.password) == 24
        assert user.principal_id == f"local:{user.id}"
        assert uuid.UUID(user.id)
        assert await login(client, user.username, user.password)


def problem_type(resp: Any) -> str:
    return str(resp.json()["type"])


def user_count(sql: Sql, env: Any) -> int:
    return int(sql(env.db_url, "SELECT count(*) AS n FROM users")[0]["n"])


def created_events(sql: Sql, env: Any) -> int:
    return int(
        sql(env.db_url, "SELECT count(*) AS n FROM outbox WHERE action = 'user.created'")[0]["n"]
    )


def new_user(username: str = "dora", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "username": username,
        "password": PASSPHRASE,
        "display_name": username.title(),
    }
    body.update(overrides)
    return body


async def test_only_a_global_admin_may_create_list_or_read_users(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    bootstrap_token: Callable[[Any, Any], Awaitable[str]],
    fake_issuer: Any,
    sql: Sql,
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    grant_sql: Callable[..., None],
    create_user: Callable[..., Awaitable[Any]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        # The bootstrap-restricted token is checked before the admin rotates the password.
        restricted = await bootstrap_token(client, env)
        for method, url in (("POST", USERS), ("GET", USERS)):
            resp = await client.request(
                method,
                url,
                headers=auth(restricted),
                **({"json": new_user()} if method == "POST" else {}),
            )
            assert resp.status_code == 403, (method, resp.text)
            assert problem_type(resp) == "urn:dc:problem:password-rotation-required"

        token = await admin_token(client, env)
        org, _, _ = await folder_chain(client, token, root_id(sql, env), "acme")
        grant_sql(env.db_url, "fake:oa", "org_admin", org)
        target = await create_user(client, token, "target-user")

        callers = {
            "org_admin": fake_issuer.mint("oa"),
            "stranger": fake_issuer.mint("nobody"),
        }
        for name, caller in callers.items():
            post = await client.post(USERS, json=new_user("eve"), headers=auth(caller))
            assert post.status_code == 403, (name, post.text)
            assert problem_type(post) == "urn:dc:problem:forbidden"
            listed = await client.get(USERS, headers=auth(caller))
            assert listed.status_code == 403, name
            assert problem_type(listed) == "urn:dc:problem:forbidden"
            one = await client.get(f"{USERS}/{target.id}", headers=auth(caller))
            assert one.status_code == 403, name

        assert (await client.post(USERS, json=new_user("eve"))).status_code == 401
        assert (await client.get(USERS)).status_code == 401
        assert sql(env.db_url, "SELECT 1 FROM users WHERE username = 'eve'") == []


@pytest.mark.parametrize(
    "password",
    ["short-pw", "DORA", "password1234", "x" * 11],
)
async def test_a_weak_password_is_a_policy_violation_with_no_row_and_no_event(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    password: str,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        users, events = user_count(sql, env), created_events(sql, env)
        resp = await client.post(
            USERS, json=new_user("dora", password=password), headers=auth(token)
        )
        assert resp.status_code == 422, resp.text
        assert problem_type(resp) == "urn:dc:problem:password-policy"
        assert password not in resp.text
        assert (user_count(sql, env), created_events(sql, env)) == (users, events)


async def test_a_password_equal_to_the_username_in_any_case_is_refused(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        users = user_count(sql, env)
        resp = await client.post(
            USERS,
            json=new_user("Long.Username.Here", password="LONG.username.HERE"),
            headers=auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert problem_type(resp) == "urn:dc:problem:password-policy"
        assert user_count(sql, env) == users
        assert created_events(sql, env) == 0


@pytest.mark.parametrize(
    "username",
    ["ab", "-alice", "alice_", "al ice", "a" * 65, "al/ice", "alice.", ""],
)
async def test_an_invalid_username_is_a_validation_error(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    username: str,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        users = user_count(sql, env)
        resp = await client.post(USERS, json=new_user(username), headers=auth(token))
        assert resp.status_code == 422, resp.text
        assert problem_type(resp) == "urn:dc:problem:validation-error"
        assert user_count(sql, env) == users
        assert created_events(sql, env) == 0


@pytest.mark.parametrize("email", ["no-at-sign", "a@b c", "a@@b", "x" * 243 + "@example.com"])
async def test_an_invalid_email_is_a_validation_error(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    email: str,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        resp = await client.post(USERS, json=new_user(email=email), headers=auth(token))
        assert resp.status_code == 422, resp.text
        assert problem_type(resp) == "urn:dc:problem:validation-error"
        assert sql(env.db_url, "SELECT 1 FROM users WHERE username = 'dora'") == []


async def test_a_255_character_email_is_refused(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        email = "x" * (255 - len("@example.com")) + "@example.com"
        assert len(email) == 255
        resp = await client.post(USERS, json=new_user(email=email), headers=auth(token))
        assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("field", ["is_active", "must_rotate", "id", "password_hash", "roles"])
async def test_server_owned_fields_cannot_be_mass_assigned(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    field: str,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        value: Any = False if field in ("is_active", "must_rotate") else "x"
        resp = await client.post(USERS, json=new_user(**{field: value}), headers=auth(token))
        assert resp.status_code == 422, resp.text
        assert sql(env.db_url, "SELECT 1 FROM users WHERE username = 'dora'") == []


async def test_idempotency_creating_the_same_username_twice_in_any_case_is_a_409(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        first = await client.post(USERS, json=new_user("bob"), headers=auth(token))
        assert first.status_code == 201, first.text
        second = await client.post(USERS, json=new_user("BOB"), headers=auth(token))
        assert second.status_code == 409, second.text
        assert problem_type(second) == "urn:dc:problem:conflict"
        assert (
            await client.post(USERS, json=new_user("bob"), headers=auth(token))
        ).status_code == 409
        assert len(sql(env.db_url, "SELECT 1 FROM users WHERE username = 'bob'")) == 1
        assert created_events(sql, env) == 1
        # The first user's password is untouched by the rejected duplicates.
        assert (
            await client.post(
                "/api/v1/identity/auth/login", json={"username": "bob", "password": PASSPHRASE}
            )
        ).status_code == 200


async def test_concurrency_two_simultaneous_creates_of_one_username_yield_one_201(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        responses = await asyncio.gather(
            client.post(USERS, json=new_user("carol"), headers=auth(token)),
            client.post(USERS, json=new_user("carol"), headers=auth(token)),
        )
        assert {r.status_code for r in responses} == {201, 409}, [r.text for r in responses]
        assert len(sql(env.db_url, "SELECT 1 FROM users WHERE username = 'carol'")) == 1
        assert created_events(sql, env) == 1


async def test_users_list_is_keyset_paged_without_duplicates_or_totals(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        # The bootstrap admin is user number one; three more make four in total.
        for name in ("user-one", "user-two", "user-three"):
            await create_user(client, token, name)
        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            url = f"{USERS}?limit=2" + (f"&cursor={cursor}" if cursor else "")
            page = await client.get(url, headers=auth(token))
            assert page.status_code == 200, page.text
            body = page.json()
            assert set(body) == {"items", "next_cursor"}
            assert len(body["items"]) <= 2
            seen.extend(item["id"] for item in body["items"])
            pages += 1
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert pages == 2
        assert len(seen) == len(set(seen)) == 4
        assert seen == sorted(seen)
        bad = await client.get(f"{USERS}?cursor=not-a-cursor", headers=auth(token))
        assert bad.status_code == 422
        assert problem_type(bad) == "urn:dc:problem:validation-error"


async def test_an_unknown_user_id_is_a_404_and_a_created_user_cannot_read_the_directory(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        missing = await client.get(f"{USERS}/{uuid.uuid4()}", headers=auth(token))
        assert missing.status_code == 404
        assert problem_type(missing) == "urn:dc:problem:not-found"
        carol = await create_user(client, token, "carol")
        carol_token = await login(client, carol.username, carol.password)
        assert (await client.get(USERS, headers=auth(carol_token))).status_code == 403
        assert (
            await client.get(f"{USERS}/{carol.id}", headers=auth(carol_token))
        ).status_code == 403
