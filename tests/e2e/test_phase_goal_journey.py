"""The phase goal, end to end through the gateway (ROADMAP Phase 1 success criteria 1 and 3).

"An engineer runs one command, logs in locally through the gateway, creates a team folder, stores
an ODCS contract in it and fetches it back unchanged. Every write is audited."

One journey: the admin (already logged in through https://traefik by the ``admin_token`` fixture)
builds Organization -> Project -> Team, stores a commented YAML contract, fetches the identical
bytes, finds the contract by list and by ``q`` search, and then reads the audit trail: exactly one
event per write, each with the admin as the typed actor.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest

pytestmark = pytest.mark.e2e

FOLDERS = "/api/v1/identity/folders"
CONTRACTS = "/api/v1/contracts/contracts"


def _journey_contract(odcs_id: str, name: str) -> bytes:
    # Everything the author wrote must come back unchanged: comments, key order, odd spacing,
    # quoting, and a timestamp-looking scalar that a YAML loader would otherwise retype.
    text = f"""# Phase-goal journey: comments must survive byte for byte.
kind: DataContract
id: {odcs_id}   # inline comment
apiVersion: v3.2.0
name:   {name}
version: "1.0.0"
status: draft
domain: journey
tags: [journey, orders]
description:
  purpose: Stored through the gateway and fetched back unchanged.
schema:
  - name: orders
    physicalType: table
    properties:
      - name: order_id
        logicalType: string
customProperties:
  - property: reviewedOn
    value: 2026-11-09T10:00:00Z   # must stay a string
"""
    return text.encode("utf-8")


async def _create_folder(
    gateway: httpx.AsyncClient, headers: dict[str, str], parent_id: str, slug: str
) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent_id, "slug": slug, "display_name": f"Journey {slug}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _audit_events(
    audit: httpx.AsyncClient, headers: dict[str, str], aggregate_id: str
) -> list[dict[str, Any]] | None:
    response = await audit.get(
        "/internal/v1/events", params={"aggregate_id": aggregate_id}, headers=headers
    )
    response.raise_for_status()
    events: list[dict[str, Any]] = response.json()
    return events or None


async def test_the_phase_goal_journey_works_through_the_gateway(
    gateway: httpx.AsyncClient,
    internal: Callable[[str], httpx.AsyncClient],
    admin_token: str,
    wait_for: Callable[..., Any],
) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}
    # The admin's platform id: namespace of the local issuer plus the token subject.
    admin_id = "local:" + str(jwt.decode(admin_token, options={"verify_signature": False})["sub"])

    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=headers)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"

    run = uuid.uuid4().hex[:10]
    org = await _create_folder(gateway, headers, root["id"], f"journey-{run}")
    project = await _create_folder(gateway, headers, org, "payments")
    team = await _create_folder(gateway, headers, project, "ingest")

    odcs_id = f"urn:datacontract:journey:{uuid.uuid4()}"
    name_fragment = f"Journey{run}"
    raw = _journey_contract(odcs_id, f"{name_fragment} Orders")
    created = await gateway.post(
        f"/api/v1/contracts/folders/{team}/contracts",
        content=raw,
        headers={**headers, "Content-Type": "application/yaml"},
    )
    assert created.status_code == 201, created.text
    contract_id = created.json()["contract"]["id"]
    assert created.json()["structural_validation"]["valid"] is True

    # Fetch it back unchanged: byte-identical document, matching metadata.
    document = await gateway.get(f"{CONTRACTS}/{contract_id}/document", headers=headers)
    assert document.status_code == 200
    assert document.content == raw
    metadata = await gateway.get(f"{CONTRACTS}/{contract_id}", headers=headers)
    assert metadata.status_code == 200
    assert metadata.json()["folder_id"] == team
    assert metadata.json()["organization_id"] == org
    assert metadata.json()["odcs_id"] == odcs_id
    # created_by is derived from the verified token, never from the client.
    assert metadata.json()["created_by"] == admin_id

    # Found by text search...
    searched = await gateway.get(CONTRACTS, params={"q": name_fragment}, headers=headers)
    assert searched.status_code == 200, searched.text
    assert [item["id"] for item in searched.json()["items"]] == [contract_id]
    # ...and by listing the organization's subtree.
    listed = await gateway.get(
        CONTRACTS, params={"folder_id": org, "include_subtree": "true"}, headers=headers
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [contract_id]

    # Audit: one event per write, the admin as the typed actor, no document content.
    audit = internal("audit")
    for folder_id, expected_parent in ((org, root["id"]), (project, org), (team, project)):
        events = await wait_for(
            lambda folder_id=folder_id: _audit_events(audit, headers, folder_id),
            30.0,
            description=f"the folder.created event for {folder_id}",
        )
        assert [event["action"] for event in events] == ["folder.created"], events
        assert events[0]["actor"]["type"] == "user"
        assert events[0]["actor"]["id"] == admin_id
        assert events[0]["after"]["parent_id"] == expected_parent
    contract_events = await wait_for(
        lambda: _audit_events(audit, headers, contract_id),
        30.0,
        description="the contract.created event",
    )
    assert [event["action"] for event in contract_events] == ["contract.created"], contract_events
    assert contract_events[0]["actor"]["type"] == "user"
    assert contract_events[0]["actor"]["id"] == admin_id
    assert contract_events[0]["scope_path"] == [root["id"], org, project, team]
    assert "2026-11-09T10:00:00Z" not in str(contract_events[0])
