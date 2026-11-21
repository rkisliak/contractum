"""The one name rule (quick 261120-lbz, D-01) on every identity create and rename endpoint.

A name with a bidirectional override or an emoji is a 422 ``validation-error`` whose pointer names
the field and whose detail names the code point, never the character; a valid name in any script
is stored trimmed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]

SPOOF = "Team\u202eevil"
EMOJI = "Ops \U0001f600"
PASSPHRASE = "correct horse battery staple 42"  # noqa: S105


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _expiry() -> str:
    return (datetime.now(UTC) + timedelta(days=30)).isoformat()


def refused(resp: Any, pointer: str, point: str) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["type"] == "urn:dc:problem:validation-error"
    assert body["errors"][0]["pointer"] == pointer
    assert point in body["errors"][0]["detail"]
    assert "\u202e" not in resp.text
    assert "\U0001f600" not in resp.text


def root_id(sql: Sql, env: Any) -> str:
    return str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])


@pytest.mark.parametrize(("name", "point"), [(SPOOF, "U+202E"), (EMOJI, "U+1F600")])
async def test_users_groups_service_accounts_and_tokens_refuse_the_name(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
    name: str,
    point: str,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = root_id(sql, env)

        user = await client.post(
            f"{IDENTITY}/users",
            json={"username": "bad.name", "password": PASSPHRASE, "display_name": name},
            headers=auth(token),
        )
        refused(user, "/body/display_name", point)
        group = await client.post(
            f"{IDENTITY}/groups",
            json={"owner_folder_id": root, "name": name},
            headers=auth(token),
        )
        refused(group, "/body/name", point)
        account = await client.post(
            f"{IDENTITY}/service-accounts",
            json={"owner_folder_id": root, "name": name},
            headers=auth(token),
        )
        refused(account, "/body/name", point)
        pat = await client.post(
            f"{IDENTITY}/pats", json={"name": name, "expires_at": _expiry()}, headers=auth(token)
        )
        refused(pat, "/body/name", point)
        assert sql(env.db_url, "SELECT count(*) AS n FROM api_credentials")[0]["n"] == 0


async def test_renames_and_keys_follow_the_rule_and_valid_names_are_trimmed(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = root_id(sql, env)

        user = await client.post(
            f"{IDENTITY}/users",
            json={"username": "ana", "password": PASSPHRASE, "display_name": "  数据 Ana  "},
            headers=auth(token),
        )
        assert user.status_code == 201, user.text
        assert user.json()["display_name"] == "数据 Ana"
        user_url = f"{IDENTITY}/users/{user.json()['id']}"
        refused(
            await client.patch(user_url, json={"display_name": SPOOF}, headers=auth(token)),
            "/body/display_name",
            "U+202E",
        )

        group = await client.post(
            f"{IDENTITY}/groups",
            json={"owner_folder_id": root, "name": "  Équipe données  "},
            headers=auth(token),
        )
        assert group.status_code == 201, group.text
        assert group.json()["name"] == "Équipe données"
        refused(
            await client.patch(
                f"{IDENTITY}/groups/{group.json()['id']}",
                json={"name": EMOJI},
                headers=auth(token),
            ),
            "/body/name",
            "U+1F600",
        )

        account = await client.post(
            f"{IDENTITY}/service-accounts",
            json={"owner_folder_id": root, "name": " pipeline-bot "},
            headers=auth(token),
        )
        assert account.status_code == 201, account.text
        assert account.json()["name"] == "pipeline-bot"
        account_url = f"{IDENTITY}/service-accounts/{account.json()['id']}"
        refused(
            await client.patch(account_url, json={"name": SPOOF}, headers=auth(token)),
            "/body/name",
            "U+202E",
        )
        refused(
            await client.post(
                f"{account_url}/keys",
                json={"name": EMOJI, "expires_at": _expiry()},
                headers=auth(token),
            ),
            "/body/name",
            "U+1F600",
        )

        pat = await client.post(
            f"{IDENTITY}/pats",
            json={"name": "  ci token (EU)  ", "expires_at": _expiry()},
            headers=auth(token),
        )
        assert pat.status_code == 201, pat.text
        assert pat.json()["name"] == "ci token (EU)"


async def test_me_rename_follows_the_rule(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        ana = await create_user(client, admin, "ana", display_name="Ana")
        token = await login(client, ana.username, ana.password)
        refused(
            await client.patch(f"{IDENTITY}/me", json={"display_name": SPOOF}, headers=auth(token)),
            "/body/display_name",
            "U+202E",
        )
        ok = await client.patch(
            f"{IDENTITY}/me", json={"display_name": "  Ana Díaz  "}, headers=auth(token)
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["display_name"] == "Ana Díaz"
