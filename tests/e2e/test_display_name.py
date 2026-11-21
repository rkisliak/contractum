"""Display names through the gateway (quick 261119-16f).

A per-run local user renames themself with ``PATCH /api/v1/identity/me`` and the new name is what
``POST /principals/names`` answers; the Global admin renames that user with
``PATCH /api/v1/identity/users/{id}``; a non-admin cannot rename anyone else (403); a signed-in
Entra user gets 409 ``display-name-managed-externally``. Only a per-run user is ever renamed:
never the bootstrap admin or a shared persona.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from conftest import FakeGraph

pytestmark = pytest.mark.e2e

IDENTITY = "/api/v1/identity"
ME = f"{IDENTITY}/me"
NAMES = f"{IDENTITY}/principals/names"
# Mirrors conftest.ENTRA_PERSONAS (the data-eng group admits alice; Plan 04-07 seeds).
ENTRA_DATA_ENG_GROUP = "9a0f0000-0000-4000-8000-0000000000a1"
DISPLAY_NAME_MANAGED = "urn:dc:problem:display-name-managed-externally"
INTERACTIVE_REQUIRED = "urn:dc:problem:interactive-session-required"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _name_of(gateway: httpx.AsyncClient, token: str, principal_id: str) -> str | None:
    response = await gateway.post(NAMES, json={"ids": [principal_id]}, headers=_bearer(token))
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    return str(items[0]["display_name"]) if items else None


async def test_a_user_renames_themself_and_the_admin_renames_them(
    gateway: httpx.AsyncClient, admin_token: str, users: Any
) -> None:
    user = await users.create("rename")
    token = await users.token(user)
    other = await users.create("bystander")

    own = await gateway.patch(ME, json={"display_name": "  Rena Me  "}, headers=_bearer(token))
    assert own.status_code == 200, own.text
    assert own.json()["display_name"] == "Rena Me"
    assert own.json()["principal_id"] == user.principal_id
    assert await _name_of(gateway, admin_token, user.principal_id) == "Rena Me"
    me = await gateway.get(ME, headers=_bearer(token))
    assert me.json()["display_name"] == "Rena Me"

    bad = await gateway.patch(ME, json={"display_name": "x\u202ey"}, headers=_bearer(token))
    assert bad.status_code == 422, bad.text

    # A user is not a Global admin: nobody else's name, and not their own through the admin route.
    for target in (other.id, user.id):
        denied = await gateway.patch(
            f"{IDENTITY}/users/{target}", json={"display_name": "Nope"}, headers=_bearer(token)
        )
        assert denied.status_code == 403, denied.text

    renamed = await gateway.patch(
        f"{IDENTITY}/users/{user.id}",
        json={"display_name": "Rena Renamed"},
        headers=_bearer(admin_token),
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "Rena Renamed"
    assert await _name_of(gateway, admin_token, user.principal_id) == "Rena Renamed"
    assert await _name_of(gateway, admin_token, other.principal_id) == other.username


async def test_an_entra_user_cannot_rename_themself(
    gateway: httpx.AsyncClient,
    admin_token: str,
    entra_token: Callable[[str], Awaitable[str]],
    fake_graph: FakeGraph,
) -> None:
    await fake_graph.reset()
    admin = _bearer(admin_token)
    roots = await gateway.get(f"{IDENTITY}/folders", params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    # Admit alice through the registered data-eng group (409: an earlier run already did).
    registered = await gateway.post(
        f"{IDENTITY}/entra/groups",
        json={"owner_folder_id": root["id"], "object_id": ENTRA_DATA_ENG_GROUP},
        headers=admin,
    )
    assert registered.status_code in (200, 201, 409), registered.text

    alice = _bearer(await entra_token("alice"))
    refused = await gateway.patch(ME, json={"display_name": "Alice Changed"}, headers=alice)
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == DISPLAY_NAME_MANAGED
    assert "Alice Changed" not in refused.text

    admin_route = await gateway.patch(
        f"{IDENTITY}/users/00000000-0000-4000-8000-000000000000",
        json={"display_name": "Nobody"},
        headers=admin,
    )
    assert admin_route.status_code == 404, admin_route.text
