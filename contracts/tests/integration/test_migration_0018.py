"""Migration 0018 makes ``subscriber_card.organization_id`` nullable for cards at ROOT.

Quick 261119-wo3. The upgrade keeps every existing card; the downgrade restores NOT NULL and
refuses, by name, while a card owned by the ROOT folder exists.
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
BEFORE = "0017_folder_purge_guards"
AFTER = "0018_card_organization_nullable"


def _run(url: str, command: str, revision: str = "head") -> None:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=url,
        command=command,  # type: ignore[arg-type]
        revision=revision,
    )


def _nullable(url: str, sql: Sql) -> str:
    rows = sql(
        url,
        "SELECT is_nullable FROM information_schema.columns WHERE table_name = "
        "'subscriber_card' AND column_name = 'organization_id'",
    )
    return str(rows[0]["is_nullable"])


def _insert_card(url: str, sql: Sql, organization_id: str | None) -> None:
    folder = str(uuid.uuid4())
    sql(
        url,
        "INSERT INTO subscriber_card (id, folder_id, ancestor_ids, organization_id, name, "
        "description, created_by, updated_by) VALUES (%s, %s, %s, %s, 'c', 'd', "
        "'system:test', 'system:test')",
        str(uuid.uuid4()),
        folder,
        [folder],
        organization_id,
    )


def test_the_upgrade_allows_a_card_without_an_organization_and_the_downgrade_restores_it(
    fresh_db_url: str, sql: Sql
) -> None:
    _run(fresh_db_url, "upgrade", BEFORE)
    assert _nullable(fresh_db_url, sql) == "NO"
    with pytest.raises(psycopg.errors.NotNullViolation):
        _insert_card(fresh_db_url, sql, None)

    _run(fresh_db_url, "upgrade", AFTER)
    assert _nullable(fresh_db_url, sql) == "YES"
    _insert_card(fresh_db_url, sql, str(uuid.uuid4()))
    _run(fresh_db_url, "downgrade", BEFORE)
    assert _nullable(fresh_db_url, sql) == "NO"
    _run(fresh_db_url, "upgrade", "head")
    _run(fresh_db_url, "check", "head")


def test_the_downgrade_refuses_while_a_root_card_exists(fresh_db_url: str, sql: Sql) -> None:
    _run(fresh_db_url, "upgrade", AFTER)
    _insert_card(fresh_db_url, sql, None)
    with pytest.raises(Exception, match="owned by the ROOT folder"):
        _run(fresh_db_url, "downgrade", BEFORE)
    assert _nullable(fresh_db_url, sql) == "YES"
