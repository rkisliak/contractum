"""``POST /internal/v1/authz/principals/resolve``: a bounded, tri-state existence check (D-153).

The answer per name is ``found``, ``not_found`` or ``unverifiable`` and nothing else. The principal
is the forwarded token's; the gate runs first.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from dc_auth.principal import Principal
from dc_auth.remote import RemoteAuthorizer
from dc_auth.testing import ENTRA_TENANT_ID
from pydantic import SecretStr

pytestmark = pytest.mark.integration

RESOLVE = "/internal/v1/authz/principals/resolve"
FOLDERS = "/api/v1/identity/folders"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]
CreateUser = Callable[..., Awaitable[Any]]
Login = Callable[..., Awaitable[str]]
CreateGroup = Callable[..., Awaitable[str]]

_ANYONE = Principal(
    id="x:anyone", type="user", display="anyone", issuer="x", subject="anyone", via="jwt"
)
_STATES = {"found", "not_found", "unverifiable"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _remote(app: Any, token: str) -> Any:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        yield RemoteAuthorizer(
            http, base_url="http://identity", bearer=SecretStr(token), timeout_s=2.0
        )


async def _root(client: Any, admin: str) -> str:
    listed = await client.get(FOLDERS, headers=_bearer(admin))
    return str(next(f["id"] for f in listed.json()["items"] if f["kind"] == "root"))


async def test_users_and_groups_resolve_to_found_or_not_found(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    create_user: CreateUser,
    create_group: CreateGroup,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root(client, admin)
        await create_user(client, admin, "alice")
        sql(
            env.db_url, "UPDATE users SET email = %s WHERE username = 'alice'", "alice@corp.example"
        )
        await create_group(client, admin, root, "payments")
        users = ["alice", "ALICE@corp.example", "ghost"]
        groups = ["Payments", "nope"]
        outbox_before = sql(env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"]
        resp = await client.post(
            RESOLVE, json={"users": users, "groups": groups}, headers=_bearer(admin)
        )
        outbox_after = sql(env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"]
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "users": {"alice": "found", "ALICE@corp.example": "found", "ghost": "not_found"},
        "groups": {"Payments": "found", "nope": "not_found"},
    }
    assert set(body["users"].values()) | set(body["groups"].values()) <= _STATES
    assert outbox_after == outbox_before  # a read: nothing is audited or written


async def test_the_client_returns_the_same_maps_over_a_real_identity_app(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: CreateUser,
    create_group: CreateGroup,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root(client, admin)
        await create_user(client, admin, "alice")
        await create_group(client, admin, root, "payments")
        async with _remote(env.app, admin) as authz:
            out = await authz.resolve_principals(_ANYONE, ["alice", "ghost"], ["PAYMENTS", "nope"])
    assert out.users == {"alice": "found", "ghost": "not_found"}
    assert out.groups == {"PAYMENTS": "found", "nope": "not_found"}


async def test_an_unmatched_email_is_unverifiable_only_while_an_entra_issuer_is_configured(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
) -> None:
    env = make_identity()
    assert env.app.state.entra_tenant_ids  # the test auth settings carry an Entra issuer
    names = ["bob@corp.example", "bob", "known@corp.example", "KNOWN@Corp.Example"]
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        sql(
            env.db_url,
            "INSERT INTO entra_users (tenant_id, object_id, display_name, preferred_username, "
            "created_by, updated_by) VALUES (%s, %s, 'Known', %s, 'test', 'test')",
            ENTRA_TENANT_ID,
            "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
            "known@corp.example",
        )
        with_entra = await client.post(RESOLVE, json={"users": names}, headers=_bearer(admin))
        env.app.state.entra_tenant_ids = frozenset()  # no Entra issuer configured
        without = await client.post(RESOLVE, json={"users": names}, headers=_bearer(admin))
    assert with_entra.json()["users"] == {
        "bob@corp.example": "unverifiable",
        "bob": "not_found",
        "known@corp.example": "found",
        "KNOWN@Corp.Example": "found",
    }
    assert without.json()["users"]["bob@corp.example"] == "not_found"


async def test_an_entra_group_is_found_by_its_registered_display_name(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root(client, admin)
        sql(
            env.db_url,
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
            "display_name, created_by, updated_by) "
            "VALUES ('group', %s, %s, %s, 'Data Platform', 'test', 'test')",
            ENTRA_TENANT_ID,
            "1b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
            root,
        )
        resp = await client.post(
            RESOLVE, json={"groups": ["data platform", "other"]}, headers=_bearer(admin)
        )
    assert resp.json()["groups"] == {"data platform": "found", "other": "not_found"}


async def test_the_caps_answer_422_without_echoing_any_submitted_name(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        too_many = await client.post(
            RESOLVE,
            json={"users": [f"secret-user-{i}" for i in range(101)]},
            headers=_bearer(admin),
        )
        too_many_groups = await client.post(
            RESOLVE,
            json={"groups": [f"secret-group-{i}" for i in range(101)]},
            headers=_bearer(admin),
        )
        too_long = await client.post(
            RESOLVE, json={"users": ["secret-" + "x" * 250]}, headers=_bearer(admin)
        )
        at_cap = await client.post(
            RESOLVE,
            json={"users": [f"u{i}" for i in range(100)], "groups": ["x" * 256]},
            headers=_bearer(admin),
        )
    for resp in (too_many, too_many_groups, too_long):
        assert resp.status_code == 422, resp.text
        assert "secret-" not in resp.text
        assert resp.json()["type"] == "urn:dc:problem:validation-error"
    assert at_cap.status_code == 200, at_cap.text


async def test_one_principal_over_the_rolling_minute_budget_gets_429_and_others_do_not(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: CreateUser,
    login: Login,
) -> None:
    env = make_identity()
    assert env.settings.principal_resolve_names_per_minute == 1000
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        alice = await create_user(client, admin, "alice")
        alice_token = await login(client, "alice", alice.password)
        body = {"users": [f"u{i}" for i in range(100)], "groups": [f"g{i}" for i in range(100)]}
        for _ in range(5):  # 5 x 200 = 1000 names
            ok = await client.post(RESOLVE, json=body, headers=_bearer(admin))
            assert ok.status_code == 200, ok.text
        throttled = await client.post(RESOLVE, json={"users": ["one"]}, headers=_bearer(admin))
        other = await client.post(RESOLVE, json={"users": ["one"]}, headers=_bearer(alice_token))
    assert throttled.status_code == 429, throttled.text
    assert int(throttled.headers["Retry-After"]) >= 1
    assert throttled.json()["type"] == "urn:dc:problem:too-many-requests"
    assert "one" not in throttled.json().get("detail", "")
    assert other.status_code == 200, other.text


async def test_without_a_valid_token_there_is_no_answer(
    make_identity: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        anonymous = await client.post(RESOLVE, json={"users": ["alice"]})
        forged = await client.post(RESOLVE, json={"users": ["alice"]}, headers=_bearer("nonsense"))
    assert anonymous.status_code == 401
    assert forged.status_code == 401
    assert "alice" not in json.dumps([anonymous.text, forged.text])
