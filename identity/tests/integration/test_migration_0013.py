"""Migration 0013 adds ``refresh_tokens.client_summary`` and ``access_jti`` (quick 261120-s0p)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from dc_core.migrations import run_alembic
from dc_identity.migrate import SCRIPT_LOCATION, VERSION_TABLE

pytestmark = pytest.mark.integration

Sql = Callable[..., list[dict[str, Any]]]
BEFORE = "0012_refresh_session_start"


def _run(url: str, command: str, revision: str = "head") -> None:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=url,
        command=command,  # type: ignore[arg-type]
        revision=revision,
    )


def _columns(url: str, sql: Sql) -> set[str]:
    rows = sql(
        url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'refresh_tokens' AND column_name IN ('client_summary', 'access_jti')",
    )
    return {r["column_name"] for r in rows}


def test_the_upgrade_adds_both_nullable_columns_and_the_downgrade_drops_them(
    fresh_db_url: str, sql: Sql
) -> None:
    _run(fresh_db_url, "upgrade", BEFORE)
    assert _columns(fresh_db_url, sql) == set()

    _run(fresh_db_url, "upgrade")

    assert _columns(fresh_db_url, sql) == {"client_summary", "access_jti"}
    nullable = sql(
        fresh_db_url,
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'refresh_tokens' AND column_name IN ('client_summary', 'access_jti')",
    )
    assert {r["is_nullable"] for r in nullable} == {"YES"}
    # the summary is bounded by a check constraint
    checks = sql(
        fresh_db_url,
        "SELECT conname FROM pg_constraint WHERE conrelid = 'refresh_tokens'::regclass "
        "AND contype = 'c'",
    )
    assert any("client_summary" in r["conname"] for r in checks)

    _run(fresh_db_url, "downgrade", BEFORE)
    assert _columns(fresh_db_url, sql) == set()
