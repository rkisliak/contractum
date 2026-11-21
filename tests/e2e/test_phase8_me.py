"""``GET /api/v1/identity/me`` and ``/me/capabilities`` through the gateway (UI-07, D-UI-08-19/20).

Both operations answer 2xx for a Global admin JWT, a read-only PAT and a service-account key, so
``test_zz_operation_coverage.py`` counts them as exercised. The capability lists are checked for
the narrowing the PDP applies: a read-only PAT lists no write, a PAT or key lists no
interactive-only permission (the set comes from ``api04_areas.json``: the e2e image carries no
``dc_*`` package). A principal with no grant gets the uniform 404 problem for a folder it cannot
read. The capabilities only inform the UI; the server re-checks every write (D-UI-08-21).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

IDENTITY = "/api/v1/identity"
ME = f"{IDENTITY}/me"
CAPABILITIES = "/api/v1/identity/me/capabilities"
FOLDERS = f"{IDENTITY}/folders"
INTERACTIVE_ONLY: frozenset[str] = frozenset(
    json.loads(Path(__file__).with_name("api04_areas.json").read_text(encoding="utf-8"))[
        "interactive_only"
    ]
)
# A sample of write-class permissions a Global admin holds at a folder or at ROOT.
WRITE_SAMPLE = frozenset(
    {
        "folder:create",
        "folder:update",
        "folder:archive",
        "grant:manage",
        "contract:create",
        "contract:update",
        "contract:publish",
        "ruleset:create",
        "user:create",
        "mail:send",
    }
)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _expiry() -> str:
    return (datetime.now(UTC) + timedelta(days=30)).isoformat()


async def _created(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_me_and_capabilities_answer_for_a_jwt_a_read_only_pat_and_a_key(
    gateway: httpx.AsyncClient, admin_token: str, users: Any
) -> None:
    admin = _bearer(admin_token)
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"
    run = uuid.uuid4().hex[:10]
    org = await _created(
        await gateway.post(
            FOLDERS,
            json={"parent_id": root["id"], "slug": f"me-{run}", "display_name": f"me-{run}"},
            headers=admin,
        )
    )
    ro_pat = await _created(
        await gateway.post(
            f"{IDENTITY}/pats",
            json={"name": f"me-ro-{run}", "expires_at": _expiry(), "read_only": True},
            headers=admin,
        )
    )
    account = await _created(
        await gateway.post(
            f"{IDENTITY}/service-accounts",
            json={"owner_folder_id": org["id"], "name": f"me-bot-{run}"},
            headers=admin,
        )
    )
    granted = await gateway.post(
        f"{FOLDERS}/{org['id']}/grants",
        json={
            "principal_type": "service_account",
            "principal_id": account["principal_id"],
            "role": "org_admin",
        },
        headers=admin,
    )
    assert granted.status_code == 201, granted.text
    key = await _created(
        await gateway.post(
            f"{IDENTITY}/service-accounts/{account['id']}/keys",
            json={"name": "me", "expires_at": _expiry()},
            headers=admin,
        )
    )

    # Global admin JWT.
    me = await gateway.get(ME, headers=admin)
    assert me.status_code == 200, me.text
    assert me.json()["principal_id"].startswith("local:")
    assert me.json()["via"] == "jwt" and me.json()["interactive"] is True
    at_root = await gateway.get(CAPABILITIES, params={"folder_id": root["id"]}, headers=admin)
    at_org = await gateway.get(CAPABILITIES, params={"folder_id": org["id"]}, headers=admin)
    global_caps = await gateway.get(CAPABILITIES, headers=admin)
    for response in (at_root, at_org, global_caps):
        assert response.status_code == 200, response.text
    assert at_org.json()["grantable_roles"], "an admin can grant something at its Organization"
    assert global_caps.json()["permissions"] == me.json()["global_permissions"]
    admin_permissions = set(at_org.json()["permissions"])
    assert "grant:manage" in admin_permissions

    # Read-only PAT: both operations answer, and nothing that writes is listed.
    pat = _bearer(ro_pat["token"])
    pat_me = await gateway.get(ME, headers=pat)
    pat_org = await gateway.get(CAPABILITIES, params={"folder_id": org["id"]}, headers=pat)
    pat_global = await gateway.get(CAPABILITIES, headers=pat)
    for response in (pat_me, pat_org, pat_global):
        assert response.status_code == 200, response.text
    assert pat_me.json()["via"] == "pat" and pat_me.json()["read_only"] is True
    assert pat_me.json()["principal_id"] == me.json()["principal_id"]
    assert pat_me.json()["principal_type"] == "user"
    for body in (pat_org.json(), pat_global.json(), pat_me.json()):
        listed = set(body.get("permissions") or body["global_permissions"])
        assert listed, "a read-only PAT still reads"
        assert not listed & WRITE_SAMPLE
        assert not listed & INTERACTIVE_ONLY
    assert set(pat_org.json()["permissions"]) < admin_permissions
    assert pat_org.json()["grantable_roles"] == []

    # Service-account key: both answer, and nothing interactive-only is listed.
    sa = _bearer(key["token"])
    sa_me = await gateway.get(ME, headers=sa)
    sa_org = await gateway.get(CAPABILITIES, params={"folder_id": org["id"]}, headers=sa)
    sa_global = await gateway.get(CAPABILITIES, headers=sa)
    for response in (sa_me, sa_org, sa_global):
        assert response.status_code == 200, response.text
    assert sa_me.json()["principal_type"] == "service_account"
    assert sa_me.json()["via"] == "key" and sa_me.json()["interactive"] is False
    assert sa_me.json()["principal_id"] == account["principal_id"]
    assert sa_org.json()["roles"] == ["org_admin"]
    assert "folder:read" in sa_org.json()["permissions"]
    assert not set(sa_org.json()["permissions"]) & INTERACTIVE_ONLY
    assert not set(sa_global.json()["permissions"]) & INTERACTIVE_ONLY
    assert sa_org.json()["grantable_roles"] == []

    # A principal with no grant: the uniform 404 for a folder it cannot read, 200 for itself.
    nobody = await users.create("me-nobody")
    nobody_token = _bearer(await users.token(nobody))
    own = await gateway.get(ME, headers=nobody_token)
    assert own.status_code == 200, own.text
    assert own.json()["assignments"] == []
    denied = await gateway.get(CAPABILITIES, params={"folder_id": org["id"]}, headers=nobody_token)
    unknown = await gateway.get(
        CAPABILITIES, params={"folder_id": str(uuid.uuid4())}, headers=nobody_token
    )
    for response in (denied, unknown):
        assert response.status_code == 404, response.text
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["type"] == "urn:dc:problem:not-found"
    assert denied.json()["detail"] == unknown.json()["detail"]
