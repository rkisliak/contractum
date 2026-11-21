"""Deleting folders through the gateway with the real services (quick 261119-0v1).

An admin builds organisation > project > team, sees what blocks, deletes the empty team and finds
that nothing can be created in it afterwards; a non-admin cannot delete. The journey cleans up
after itself (project, then organisation).
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.e2e

FOLDERS = "/api/v1/identity/folders"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _folder(gateway: httpx.AsyncClient, token: str, parent: str, slug: str) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent, "slug": slug, "display_name": slug},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _contract(odcs_id: str) -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "1.0.0"\n'
        "status: draft\nname: Orders\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


async def test_an_admin_deletes_an_empty_team_and_nothing_can_be_created_in_it_afterwards(
    gateway: httpx.AsyncClient, admin_token: str, non_admin_token: str
) -> None:
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=_auth(admin_token))
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]["id"]
    run = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    org = await _folder(gateway, admin_token, root, f"fd-{run}")
    project = await _folder(gateway, admin_token, org, "payments")
    team = await _folder(gateway, admin_token, project, "ingest")

    # The organisation still holds a project; the team holds nothing.
    busy = await gateway.get(f"{FOLDERS}/{org}/deletion-check", headers=_auth(admin_token))
    assert busy.status_code == 200, busy.text
    assert busy.json()["deletable"] is False
    assert {"kind": "subfolders", "count": 1} in busy.json()["blockers"]
    refused = await gateway.delete(f"{FOLDERS}/{org}", headers=_auth(admin_token))
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == "urn:dc:problem:folder-not-empty"
    empty = await gateway.get(f"{FOLDERS}/{team}/deletion-check", headers=_auth(admin_token))
    assert empty.status_code == 200, empty.text
    assert empty.json()["deletable"] is True
    assert empty.json()["blockers"] == []

    # A non-admin cannot delete (invisible or read-only), and the project is still there.
    other = await gateway.delete(f"{FOLDERS}/{project}", headers=_auth(non_admin_token))
    assert other.status_code in (403, 404), other.text
    assert (
        await gateway.get(f"{FOLDERS}/{project}", headers=_auth(admin_token))
    ).status_code == 200

    deleted = await gateway.delete(f"{FOLDERS}/{team}", headers=_auth(admin_token))
    assert deleted.status_code == 204, deleted.text
    assert (await gateway.get(f"{FOLDERS}/{team}", headers=_auth(admin_token))).status_code == 404

    # The deleted folder no longer resolves anywhere: a contract, a template and a rule set fail.
    contract = await gateway.post(
        f"/api/v1/contracts/folders/{team}/contracts",
        content=_contract(f"urn:fd:{uuid.uuid4().hex}"),
        headers={**_auth(admin_token), "Content-Type": "application/yaml"},
    )
    assert contract.status_code == 404, contract.text
    template = await gateway.post(
        f"/api/v1/contracts/folders/{team}/templates",
        params={"name": "gone"},
        content=_contract(f"urn:fd:{uuid.uuid4().hex}"),
        headers={**_auth(admin_token), "Content-Type": "application/yaml"},
    )
    assert template.status_code == 404, template.text
    rule_set = await gateway.post(
        f"/api/v1/validation/folders/{team}/rule-sets",
        json={"name": "gone", "description": "gone"},
        headers=_auth(admin_token),
    )
    assert rule_set.status_code == 404, rule_set.text

    # Clean up: the project and then the organisation are empty now.
    for folder in (project, org):
        gone = await gateway.delete(f"{FOLDERS}/{folder}", headers=_auth(admin_token))
        assert gone.status_code == 204, gone.text
