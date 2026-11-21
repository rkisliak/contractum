"""The audit query's pure parts: cursor codec, filter validation and the action rule (D-166)."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from dc_audit.query import (
    AuditFilters,
    build_statement,
    decode_cursor,
    encode_cursor,
    make_filters,
)
from dc_core.problems import ProblemError
from sqlalchemy import true
from sqlalchemy.dialects import postgresql

pytestmark = pytest.mark.unit

AT = datetime(2026, 11, 9, 12, 30, 15, 123456, tzinfo=UTC)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _sql(filters: AuditFilters, cursor: tuple[datetime, int] | None = None) -> str:
    stmt = build_statement(true(), filters, 50, cursor=cursor)
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    return sql.replace("%%", "%")  # the psycopg dialect doubles % for its pyformat paramstyle


def _assert_422(exc: pytest.ExceptionInfo[ProblemError], pointer: str) -> None:
    assert exc.value.status == 422
    assert exc.value.slug == "validation-error"
    assert exc.value.extensions["errors"][0]["pointer"] == pointer


# --- cursor ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "row_id"),
    [
        (AT, 1),
        (AT.replace(microsecond=0), 9_223_372_036_854_775_807),
        (datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2))), 42),
    ],
)
def test_a_cursor_round_trips(at: datetime, row_id: int) -> None:
    raw = encode_cursor(at, row_id)
    assert "=" not in raw
    decoded_at, decoded_id = decode_cursor(raw)
    assert decoded_at == at
    assert decoded_at.utcoffset() == timedelta(0)
    assert decoded_id == row_id


@pytest.mark.parametrize(
    "raw",
    [
        encode_cursor(AT, 7) + "=",  # padding
        _b64(f"{AT.isoformat()}|7|extra"),  # extra field
        _b64("2026-11-09T14:30:15+02:00|7"),  # non-UTC offset
        _b64("2026-11-09T12:30:15|7"),  # naive time
        _b64(f"{AT.isoformat()}|seven"),  # non-integer id
        _b64(f"{AT.isoformat()}|+7"),  # non-canonical integer
        _b64(f"{AT.isoformat()}| 7"),
        _b64(f"{AT.isoformat()}"),  # missing id
        _b64("yesterday|7"),
        "not base64!",
        "",
        "été",
    ],
)
def test_a_malformed_or_non_canonical_cursor_is_422(raw: str) -> None:
    with pytest.raises(ProblemError) as exc:
        decode_cursor(raw)
    _assert_422(exc, "/query/cursor")


# --- filters ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["aggregate_type", "aggregate_id", "actor_id", "service", "action"]
)
def test_a_nul_character_in_a_text_filter_is_422(name: str) -> None:
    with pytest.raises(ProblemError) as exc:
        make_filters(**{name: "a\x00b"})
    _assert_422(exc, f"/query/{name}")


@pytest.mark.parametrize("name", ["from_", "to"])
def test_a_naive_time_bound_is_422(name: str) -> None:
    with pytest.raises(ProblemError) as exc:
        make_filters(**{name: datetime(2026, 11, 9, 12, 0)})
    _assert_422(exc, "/query/" + name.rstrip("_"))


def test_from_after_to_is_422_and_from_equal_to_is_allowed() -> None:
    with pytest.raises(ProblemError) as exc:
        make_filters(from_=AT, to=AT - timedelta(microseconds=1))
    _assert_422(exc, "/query/from")
    assert make_filters(from_=AT, to=AT).from_ == AT


def test_an_action_with_a_trailing_dot_is_an_escaped_like_prefix() -> None:
    sql = _sql(make_filters(action="con_tract%.v\\ersion."))
    assert "audit_event.action LIKE 'con\\_tract\\%.v\\\\ersion.%' ESCAPE '\\'" in sql


def test_an_action_without_a_trailing_dot_is_exact() -> None:
    sql = _sql(make_filters(action="contract.created"))
    assert "audit_event.action = 'contract.created'" in sql
    assert "LIKE" not in sql


def test_the_filters_combine_with_and_and_use_the_index_expressions() -> None:
    sql = _sql(
        make_filters(
            aggregate_type="contract",
            aggregate_id="c-1",
            actor_id="sa:" + str(uuid.UUID(int=1)),
            service="contracts",
            category="access",
            from_=AT,
            to=AT + timedelta(days=1),
        )
    )
    for fragment in (
        "audit_event.aggregate_type = 'contract'",
        "audit_event.aggregate_id = 'c-1'",
        "(audit_event.actor ->> 'id') = 'sa:00000000-0000-0000-0000-000000000001'",
        "audit_event.service = 'contracts'",
        "audit_event.category = 'access'",
        "audit_event.occurred_at >= '2026-11-09 12:30:15.123456+00:00'",
        "audit_event.occurred_at < '2026-11-10 12:30:15.123456+00:00'",
        "ORDER BY audit_event.occurred_at DESC, audit_event.id DESC",
        "LIMIT 51",
    ):
        assert fragment in sql, fragment


def test_the_cursor_is_a_row_comparison_on_occurred_at_and_id() -> None:
    sql = _sql(AuditFilters(), cursor=(AT, 7))
    assert (
        "(audit_event.occurred_at, audit_event.id) < ('2026-11-09 12:30:15.123456+00:00', 7)" in sql
    )
