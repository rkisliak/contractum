"""CONT-01, CONT-06, AUD-02 through the gateway: store a contract, fetch the identical bytes,
and find exactly one audit event for the create."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

FOLDERS = "/api/v1/identity/folders"


def _yaml_contract(odcs_id: str) -> bytes:
    # CRLF-free on purpose: the byte-identity claim is about everything the author wrote,
    # comments, key order, quoting and trailing spaces included.
    text = f"""# Owned by the e2e suite; comments must survive byte for byte.
kind: DataContract
id: {odcs_id}   # inline comment
apiVersion: v3.2.0
name:   E2E Orders
version: "1.0.0"
status: draft
domain: e2e
tags: [e2e, orders]
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


async def _folder(
    gateway: httpx.AsyncClient, headers: dict[str, str], parent: str, slug: str
) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent, "slug": slug, "display_name": slug},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def test_admin_stores_a_contract_through_the_gateway_and_fetches_the_same_bytes(
    gateway: httpx.AsyncClient,
    internal: Callable[[str], httpx.AsyncClient],
    admin_token: str,
    wait_for: Callable[..., Any],
) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=headers)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"

    run = uuid.uuid4().hex[:10]
    org = await _folder(gateway, headers, root["id"], f"e2e-{run}")
    project = await _folder(gateway, headers, org, "payments")
    team = await _folder(gateway, headers, project, "ingest")

    odcs_id = f"urn:datacontract:e2e:{uuid.uuid4()}"
    raw = _yaml_contract(odcs_id)
    created = await gateway.post(
        f"/api/v1/contracts/folders/{team}/contracts",
        content=raw,
        headers={**headers, "Content-Type": "application/yaml"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    contract_id = body["contract"]["id"]
    assert body["contract"]["odcs_id"] == odcs_id
    assert body["version"] == {"number": 1, "state": "draft", "deprecated": False}
    assert body["structural_validation"]["valid"] is True
    assert created.headers["location"] == f"/api/v1/contracts/contracts/{contract_id}"

    document = await gateway.get(
        f"/api/v1/contracts/contracts/{contract_id}/document", headers=headers
    )
    assert document.status_code == 200
    assert document.content == raw
    assert document.headers["content-type"] == "application/yaml"

    metadata = await gateway.get(f"/api/v1/contracts/contracts/{contract_id}", headers=headers)
    assert metadata.status_code == 200
    assert metadata.json()["organization_id"] == org

    audit = internal("audit")

    async def created_events() -> list[dict[str, Any]] | None:
        response = await audit.get(
            "/internal/v1/events", params={"aggregate_id": contract_id}, headers=headers
        )
        response.raise_for_status()
        events: list[dict[str, Any]] = response.json()
        return events or None

    events = await wait_for(created_events, 30.0, description="the contract.created audit event")
    assert [event["action"] for event in events] == ["contract.created"]
    event = events[0]
    assert event["actor"]["type"] == "user"
    assert "2026-11-09T10:00:00Z" not in str(event)  # no document content in the event
    assert event["after"]["content_sha256"] == metadata.json()["content_sha256"]
