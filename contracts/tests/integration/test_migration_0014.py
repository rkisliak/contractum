"""Migration 0014 (pin columns, replaced guard) rolls back and forward cleanly (D-132, RULE-07).

The proof is made on the guard's behaviour, not only on its text: after the downgrade a publish
that also changes a column outside the 0005 allow-list is refused again, and a version published
before the upgrade keeps NULL pins (nothing recorded what it was validated against, so no backfill).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from dc_contracts.migrate import SCRIPT_LOCATION, VERSION_TABLE
from dc_core.migrations import run_alembic

pytestmark = pytest.mark.integration

Sql = Callable[..., list[dict[str, Any]]]
BEFORE = "0013_card_webhook_secret"
AFTER = "0014_contract_version_pins"

PUBLISH = (
    "UPDATE contract_version SET state = 'published', semver = '1.0.0', semver_major = 1, "
    "semver_minor = 0, semver_patch = 0, semver_prerelease = '', published_at = now(), "
    "published_by = 'system:test'{extra} WHERE id = %s"
)


def _run(url: str, command: str, revision: str = "head") -> None:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=url,
        command=command,  # type: ignore[arg-type]
        revision=revision,
    )


def _seed_draft(url: str, sql: Sql) -> str:
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
        f"urn:dc:migration14:{contract}",
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
    return version


def _columns(url: str, sql: Sql) -> list[str]:
    rows = sql(
        url,
        "SELECT column_name FROM information_schema.columns WHERE table_name = "
        "'contract_version' AND column_name IN ('validation_run_id', 'pinned_rule_sets', "
        "'engine_version', 'schema_sha') ORDER BY column_name",
    )
    return [r["column_name"] for r in rows]


def test_a_version_published_before_the_upgrade_keeps_null_pins_and_stays_frozen(
    fresh_db_url: str, sql: Sql
) -> None:
    _run(fresh_db_url, "upgrade", BEFORE)
    version = _seed_draft(fresh_db_url, sql)
    sql(fresh_db_url, PUBLISH.format(extra=""), version)

    _run(fresh_db_url, "upgrade", AFTER)

    row = sql(
        fresh_db_url,
        "SELECT state, pinned_rule_sets, validation_run_id, engine_version, schema_sha "
        "FROM contract_version WHERE id = %s",
        version,
    )[0]
    assert row == {
        "state": "published",
        "pinned_rule_sets": None,
        "validation_run_id": None,
        "engine_version": None,
        "schema_sha": None,
    }
    # No backfill, and a published row cannot be given pins afterwards either.
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        sql(
            fresh_db_url,
            "UPDATE contract_version SET pinned_rule_sets = '[]'::jsonb WHERE id = %s",
            version,
        )
    # Later revisions add tables, so the models only match the database at head.
    _run(fresh_db_url, "upgrade", "head")
    _run(fresh_db_url, "check", "head")


def test_the_downgrade_restores_the_previous_guard_and_the_upgrade_installs_it_again(
    fresh_db_url: str, sql: Sql
) -> None:
    _run(fresh_db_url, "upgrade", AFTER)
    assert len(_columns(fresh_db_url, sql)) == 4

    # A publish may set the pin columns on 0014.
    on_head = _seed_draft(fresh_db_url, sql)
    sql(
        fresh_db_url,
        PUBLISH.format(extra=", validation_run_id = gen_random_uuid(), schema_sha = 'abc'"),
        on_head,
    )

    _run(fresh_db_url, "downgrade", BEFORE)
    assert _columns(fresh_db_url, sql) == []

    # On 0013 the pin columns are gone. Put a stand-in column back to prove which allow-list the
    # restored guard holds: a column it does not name must not change in the publish statement.
    sql(fresh_db_url, "ALTER TABLE contract_version ADD COLUMN validation_run_id uuid")
    on_0013 = _seed_draft(fresh_db_url, sql)
    with pytest.raises(psycopg.errors.RaiseException, match="publishing must not change"):
        sql(
            fresh_db_url,
            PUBLISH.format(extra=", validation_run_id = gen_random_uuid()"),
            on_0013,
        )
    sql(fresh_db_url, PUBLISH.format(extra=""), on_0013)
    sql(fresh_db_url, "ALTER TABLE contract_version DROP COLUMN validation_run_id")

    _run(fresh_db_url, "upgrade", AFTER)
    assert len(_columns(fresh_db_url, sql)) == 4
    again = _seed_draft(fresh_db_url, sql)
    sql(
        fresh_db_url,
        PUBLISH.format(extra=", validation_run_id = gen_random_uuid(), schema_sha = 'abc'"),
        again,
    )
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        sql(
            fresh_db_url,
            "UPDATE contract_version SET schema_sha = 'tampered' WHERE id = %s",
            again,
        )
    # Later revisions add tables, so the models only match the database at head.
    _run(fresh_db_url, "upgrade", "head")
    _run(fresh_db_url, "check", "head")
