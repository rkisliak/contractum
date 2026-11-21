"""Migration 0008 adds the ``notifier`` role; the downgrade refuses while one is granted."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from dc_core.migrations import run_alembic
from dc_identity.migrate import SCRIPT_LOCATION, VERSION_TABLE

pytestmark = pytest.mark.integration

Sql = Callable[..., list[dict[str, Any]]]
BEFORE = "0007_session_cutoff"
NOTIFIER = "0008_notifier_role"


def _run(url: str, command: str, revision: str = "head") -> None:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=url,
        command=command,  # type: ignore[arg-type]
        revision=revision,
    )


def _version(url: str, sql: Sql) -> str:
    rows = sql(url, "SELECT version_num FROM alembic_version_identity")
    return str(rows[0]["version_num"])


def test_a_notifier_grant_blocks_the_downgrade_until_it_is_removed(
    fresh_db_url: str, sql: Sql
) -> None:
    _run(fresh_db_url, "upgrade", NOTIFIER)
    # Bootstrap (not a migration) creates ROOT, so the test plants one.
    root = sql(
        fresh_db_url,
        "INSERT INTO folder (kind, slug, display_name, path, parent_id, created_by, updated_by) "
        "VALUES ('root', 'root', 'Root', 'root', NULL, 'test', 'test') RETURNING id",
    )[0]["id"]
    grant = sql(
        fresh_db_url,
        "INSERT INTO role_assignments (folder_id, principal_type, principal_id, role, "
        "created_by, updated_by) VALUES (%s, 'service_account', %s, 'notifier', 'test', 'test') "
        "RETURNING id",
        root,
        str(uuid.uuid4()),
    )[0]["id"]

    with pytest.raises(RuntimeError, match="remove notifier grants before downgrading"):
        _run(fresh_db_url, "downgrade", BEFORE)
    assert _version(fresh_db_url, sql) == NOTIFIER

    sql(fresh_db_url, "DELETE FROM role_assignments WHERE id = %s", grant)
    _run(fresh_db_url, "downgrade", BEFORE)
    assert _version(fresh_db_url, sql) == BEFORE
    # Back at 0007 the role check no longer admits the notifier role.
    check = sql(
        fresh_db_url,
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = 'role_assignments'::regclass AND conname LIKE %s",
        "%role_values",
    )[0]["def"]
    assert "'notifier'" not in check
