"""The audit migrations downgrade to an empty schema and upgrade back without drift."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from dc_audit.migrate import SCRIPT_LOCATION, VERSION_TABLE
from dc_core.migrations import run_alembic

pytestmark = pytest.mark.integration

AUDIT_TABLES = {"audit_event", "audit_dead_letter"}


def _alembic(url: str, command: str, revision: str = "head") -> None:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=url,
        command=command,  # type: ignore[arg-type]
        revision=revision,
    )


def _tables(url: str) -> set[str]:
    engine = sa.create_engine(url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_downgrade_to_base_drops_every_audit_table_and_upgrade_restores_them(
    fresh_db_url: str,
) -> None:
    _alembic(fresh_db_url, "upgrade")
    assert _tables(fresh_db_url) >= AUDIT_TABLES

    _alembic(fresh_db_url, "downgrade", "base")
    assert not _tables(fresh_db_url) & AUDIT_TABLES

    _alembic(fresh_db_url, "upgrade")
    assert _tables(fresh_db_url) >= AUDIT_TABLES
    # The models and the migrated schema agree after the round trip (raises on drift).
    _alembic(fresh_db_url, "check")
