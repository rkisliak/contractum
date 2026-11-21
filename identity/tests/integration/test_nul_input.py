"""WR-02: a NUL (U+0000) in any identity text input is a 422, never a database 500."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

pytestmark = pytest.mark.integration

AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]

API = "/api/v1/identity"
NUL = "bad\x00text"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def root_id(sql: Sql, env: Any) -> str:
    return str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])


def assert_validation(response: Any) -> None:
    assert response.status_code == 422, response.text
    assert response.json()["type"] == "urn:dc:problem:validation-error"


async def test_nul_in_a_text_field_is_a_422_on_every_identity_input(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    create_group: Callable[..., Awaitable[str]],
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = root_id(sql, env)
        org, _project, _team = await folder_chain(client, token, root, "nul")
        group_id = await create_group(client, token, org, "ok-group")
        headers = auth(token)

        # users: display name, e-mail, username, the ?username= filter
        user = {"username": "nul-user", "password": "correct horse battery staple 42"}
        for field, extra in (
            ("display_name", {"display_name": NUL}),
            ("email", {"display_name": "N", "email": f"{NUL}@example.com"}),
            ("username", {"display_name": "N", "username": NUL}),
        ):
            created = await client.post(f"{API}/users", json={**user, **extra}, headers=headers)
            assert_validation(created)
            assert field in created.text
        assert_validation(
            await client.get(f"{API}/users", params={"username": NUL}, headers=headers)
        )

        # groups: name on create and rename, username on member add
        assert_validation(
            await client.post(
                f"{API}/groups", json={"owner_folder_id": org, "name": NUL}, headers=headers
            )
        )
        assert_validation(
            await client.patch(f"{API}/groups/{group_id}", json={"name": NUL}, headers=headers)
        )
        assert_validation(
            await client.post(
                f"{API}/groups/{group_id}/members", json={"username": NUL}, headers=headers
            )
        )

        # folders: display name on create and rename
        assert_validation(
            await client.post(
                f"{API}/folders",
                json={"parent_id": org, "slug": "nul-child", "display_name": NUL},
                headers=headers,
            )
        )
        assert_validation(
            await client.patch(f"{API}/folders/{org}", json={"display_name": NUL}, headers=headers)
        )

        # login (public): a NUL username is refused before it reaches the database
        assert_validation(
            await client.post(
                f"{API}/auth/login", json={"username": NUL, "password": "irrelevant-password"}
            )
        )
