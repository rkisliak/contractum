"""``POST /api/v1/identity/principals/names``: display names for principal ids.

Any non-restricted principal (a user with no grant, a read-only PAT) turns ids into names; only the
display name comes back; unknown, malformed and other-namespace ids are absent, never an error; a
restricted bootstrap token is 403; no token is 401; the list is bounded at 100.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.integration

NAMES = "/api/v1/identity/principals/names"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_a_user_with_no_grant_sees_names_and_nothing_else(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: Callable[[Any, Any], Awaitable[str]],
    create_user: Callable[..., Awaitable[Any]],
    create_service_account: Callable[..., Awaitable[dict[str, Any]]],
    login: Callable[..., Awaitable[str]],
    sql: Callable[..., list[dict[str, Any]]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        lena = await create_user(client, admin, "lena", display_name="Lena Ledger")
        nobody = await create_user(client, admin, "nobody")
        account = await create_service_account(client, admin, root, "nightly-loader")
        token = await login(client, nobody.username, nobody.password)
        admin_id = sql(env.db_url, "SELECT id FROM users WHERE username = %s", "admin")[0]["id"]
        unknown = f"local:{uuid.uuid4()}"
        ids = [
            lena.principal_id,
            lena.principal_id,
            account["principal_id"],
            f"local:{admin_id}",
            unknown,
            "LOCAL:not-canonical",
            "mock:someone",
            f"entra:{uuid.uuid4()}:{uuid.uuid4()}",
        ]
        resp = await client.post(NAMES, json={"ids": ids}, headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items == sorted(items, key=lambda item: item["id"])
    assert {item["id"]: item["display_name"] for item in items} == {
        lena.principal_id: "Lena Ledger",
        account["principal_id"]: "nightly-loader",
        f"local:{admin_id}": sql(
            env.db_url, "SELECT display_name FROM users WHERE username = %s", "admin"
        )[0]["display_name"],
    }
    assert all(set(item) == {"id", "display_name"} for item in items)


async def test_a_read_only_pat_may_ask(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: Callable[[Any, Any], Awaitable[str]],
    create_user: Callable[..., Awaitable[Any]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        lena = await create_user(client, admin, "lena", display_name="Lena Ledger")
        made = await client.post(
            "/api/v1/identity/pats",
            json={
                "name": "ro",
                "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "read_only": True,
            },
            headers=_bearer(admin),
        )
        assert made.status_code == 201, made.text
        resp = await client.post(
            NAMES, json={"ids": [lena.principal_id]}, headers=_bearer(made.json()["token"])
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == [{"id": lena.principal_id, "display_name": "Lena Ledger"}]


async def test_no_token_is_401_and_a_restricted_token_is_403(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    bootstrap_token: Callable[[Any, Any], Awaitable[str]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        anonymous = await client.post(NAMES, json={"ids": ["local:x"]})
        restricted = await bootstrap_token(client, env)
        refused = await client.post(NAMES, json={"ids": ["local:x"]}, headers=_bearer(restricted))
    assert anonymous.status_code == 401, anonymous.text
    assert refused.status_code == 403, refused.text
    assert refused.json()["type"] == "urn:dc:problem:password-rotation-required"


@pytest.mark.parametrize(
    "ids",
    [[], [f"local:{uuid.uuid4()}" for _ in range(101)], ["x" * 201]],
    ids=["empty", "too-many", "too-long"],
)
async def test_the_list_is_bounded(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: Callable[[Any, Any], Awaitable[str]],
    ids: list[str],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        resp = await client.post(NAMES, json={"ids": ids}, headers=_bearer(admin))
    assert resp.status_code == 422, resp.text
    assert resp.json()["type"] == "urn:dc:problem:validation-error"


async def test_entra_users_and_service_principals_are_named_and_groups_are_not(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: Callable[[Any, Any], Awaitable[str]],
    sql: Callable[..., list[dict[str, Any]]],
) -> None:
    env = make_identity()
    tenant = str(uuid.uuid4())
    user_oid, sp_oid, group_oid = (str(uuid.uuid4()) for _ in range(3))
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        insert = (
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
            "display_name, created_by, updated_by) VALUES (%s, %s, %s, %s, %s, 'test', 'test')"
        )
        sql(env.db_url, insert, "user", tenant, user_oid, None, "Alice (registered)")
        sql(env.db_url, insert, "service_principal", tenant, sp_oid, root, "ETL pipeline")
        sql(env.db_url, insert, "group", tenant, group_oid, root, "Data Eng")
        # The just-in-time row carries the name Entra last reported; it wins over a registration.
        sql(
            env.db_url,
            "INSERT INTO entra_users (tenant_id, object_id, display_name, created_by, updated_by) "
            "VALUES (%s, %s, 'Alice Example', 'test', 'test')",
            tenant,
            user_oid,
        )
        ids = [f"entra:{tenant}:{oid}" for oid in (user_oid, sp_oid, group_oid)]
        resp = await client.post(NAMES, json={"ids": ids}, headers=_bearer(admin))
    assert resp.status_code == 200, resp.text
    assert {item["id"]: item["display_name"] for item in resp.json()["items"]} == {
        f"entra:{tenant}:{user_oid}": "Alice Example",
        f"entra:{tenant}:{sp_oid}": "ETL pipeline",
    }
