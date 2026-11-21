"""Gateway 2xx calls for operations no other journey answered successfully (operation coverage).

Before this module, ``tests/e2e/operation_exemptions.json`` exempted the folder card list, a card
member removal, card archiving and logout because no journey called them successfully; they are
safe to exercise on a shared stack (per-run folders and users, nothing shared is changed), so they
are exercised here and their exemptions are gone. The remaining exemptions change shared personas
or the publish gate of the whole stack and stay exempt with their reasons.

* cards: the Global admin creates a per-run Organization -> Project -> Team, a card on the team and
  a per-run ROOT-owned local group; links the group, lists the folder's cards, unlinks the group
  (204, and the member list no longer has it) and archives the card (the archived card is only
  listed with ``include_archived``).
* principal names: a per-run user with no grant asks for its own name and an unknown id through the
  gateway (200, only the known id answered).
* logout: a per-run local user signs in with body delivery, logs out with its refresh token (204),
  and that refresh token is refused afterwards (401): the family is revoked.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
CARDS = "/api/v1/contracts/cards"
LOGIN = f"{IDENTITY}/auth/login"
LOGOUT = f"{IDENTITY}/auth/logout"
REFRESH = f"{IDENTITY}/auth/refresh"

Headers = dict[str, str]


def _bearer(token: str) -> Headers:
    return {"Authorization": f"Bearer {token}"}


async def _root_id(gateway: httpx.AsyncClient, admin: Headers) -> str:
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"
    return str(root["id"])


async def _folder(gateway: httpx.AsyncClient, admin: Headers, parent_id: str, slug: str) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent_id, "slug": slug, "display_name": f"Gaps {slug}"},
        headers=admin,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _card_ids(
    gateway: httpx.AsyncClient, admin: Headers, team: str, **params: Any
) -> list[str]:
    listed = await gateway.get(
        f"/api/v1/contracts/folders/{team}/cards", params=params, headers=admin
    )
    assert listed.status_code == 200, listed.text
    return [str(item["id"]) for item in listed.json()["items"]]


async def test_card_list_member_removal_and_archive(
    gateway: httpx.AsyncClient, admin_token: str
) -> None:
    admin = _bearer(admin_token)
    run = uuid.uuid4().hex[:10]
    root = await _root_id(gateway, admin)
    org = await _folder(gateway, admin, root, f"p8gap-{run}")
    project = await _folder(gateway, admin, org, "data")
    team = await _folder(gateway, admin, project, "team")

    created = await gateway.post(
        f"/api/v1/contracts/folders/{team}/cards",
        json={
            "name": f"gap-card-{run}",
            "description": "Operation coverage card.",
            "contacts": [{"name": "Ops", "kind": "email", "value": f"ops-{run}@example.test"}],
            "email_enabled": False,
        },
        headers=admin,
    )
    assert created.status_code == 201, created.text
    card_id = str(created.json()["id"])

    group = await gateway.post(
        f"{IDENTITY}/groups",
        json={"owner_folder_id": root, "name": f"p8gap-grp-{run}"},
        headers=admin,
    )
    assert group.status_code == 201, group.text
    group_id = str(group.json()["id"])
    linked = await gateway.post(
        f"{CARDS}/{card_id}/members",
        json={"member_type": "local_group", "member_id": group_id},
        headers=admin,
    )
    assert linked.status_code == 201, linked.text

    assert await _card_ids(gateway, admin, team) == [card_id]
    # The all-cards index (the UI's /cards screen) finds it under the organization's subtree.
    index = await gateway.get(CARDS, params={"folder_id": org, "subtree": "true"}, headers=admin)
    assert index.status_code == 200, index.text
    assert [str(item["id"]) for item in index.json()["items"]] == [card_id]

    member = f"{CARDS}/{card_id}/members/local_group/{group_id}"
    removed = await gateway.delete(member, headers=admin)
    assert removed.status_code == 204, removed.text
    card = await gateway.get(f"{CARDS}/{card_id}", headers=admin)
    assert card.status_code == 200, card.text
    assert [m["member_id"] for m in card.json()["members"]] == []

    archived = await gateway.post(f"{CARDS}/{card_id}/archive", headers=admin)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    assert await _card_ids(gateway, admin, team) == []
    assert await _card_ids(gateway, admin, team, include_archived="true") == [card_id]


async def test_logout_revokes_the_refresh_token_family(
    gateway: httpx.AsyncClient, users: Any
) -> None:
    user = await users.create("p8gap")
    for attempt in range(5):
        login = await gateway.post(
            LOGIN, json={"username": user.username, "password": user.password}
        )
        if login.status_code != 429 or attempt == 4:
            break
        await asyncio.sleep(1.0)
    assert login.status_code == 200, login.text
    refresh_token = login.json()["refresh_token"]

    logout = await gateway.post(LOGOUT, json={"refresh_token": refresh_token})
    assert logout.status_code == 204, logout.text
    refused = await gateway.post(REFRESH, json={"refresh_token": refresh_token})
    assert refused.status_code == 401, refused.text


async def test_a_user_with_no_grant_turns_principal_ids_into_names(
    gateway: httpx.AsyncClient, users: Any
) -> None:
    user = await users.create("p8names")
    token = await users.token(user)
    unknown = f"local:{uuid.uuid4()}"
    response = await gateway.post(
        f"{IDENTITY}/principals/names",
        json={"ids": [user.principal_id, unknown]},
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == [{"id": user.principal_id, "display_name": user.username}]
