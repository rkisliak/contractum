"""Stored validation runs are immutable and carry no document value (D-133, T-5-28, T-5-29)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from dc_odcs import StructuralReport, validate_structure
from dc_rules.model import RuleDef
from dc_rules.protocol import FindingWire, UnitResult
from dc_validation.report import RuleBinding, build_report
from dc_validation.resolution import ResolvedRuleSet
from psycopg import errors
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.integration


def _rule(key: str = "unique-property-names") -> RuleDef:
    return RuleDef.model_validate(
        {
            "key": key,
            "target": "$.schema[*]",
            "condition": "true",
            "severity": "error",
            "message": "Property names must be unique.",
            "fixtures": {"pass": [], "fail": []},
        }
    )


def _resolved() -> ResolvedRuleSet:
    return ResolvedRuleSet(
        rule_set_id=uuid.uuid4(),
        rule_set_version_id=uuid.uuid4(),
        name="Org naming",
        scope="folder",
        folder_id=uuid.uuid4(),
        folder_kind="organization",
        depth=1,
        version_no=1,
        dsl_version="cel-1",
        source="mandatory",
        mode="enforce",
        enforce_at=None,
    )


@pytest.fixture
def conn(
    fresh_db_url: str, migrate_validation: Callable[..., None]
) -> Iterator[psycopg.Connection]:
    migrate_validation()
    url = fresh_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as connection:
        yield connection


def _report_json() -> dict[str, object]:
    entry = _resolved()
    report = build_report(
        StructuralReport(valid=True, error_count=0, errors=()),
        [entry],
        {0: RuleBinding(entry, _rule())},
        [
            UnitResult(
                id=0,
                outcome="fail",
                findings=[FindingWire(outcome="fail", pointer="/schema/0", path="$.schema[0]")],
            )
        ],
    )
    return report.model_dump(mode="json")


def insert_run(conn: psycopg.Connection, report: dict[str, object]) -> uuid.UUID:
    row = conn.execute(
        "INSERT INTO validation_run (mode, contract_id, draft_id, folder_id, content_sha256,"
        " resolved, engine_version, dsl_version, schema_sha, report, publishable, requested_by)"
        " VALUES ('publish', %s, %s, %s, %s, %s, '0.1.4', 'cel-1', %s, %s, false, 'test')"
        " RETURNING id",
        (
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            "a" * 64,
            Jsonb([]),
            "b" * 64,
            Jsonb(report),
        ),
    ).fetchone()
    assert row is not None
    run_id: uuid.UUID = row[0]
    return run_id


def test_a_run_is_stored_and_read_back_unchanged(conn: psycopg.Connection) -> None:
    report = _report_json()
    run_id = insert_run(conn, report)

    row = conn.execute(
        "SELECT report, publishable, created_at FROM validation_run WHERE id = %s", (run_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == json.loads(json.dumps(report))
    assert row[1] is False
    assert row[2] is not None


def test_a_stored_run_cannot_be_updated_or_deleted(conn: psycopg.Connection) -> None:
    run_id = insert_run(conn, _report_json())

    with pytest.raises(errors.RaiseException, match="immutable"):
        conn.execute("UPDATE validation_run SET publishable = true WHERE id = %s", (run_id,))
    with pytest.raises(errors.RaiseException, match="cannot be deleted"):
        conn.execute("DELETE FROM validation_run WHERE id = %s", (run_id,))
    assert conn.execute("SELECT count(*) FROM validation_run").fetchone() == (1,)


def test_an_unknown_mode_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(errors.CheckViolation):
        conn.execute(
            "INSERT INTO validation_run (mode, contract_id, draft_id, folder_id, content_sha256,"
            " resolved, engine_version, dsl_version, schema_sha, report, publishable,"
            " requested_by) VALUES ('adhoc', gen_random_uuid(), gen_random_uuid(),"
            " gen_random_uuid(), 'a', '[]', 'e', 'cel-1', 's', '{}', false, 'test')"
        )


def test_a_stored_run_contains_no_document_value(conn: psycopg.Connection) -> None:
    secret_value, secret_key = "SECRET-VALUE-123", "SECRET-KEY-9"
    document = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": 5,
        "version": "1.0.0",
        "status": "active",
        secret_key: "x",
        "schema": [
            {
                "name": "t",
                "properties": [
                    {
                        "name": "a",
                        "logicalType": secret_value,
                        "logicalTypeOptions": {"maxLength": secret_value},
                    }
                ],
            }
        ],
    }
    structural = validate_structure(document, "v3.2.0")
    assert any(secret_value in e.message for e in structural.errors)  # the library does leak
    entry = _resolved()
    report = build_report(
        structural,
        [entry],
        {0: RuleBinding(entry, _rule())},
        [UnitResult(id=0, outcome="error", error_code="invalid_argument")],
    ).model_dump(mode="json")

    run_id = insert_run(conn, report)

    row = conn.execute(
        "SELECT report::text, resolved::text FROM validation_run WHERE id = %s", (run_id,)
    ).fetchone()
    assert row is not None
    assert secret_value not in row[0]
    # Quick 261119-wk8 (deliberate): an unknown-property finding names the offending KEY (never a
    # value) so authors can find it; the key appears only inside that fixed sentence.
    assert row[0].count(secret_key) == row[0].count(f"`{secret_key}`") == 1
    assert "The object has properties the schema does not allow: `SECRET-KEY-9`." in row[0]
    assert '"keyword"' in row[0]
