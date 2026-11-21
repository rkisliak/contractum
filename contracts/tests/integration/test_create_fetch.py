"""Tracer: an admin stores an ODCS YAML contract in a team folder and fetches the same bytes."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def v32_yaml(odcs_id: str | None = None) -> bytes:
    raw = (FIXTURES / "valid-v3.2.0.yaml").read_bytes()
    if odcs_id is not None:
        raw = raw.replace(b"urn:datacontract:payments:orders", odcs_id.encode())
    return raw


async def test_admin_stores_a_yaml_contract_and_fetches_the_identical_bytes(
    make_stack: Callable[..., AbstractAsyncContextManager[Any]], sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        raw = v32_yaml()
        created = await stack.post(chain.team, raw, "application/yaml")
        assert created.status_code == 201, created.text
        body = created.json()
        contract = body["contract"]
        contract_id = contract["id"]
        assert str(uuid.UUID(contract_id).version) == "7"
        assert created.headers["location"] == f"/api/v1/contracts/contracts/{contract_id}"
        assert contract["odcs_id"] == "urn:datacontract:payments:orders"
        assert body["version"] == {"number": 1, "state": "draft"}
        assert body["structural_validation"]["valid"] is True
        # folder facts come from identity, never from the client
        assert contract["folder_id"] == chain.team
        assert contract["organization_id"] == chain.org

        document = await stack.client.get(
            f"/api/v1/contracts/contracts/{contract_id}/document", headers=bearer(stack.admin_token)
        )
        assert document.status_code == 200
        assert document.content == raw  # comments, key order and the inline comment survive
        assert document.headers["content-type"] == "application/yaml"
        assert document.headers["x-content-type-options"] == "nosniff"
        assert document.headers["content-disposition"] == "inline"

        metadata = await stack.client.get(
            f"/api/v1/contracts/contracts/{contract_id}", headers=bearer(stack.admin_token)
        )
        assert metadata.status_code == 200
        meta = metadata.json()
        for key in (
            "id",
            "folder_id",
            "organization_id",
            "odcs_id",
            "api_version",
            "name",
            "domain",
            "status",
            "tags",
            "media_type",
            "content_sha256",
            "size_bytes",
            "version",
            "created_by",
            "created_at",
            "updated_by",
            "updated_at",
        ):
            assert key in meta, key
        assert meta["organization_id"] == chain.org
        assert meta["api_version"] == "v3.2.0"
        assert meta["name"] == "Payments Orders"
        assert meta["tags"] == ["payments", "orders", "pii-free"]
        assert meta["size_bytes"] == len(raw)
        assert meta["created_by"].startswith("local:")
        assert meta == contract  # the create response embeds the same metadata
        assert "kind" not in meta and "schema" not in meta  # metadata never injected/duplicated

        events = sql(
            stack.contracts_db_url,
            "SELECT action, aggregate_type, aggregate_id, after FROM outbox",
        )
        assert [e["action"] for e in events] == ["contract.created"]
        after = events[0]["after"]
        assert after["content_sha256"] == meta["content_sha256"]
        assert after["size_bytes"] == len(raw)
        assert after["id"] == contract_id
        assert "cc-4711" not in str(events[0])  # customProperties values never reach the event

        stored = sql(
            stack.contracts_db_url,
            "SELECT raw_document, document -> 'customProperties' AS props FROM contract_version",
        )
        assert bytes(stored[0]["raw_document"]) == raw
        assert stored[0]["props"][1]["value"] == "2026-11-09T10:00:00Z"  # a string, not a datetime


async def test_json_v310_contract_is_accepted_with_its_own_media_type(
    make_stack: Callable[..., AbstractAsyncContextManager[Any]],
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        raw = (FIXTURES / "valid-v3.1.0.json").read_bytes()
        created = await stack.post(chain.team, raw, "application/json")
        assert created.status_code == 201, created.text
        contract_id = created.json()["contract"]["id"]
        document = await stack.client.get(
            f"/api/v1/contracts/contracts/{contract_id}/document", headers=bearer(stack.admin_token)
        )
        assert document.content == raw
        assert document.headers["content-type"] == "application/json"


def test_migration_matches_the_models_and_downgrades_cleanly(fresh_db_url: str) -> None:
    from dc_contracts.migrate import SCRIPT_LOCATION, VERSION_TABLE
    from dc_core.migrations import run_alembic

    for command, revision in (("upgrade", "head"), ("check", "head"), ("downgrade", "base")):
        run_alembic(
            script_location=SCRIPT_LOCATION,
            version_table=VERSION_TABLE,
            url=fresh_db_url,
            command=command,  # type: ignore[arg-type]
            revision=revision,
        )


async def test_a_huge_contract_name_is_truncated_in_the_audit_event(
    make_stack: Callable[..., AbstractAsyncContextManager[Any]], sql: Callable[..., Any]
) -> None:
    """WR-01: free text in the event snapshot is bounded so one event cannot stall the relay."""
    import json

    document = json.loads((FIXTURES / "valid-v3.1.0.json").read_bytes())
    document["name"] = "n" * 20_000
    raw = json.dumps(document).encode()
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.post(chain.team, raw, "application/json")
        assert created.status_code == 201, created.text
        events = sql(stack.contracts_db_url, "SELECT after FROM outbox")
        assert len(events) == 1
        assert events[0]["after"]["name"] == "n" * 200
        assert len(json.dumps(events[0]["after"])) < 2_000
