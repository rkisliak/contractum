"""AUD-01: every entity table carries server-populated audit columns (created/updated by/at).

``created_by`` / ``updated_by`` are NOT NULL on every entity table of every service, filled from
the acting principal or a named system actor, and never from the client. The outbox table is the
audit record itself (``actor`` + ``occurred_at``) and is deliberately not an entity table.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

pytestmark = pytest.mark.integration

ENTITY_TABLES = {
    "identity": [
        "users",
        "bootstrap_state",
        "refresh_tokens",
        "login_throttle",
        "folder",
        "role_assignments",
    ],
    "contracts": ["contract", "contract_version"],
    "audit": ["audit_event", "audit_dead_letter"],
}
AUDIT_COLUMNS = ("created_by", "updated_by", "created_at", "updated_at")
FOLDERS = "/api/v1/identity/folders"


def test_every_entity_table_has_not_null_audit_columns(
    migrated_databases: Any, sql: Callable[..., list[dict[str, Any]]]
) -> None:
    problems: list[str] = []
    for service, tables in ENTITY_TABLES.items():
        db_url = getattr(migrated_databases, service)
        columns = sql(
            db_url,
            "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public'",
        )
        by_table: dict[str, dict[str, str]] = {}
        for row in columns:
            by_table.setdefault(row["table_name"], {})[row["column_name"]] = row["is_nullable"]
        for table in tables:
            present = by_table.get(table)
            if present is None:
                problems.append(f"{service}.{table}: table missing")
                continue
            for column in AUDIT_COLUMNS:
                if column not in present:
                    problems.append(f"{service}.{table}.{column}: column missing")
                elif present[column] != "NO":
                    problems.append(f"{service}.{table}.{column}: nullable")
    assert problems == []


async def test_audit_columns_are_populated_from_the_acting_principal_or_a_system_actor(
    completed_flow: Any,
    replay_to_audit: Callable[[Any], Any],
    sql: Callable[..., list[dict[str, Any]]],
) -> None:
    flow = completed_flow
    admin = f"local:{flow.admin_id}"
    identity, contracts, audit = (
        flow.databases.identity,
        flow.databases.contracts,
        flow.databases.audit,
    )
    await replay_to_audit(flow)

    users = sql(identity, "SELECT created_by, updated_by FROM users WHERE username = 'admin'")
    assert users == [{"created_by": "system:bootstrap", "updated_by": admin}]

    folders = sql(identity, "SELECT kind, created_by, updated_by FROM folder ORDER BY path")
    assert [f["kind"] for f in folders] == ["root", "organization", "project", "team"]
    assert {(f["created_by"], f["updated_by"]) for f in folders if f["kind"] != "root"} == {
        (admin, admin)
    }
    assert folders[0]["created_by"] == "system:bootstrap"

    for table in ("contract", "contract_version"):
        rows = sql(contracts, f"SELECT created_by, updated_by FROM {table}")  # noqa: S608
        assert rows == [{"created_by": admin, "updated_by": admin}], table

    throttle = sql(identity, "SELECT created_by, updated_by FROM login_throttle")
    assert throttle, "the failed login in the flow must have left a throttle row"
    assert {(r["created_by"], r["updated_by"]) for r in throttle} == {
        ("system:login-throttle", "system:login-throttle")
    }

    events = sql(audit, "SELECT created_by, updated_by FROM audit_event")
    assert events
    assert {(e["created_by"], e["updated_by"]) for e in events} == {
        ("system:audit-ingest", "system:audit-ingest")
    }

    # Server-side timestamps: set, and never earlier than creation.
    stamps = sql(
        identity,
        "SELECT count(*) AS n FROM folder WHERE created_at IS NULL OR updated_at < created_at",
    )
    assert stamps[0]["n"] == 0


async def test_a_client_cannot_supply_audit_columns(completed_flow: Any) -> None:
    flow = completed_flow
    response = await flow.identity.post(
        FOLDERS,
        json={
            "parent_id": flow.root_id,
            "slug": "forged-audit",
            "display_name": "Forged",
            "created_by": "local:somebody-else",
        },
        headers={"Authorization": f"Bearer {flow.admin_token}"},
    )
    assert response.status_code == 422, response.text
    assert "created_by" in response.text
