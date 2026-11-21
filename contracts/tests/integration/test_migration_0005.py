"""Migration 0005 upgrades existing Phase 1/2 contracts unchanged and downgrades cleanly (D-40)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from dc_contracts.migrate import SCRIPT_LOCATION, VERSION_TABLE
from dc_core.migrations import run_alembic

pytestmark = pytest.mark.integration

Sql = Callable[..., list[dict[str, Any]]]
BEFORE = "0004_draft_revision"


def _run(url: str, command: str, revision: str) -> None:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=url,
        command=command,  # type: ignore[arg-type]
        revision=revision,
    )


def _seed(url: str, sql: Sql) -> tuple[str, str]:
    contract = str(uuid.uuid4())
    version = str(uuid.uuid4())
    folder = str(uuid.uuid4())
    sql(
        url,
        "INSERT INTO contract (id, folder_id, ancestor_ids, organization_id, odcs_id, "
        "created_by, updated_by) VALUES (%s, %s, %s, %s, %s, 'system:test', 'system:test')",
        contract,
        folder,
        [folder],
        folder,
        f"urn:dc:migration:{contract}",
    )
    sql(
        url,
        "INSERT INTO contract_version (id, contract_id, odcs_api_version, raw_document, "
        "media_type, content_sha256, size_bytes, document, name, created_by, updated_by) "
        "VALUES (%s, %s, 'v3.2.0', %s, 'application/yaml', 'abc', 3, '{}'::jsonb, 'Orders', "
        "'system:test', 'system:test')",
        version,
        contract,
        b"x: 1",
    )
    return contract, version


def test_existing_contracts_upgrade_as_untouched_drafts_and_the_migration_reverses(
    fresh_db_url: str, sql: Sql
) -> None:
    _run(fresh_db_url, "upgrade", BEFORE)
    contract, version = _seed(fresh_db_url, sql)

    _run(fresh_db_url, "upgrade", "head")
    row = sql(fresh_db_url, "SELECT * FROM contract_version WHERE id = %s", version)[0]
    assert row["state"] == "draft"
    assert row["revision"] == 1
    assert row["is_latest"] is False
    assert row["semver"] is None
    assert row["semver_major"] is None
    assert row["published_at"] is None
    parent = sql(fresh_db_url, "SELECT * FROM contract WHERE id = %s", contract)[0]
    assert parent["latest_version_id"] is None
    assert parent["version_counter"] == 1

    _run(fresh_db_url, "downgrade", BEFORE)
    assert sql(fresh_db_url, "SELECT id FROM contract_version WHERE id = %s", version)
    _run(fresh_db_url, "upgrade", "head")
    assert sql(fresh_db_url, "SELECT state FROM contract_version WHERE id = %s", version) == [
        {"state": "draft"}
    ]
    _run(fresh_db_url, "check", "head")


def test_version_counter_starts_at_the_highest_existing_version_number(
    fresh_db_url: str, sql: Sql
) -> None:
    _run(fresh_db_url, "upgrade", BEFORE)
    contract, _version = _seed(fresh_db_url, sql)
    sql(
        fresh_db_url,
        "INSERT INTO contract_version (id, contract_id, version_no, odcs_api_version, "
        "raw_document, media_type, content_sha256, size_bytes, document, created_by, updated_by) "
        "VALUES (%s, %s, 4, 'v3.2.0', %s, 'application/yaml', 'def', 3, '{}'::jsonb, "
        "'system:test', 'system:test')",
        str(uuid.uuid4()),
        contract,
        b"x: 2",
    )
    _run(fresh_db_url, "upgrade", "head")
    counter = sql(fresh_db_url, "SELECT version_counter FROM contract WHERE id = %s", contract)
    assert counter == [{"version_counter": 4}]
