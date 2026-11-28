"""Migration 0019 adds ``ix_contract_folder_id (folder_id, id)`` for the Folders tree (D-07).

The index is additive: the upgrade creates it, the downgrade drops it, and the models agree with
the migrations afterwards (``alembic check``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from dc_contracts.migrate import SCRIPT_LOCATION, VERSION_TABLE
from dc_core.migrations import run_alembic

pytestmark = pytest.mark.integration

Sql = Callable[..., list[dict[str, Any]]]
BEFORE = "0018_card_organization_nullable"
AFTER = "0019_contract_folder_id_index"
INDEX = "ix_contract_folder_id"


def _run(url: str, command: str, revision: str = "head") -> None:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=url,
        command=command,  # type: ignore[arg-type]
        revision=revision,
    )


def _definition(url: str, sql: Sql) -> str | None:
    rows = sql(
        url,
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'contract' AND indexname = %s",
        INDEX,
    )
    return str(rows[0]["indexdef"]) if rows else None


def test_the_upgrade_adds_the_folder_index_and_the_downgrade_drops_it(
    fresh_db_url: str, sql: Sql
) -> None:
    _run(fresh_db_url, "upgrade", BEFORE)
    assert _definition(fresh_db_url, sql) is None

    _run(fresh_db_url, "upgrade", AFTER)
    definition = _definition(fresh_db_url, sql)
    assert definition is not None
    assert "(folder_id, id)" in definition

    _run(fresh_db_url, "downgrade", BEFORE)
    assert _definition(fresh_db_url, sql) is None

    _run(fresh_db_url, "upgrade", "head")
    assert _definition(fresh_db_url, sql) is not None
    _run(fresh_db_url, "check", "head")
