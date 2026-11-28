"""The Phase 2 goal, end to end through the gateway (ROADMAP Phase 2 success criteria).

"Admins delegate access anywhere in the ROOT -> Organization -> Project -> Team hierarchy with
predictable, additive inheritance, and organizations stay isolated. Policy Managers publish
templates that teams below can start contracts from."

Every test builds its own Organization -> Project -> Team and its own users through the ``users``
fixture, so the tests are order-independent on a warm shared stack. All calls go through
https://traefik; only the audit read uses the internal audit service.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
import pytest

pytestmark = pytest.mark.e2e

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
GROUPS = f"{IDENTITY}/groups"
CONTRACTS = "/api/v1/contracts/contracts"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _contract_yaml(odcs_id: str, name: str = "Phase 2 Orders") -> bytes:
    text = f"""# Phase 2 journey: comments must survive byte for byte.
kind: DataContract
id: {odcs_id}   # inline comment
apiVersion: v3.2.0
name:   {name}
version: "1.0.0"
status: active
domain: delegation
tags: [phase2, orders]
description:
  purpose: Created by a delegated member and fetched back unchanged.
schema:
  - name: orders
    physicalType: table
    properties:
      - name: order_id
        logicalType: string
"""
    return text.encode("utf-8")


def _new_odcs_id() -> str:
    return f"urn:datacontract:phase2:{uuid.uuid4()}"


@dataclass(frozen=True)
class Tree:
    root: str
    org: str
    project: str
    team: str


async def _create_folder(
    gateway: httpx.AsyncClient, headers: dict[str, str], parent_id: str, slug: str
) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent_id, "slug": slug, "display_name": f"Phase 2 {slug}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _tree(gateway: httpx.AsyncClient, admin_token: str) -> Tree:
    headers = _bearer(admin_token)
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=headers)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"
    run = uuid.uuid4().hex[:10]
    org = await _create_folder(gateway, headers, root["id"], f"p2-{run}")
    project = await _create_folder(gateway, headers, org, "payments")
    team = await _create_folder(gateway, headers, project, "ingest")
    return Tree(root=str(root["id"]), org=org, project=project, team=team)


async def _grant(
    gateway: httpx.AsyncClient,
    token: str,
    folder_id: str,
    principal_type: str,
    principal_id: str,
    role: str,
) -> httpx.Response:
    return await gateway.post(
        f"{FOLDERS}/{folder_id}/grants",
        json={"principal_type": principal_type, "principal_id": principal_id, "role": role},
        headers=_bearer(token),
    )


async def _create_contract(
    gateway: httpx.AsyncClient, token: str, folder_id: str, raw: bytes
) -> httpx.Response:
    return await gateway.post(
        f"/api/v1/contracts/folders/{folder_id}/contracts",
        content=raw,
        headers={**_bearer(token), "Content-Type": "application/yaml"},
    )


async def _audit_events(
    audit: httpx.AsyncClient, headers: dict[str, str], aggregate_id: str, count: int = 1
) -> list[dict[str, Any]] | None:
    response = await audit.get(
        "/internal/v1/events", params={"aggregate_id": aggregate_id}, headers=headers
    )
    response.raise_for_status()
    events: list[dict[str, Any]] = response.json()
    return events if len(events) >= count else None


def _principal_of(token: str) -> str:
    return "local:" + str(jwt.decode(token, options={"verify_signature": False})["sub"])


async def test_org_admin_delegates_a_team_through_a_group_and_the_member_creates_a_contract(
    gateway: httpx.AsyncClient,
    internal: Callable[[str], httpx.AsyncClient],
    admin_token: str,
    users: Any,
    wait_for: Callable[..., Any],
) -> None:
    admin = _bearer(admin_token)
    admin_id = _principal_of(admin_token)
    tree = await _tree(gateway, admin_token)

    oa = await users.create("oa")
    dev = await users.create("dev")

    # The Global admin makes oa an Organization admin of the new organization.
    org_grant = await _grant(gateway, admin_token, tree.org, "user", oa.principal_id, "org_admin")
    assert org_grant.status_code == 201, org_grant.text
    oa_token = await users.token(oa)
    dev_token = await users.token(dev)

    # oa creates a local group in the organization, adds dev by username, grants the group Team
    # editor on the team folder.
    created = await gateway.post(
        GROUPS,
        json={"owner_folder_id": tree.org, "name": f"Ingest devs {uuid.uuid4().hex[:6]}"},
        headers=_bearer(oa_token),
    )
    assert created.status_code == 201, created.text
    group_id = str(created.json()["id"])  # a bare lowercase uuid, used unmodified below
    assert str(uuid.UUID(group_id)) == group_id

    added = await gateway.post(
        f"{GROUPS}/{group_id}/members", json={"username": dev.username}, headers=_bearer(oa_token)
    )
    assert added.status_code == 201, added.text
    assert added.json()["member_id"] == dev.principal_id

    group_grant = await _grant(gateway, oa_token, tree.team, "local_group", group_id, "team_editor")
    assert group_grant.status_code == 201, group_grant.text
    assert group_grant.json()["principal_id"] == group_id

    # dev holds contract:create at the team through the group: creates a contract and reads the
    # identical bytes back.
    odcs_id = _new_odcs_id()
    raw = _contract_yaml(odcs_id)
    contract = await _create_contract(gateway, dev_token, tree.team, raw)
    assert contract.status_code == 201, contract.text
    contract_id = str(contract.json()["contract"]["id"])
    document = await gateway.get(f"{CONTRACTS}/{contract_id}/document", headers=_bearer(dev_token))
    assert document.status_code == 200, document.text
    assert document.content == raw
    metadata = await gateway.get(f"{CONTRACTS}/{contract_id}", headers=_bearer(dev_token))
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["created_by"] == dev.principal_id
    assert metadata.json()["folder_id"] == tree.team

    # Audit: exactly one event per write, each with the right typed actor.
    audit = internal("audit")

    async def only(aggregate_id: str, action: str, count: int = 1) -> dict[str, Any]:
        events = await wait_for(
            lambda: _audit_events(audit, admin, aggregate_id, count),
            30.0,
            description=f"the {action} event for {aggregate_id}",
        )
        matching: list[dict[str, Any]] = [e for e in events if e["action"] == action]
        assert len(matching) == 1, events
        return matching[0]

    for user in (oa, dev):
        event = await only(user.id, "user.created")
        assert event["actor"]["type"] == "user"
        assert event["actor"]["id"] == admin_id
    org_grant_event = await only(str(org_grant.json()["id"]), "grant.created")
    assert org_grant_event["actor"]["id"] == admin_id
    group_grant_event = await only(str(group_grant.json()["id"]), "grant.created")
    assert group_grant_event["actor"]["type"] == "user"
    assert group_grant_event["actor"]["id"] == oa.principal_id
    group_created = await only(group_id, "group.created", 2)
    assert group_created["actor"]["id"] == oa.principal_id
    member_added = await only(group_id, "group.member_added", 2)
    assert member_added["actor"]["id"] == oa.principal_id
    contract_created = await only(contract_id, "contract.created")
    assert contract_created["actor"]["type"] == "user"
    assert contract_created["actor"]["id"] == dev.principal_id
    assert contract_created["scope_path"] == [tree.root, tree.org, tree.project, tree.team]


@dataclass(frozen=True)
class Delegation:
    """An organization whose Org admin ``oa`` delegated the team to a group holding ``dev``."""

    tree: Tree
    oa: Any
    dev: Any
    oa_token: str
    dev_token: str
    group_id: str


async def _delegation(
    gateway: httpx.AsyncClient, admin_token: str, users: Any, *, group_grant: bool = True
) -> Delegation:
    tree = await _tree(gateway, admin_token)
    oa = await users.create("oa")
    dev = await users.create("dev")
    granted = await _grant(gateway, admin_token, tree.org, "user", oa.principal_id, "org_admin")
    assert granted.status_code == 201, granted.text
    oa_token = await users.token(oa)
    group = await gateway.post(
        GROUPS,
        json={"owner_folder_id": tree.org, "name": f"Devs {uuid.uuid4().hex[:6]}"},
        headers=_bearer(oa_token),
    )
    assert group.status_code == 201, group.text
    group_id = str(group.json()["id"])
    added = await gateway.post(
        f"{GROUPS}/{group_id}/members", json={"username": dev.username}, headers=_bearer(oa_token)
    )
    assert added.status_code == 201, added.text
    if group_grant:
        team = await _grant(gateway, oa_token, tree.team, "local_group", group_id, "team_editor")
        assert team.status_code == 201, team.text
    return Delegation(
        tree=tree,
        oa=oa,
        dev=dev,
        oa_token=oa_token,
        dev_token=await users.token(dev),
        group_id=group_id,
    )


async def _dev_creates_a_contract(gateway: httpx.AsyncClient, delegation: Delegation) -> str:
    created = await _create_contract(
        gateway, delegation.dev_token, delegation.tree.team, _contract_yaml(_new_odcs_id())
    )
    assert created.status_code == 201, created.text
    return str(created.json()["contract"]["id"])


def _problem_slug(response: httpx.Response) -> str:
    return str(response.json()["type"])


async def test_an_org_admin_cannot_escalate_through_a_grant_or_a_group(
    gateway: httpx.AsyncClient, admin_token: str, users: Any
) -> None:
    delegation = await _delegation(gateway, admin_token, users)

    # An Org admin may not hand out Org admin: the grant ceiling is below their own role.
    ceiling = await _grant(
        gateway,
        delegation.oa_token,
        delegation.tree.org,
        "user",
        delegation.dev.principal_id,
        "org_admin",
    )
    assert ceiling.status_code == 403, ceiling.text
    assert _problem_slug(ceiling) == "urn:dc:problem:grant-ceiling-exceeded"

    # Nor may they add themselves to a group to pick up what the group holds.
    own = await gateway.post(
        f"{GROUPS}/{delegation.group_id}/members",
        json={"username": delegation.oa.username},
        headers=_bearer(delegation.oa_token),
    )
    assert own.status_code == 403, own.text
    assert _problem_slug(own) == "urn:dc:problem:self-grant-forbidden"


async def test_a_stranger_sees_empty_lists_and_never_the_organization(
    gateway: httpx.AsyncClient, admin_token: str, users: Any
) -> None:
    delegation = await _delegation(gateway, admin_token, users)
    contract_id = await _dev_creates_a_contract(gateway, delegation)
    stranger = await users.create("stranger")
    token = await users.token(stranger)

    folders = await gateway.get(FOLDERS, headers=_bearer(token))
    assert folders.status_code == 200, folders.text
    assert folders.json()["items"] == []
    contracts = await gateway.get(CONTRACTS, headers=_bearer(token))
    assert contracts.status_code == 200, contracts.text
    assert contracts.json()["items"] == []

    # Direct probes are indistinguishable from "does not exist".
    assert (
        await gateway.get(f"{CONTRACTS}/{contract_id}", headers=_bearer(token))
    ).status_code == 404
    assert (
        await gateway.get(f"{FOLDERS}/{delegation.tree.org}", headers=_bearer(token))
    ).status_code == 404


async def test_a_policy_manager_reads_metadata_but_not_content_and_publishes_a_template(
    gateway: httpx.AsyncClient, admin_token: str, users: Any
) -> None:
    delegation = await _delegation(gateway, admin_token, users)
    contract_id = await _dev_creates_a_contract(gateway, delegation)
    pm = await users.create("pm")
    granted = await _grant(
        gateway, admin_token, delegation.tree.org, "user", pm.principal_id, "policy_manager"
    )
    assert granted.status_code == 201, granted.text
    pm_token = await users.token(pm)

    # Metadata yes, content no: the document is a plain 404.
    metadata = await gateway.get(f"{CONTRACTS}/{contract_id}", headers=_bearer(pm_token))
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["content_redacted"] is True
    document = await gateway.get(f"{CONTRACTS}/{contract_id}/document", headers=_bearer(pm_token))
    assert document.status_code == 404, document.text
    assert _problem_slug(document) == "urn:dc:problem:not-found"

    # The Policy Manager publishes a template at the organization...
    template = await gateway.post(
        f"/api/v1/contracts/folders/{delegation.tree.org}/templates",
        params={"name": f"Orders template {uuid.uuid4().hex[:6]}"},
        content=_contract_yaml(_new_odcs_id(), "Template Orders"),
        headers={**_bearer(pm_token), "Content-Type": "application/yaml"},
    )
    assert template.status_code == 201, template.text
    template_id = str(template.json()["template"]["id"])
    published = await gateway.post(
        f"/api/v1/contracts/templates/{template_id}/publish", headers=_bearer(pm_token)
    )
    assert published.status_code == 201, published.text

    # ...and the member in the team below finds it and starts a contract from it.
    available = await gateway.get(
        f"/api/v1/contracts/folders/{delegation.tree.team}/available-templates",
        headers=_bearer(delegation.dev_token),
    )
    assert available.status_code == 200, available.text
    assert template_id in [item["template_id"] for item in available.json()["items"]]
    odcs_id = _new_odcs_id()
    created = await gateway.post(
        f"/api/v1/contracts/folders/{delegation.tree.team}/contracts/from-template",
        json={"template_id": template_id, "odcs_id": odcs_id},
        headers=_bearer(delegation.dev_token),
    )
    assert created.status_code == 201, created.text
    contract = created.json()["contract"]
    assert contract["template"] == {"template_id": template_id, "version_no": 1}
    assert contract["odcs_id"] == odcs_id
    assert contract["folder_id"] == delegation.tree.team


async def test_a_consumer_cannot_see_a_draft_or_create_a_contract(
    gateway: httpx.AsyncClient, admin_token: str, users: Any
) -> None:
    delegation = await _delegation(gateway, admin_token, users)
    contract_id = await _dev_creates_a_contract(gateway, delegation)
    cs = await users.create("cs")
    granted = await _grant(
        gateway, admin_token, delegation.tree.team, "user", cs.principal_id, "consumer"
    )
    assert granted.status_code == 201, granted.text
    cs_token = await users.token(cs)

    draft = await gateway.get(f"{CONTRACTS}/{contract_id}", headers=_bearer(cs_token))
    assert draft.status_code == 404, draft.text
    created = await _create_contract(
        gateway, cs_token, delegation.tree.team, _contract_yaml(_new_odcs_id())
    )
    assert created.status_code == 403, created.text


async def test_block_inheritance_cuts_the_organization_grants_but_not_the_teams_own(
    gateway: httpx.AsyncClient, admin_token: str, users: Any
) -> None:
    delegation = await _delegation(gateway, admin_token, users)
    pm = await users.create("pm")
    granted = await _grant(
        gateway, admin_token, delegation.tree.org, "user", pm.principal_id, "policy_manager"
    )
    assert granted.status_code == 201, granted.text
    pm_token = await users.token(pm)
    first = await _dev_creates_a_contract(gateway, delegation)
    seen = await gateway.get(f"{CONTRACTS}/{first}", headers=_bearer(pm_token))
    assert seen.status_code == 200, seen.text  # inherited from the Organization grant

    blocked = await gateway.put(
        f"{FOLDERS}/{delegation.tree.team}/block-inheritance",
        json={"enabled": True},
        headers=_bearer(delegation.oa_token),
    )
    assert blocked.status_code == 200, blocked.text

    # The group grant made at the team itself keeps working (D-29)...
    second = await _dev_creates_a_contract(gateway, delegation)
    assert (
        await gateway.get(f"{CONTRACTS}/{second}", headers=_bearer(delegation.dev_token))
    ).status_code == 200
    # ...while the Organization-level Policy Manager no longer sees the team at all.
    for contract_id in (first, second):
        hidden = await gateway.get(f"{CONTRACTS}/{contract_id}", headers=_bearer(pm_token))
        assert hidden.status_code == 404, hidden.text
