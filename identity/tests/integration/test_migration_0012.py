"""Migration 0012 adds ``refresh_tokens.session_started_at`` and backfills it (quick 261120-0ye)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from dc_core.migrations import run_alembic
from dc_identity.migrate import SCRIPT_LOCATION, VERSION_TABLE

pytestmark = pytest.mark.integration

Sql = Callable[..., list[dict[str, Any]]]
BEFORE = "0011_platform_settings"


def _run(url: str, command: str, revision: str = "head") -> None:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=url,
        command=command,  # type: ignore[arg-type]
        revision=revision,
    )


def test_the_upgrade_backfills_the_session_start_from_created_at_and_the_downgrade_drops_it(
    fresh_db_url: str, sql: Sql
) -> None:
    _run(fresh_db_url, "upgrade", BEFORE)
    user, token = str(uuid.uuid4()), str(uuid.uuid4())
    sql(
        fresh_db_url,
        "INSERT INTO users (id, username, display_name, created_by, updated_by) "
        "VALUES (%s, 'old', 'Old', 'test', 'test')",
        user,
    )
    sql(
        fresh_db_url,
        "INSERT INTO refresh_tokens (id, user_id, family_id, token_hash, expires_at, created_at,"
        " created_by, updated_by) VALUES (%s, %s, %s, 'h', now() + interval '1 day',"
        " now() - interval '3 days', 'test', 'test')",
        token,
        user,
        str(uuid.uuid4()),
    )

    _run(fresh_db_url, "upgrade")

    row = sql(
        fresh_db_url,
        "SELECT created_at, session_started_at FROM refresh_tokens WHERE id = %s",
        token,
    )[0]
    assert row["session_started_at"] == row["created_at"]
    column = sql(
        fresh_db_url,
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'refresh_tokens' AND column_name = 'session_started_at'",
    )[0]
    assert column["is_nullable"] == "NO"
    assert "now()" in str(column["column_default"])

    _run(fresh_db_url, "downgrade", BEFORE)
    assert not sql(
        fresh_db_url,
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'refresh_tokens' AND column_name = 'session_started_at'",
    )
