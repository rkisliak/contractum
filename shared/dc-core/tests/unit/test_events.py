"""Subject naming and the event envelope (D-21 transport seam inputs)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from dc_core.events import (
    ACCESS_STREAM,
    AUDIT_STREAM,
    SUBJECT_TOKEN,
    JetStreamPublisher,
    build_access_envelope,
    build_envelope,
    encode_envelope,
    subject_for,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("service", "aggregate_type", "action", "expected"),
    [
        ("probe", "widget", "widget.created", "audit.probe.widget.widget.created"),
        (
            "identity",
            "user",
            "user.password_rotated",
            "audit.identity.user.user.password_rotated",
        ),
        ("probe", "widget", "created", "audit.probe.widget.created"),
        ("identity", "system", "system.bootstrapped", "audit.identity.system.system.bootstrapped"),
        ("identity", "folder", "folder.created", "audit.identity.folder.folder.created"),
        ("identity", "folder", "folder.renamed", "audit.identity.folder.folder.renamed"),
        ("identity", "folder", "folder.archived", "audit.identity.folder.folder.archived"),
        ("identity", "folder", "folder.unarchived", "audit.identity.folder.folder.unarchived"),
        ("contracts", "contract", "contract.created", "audit.contracts.contract.contract.created"),
        ("a-b_c", "x-1", "A-9_z.b", "audit.a-b_c.x-1.A-9_z.b"),
    ],
)
def test_subject_for_accepts_every_action_producers_emit(
    service: str, aggregate_type: str, action: str, expected: str
) -> None:
    assert subject_for(service, aggregate_type, action) == expected


@pytest.mark.parametrize(
    "action", [".x", "x.", "a..b", "", "a b", "a.*", "a.>", "*", ">", "a\n", "a/b", "ä"]
)
def test_subject_for_rejects_bad_actions(action: str) -> None:
    with pytest.raises(ValueError, match="action"):
        subject_for("probe", "widget", action)


@pytest.mark.parametrize("service", ["pro.be", "", "*", ">", "a b", "pro\nbe"])
def test_subject_for_rejects_bad_service(service: str) -> None:
    with pytest.raises(ValueError, match="service"):
        subject_for(service, "widget", "widget.created")


@pytest.mark.parametrize("aggregate_type", ["wid.get", "", "*", ">", "a b"])
def test_subject_for_rejects_bad_aggregate_type(aggregate_type: str) -> None:
    with pytest.raises(ValueError, match="aggregate_type"):
        subject_for("probe", aggregate_type, "widget.created")


def test_subject_token_pattern_is_the_documented_one() -> None:
    assert SUBJECT_TOKEN == r"^[A-Za-z0-9_-]+$"


def _row(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "event_id": uuid.UUID("01900000-0000-7000-8000-000000000001"),
        "occurred_at": datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC),
        "aggregate_type": "widget",
        "aggregate_id": "w-1",
        "aggregate_version": None,
        "scope_path": [uuid.UUID("01900000-0000-7000-8000-0000000000aa")],
        "action": "widget.created",
        "actor": {"type": "user", "id": "fake:admin", "display": "Admin", "via": "jwt"},
        "request_id": "req-1",
        "before": None,
        "after": {"id": "w-1"},
        "schema_version": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_envelope_round_trips_through_json_with_strings_for_uuids_and_datetimes() -> None:
    envelope = build_envelope(_row(), service="probe")
    decoded = json.loads(encode_envelope(envelope))
    assert decoded == {
        "event_id": "01900000-0000-7000-8000-000000000001",
        "occurred_at": "2026-11-09T12:00:00+00:00",
        "service": "probe",
        "aggregate_type": "widget",
        "aggregate_id": "w-1",
        "aggregate_version": None,
        "scope_path": ["01900000-0000-7000-8000-0000000000aa"],
        "action": "widget.created",
        "actor": {"type": "user", "id": "fake:admin", "display": "Admin", "via": "jwt"},
        "request_id": "req-1",
        "before": None,
        "after": {"id": "w-1"},
        "schema_version": 1,
    }


def test_encode_envelope_is_compact_utf8() -> None:
    raw = encode_envelope(build_envelope(_row(after={"name": "Zoë"}), service="probe"))
    assert b", " not in raw and b": " not in raw
    assert "Zoë".encode() in raw


BASELINE_KEYS = {
    "event_id",
    "occurred_at",
    "service",
    "aggregate_type",
    "aggregate_id",
    "aggregate_version",
    "scope_path",
    "action",
    "actor",
    "request_id",
    "before",
    "after",
    "schema_version",
}


def test_subject_for_access_prefix_and_unchanged_default() -> None:
    assert ACCESS_STREAM == "ACCESS"
    assert AUDIT_STREAM == "AUDIT"
    assert (
        subject_for("contracts", "contract", "read", prefix="access")
        == "access.contracts.contract.read"
    )
    assert subject_for("contracts", "contract", "read") == "audit.contracts.contract.read"
    assert (
        subject_for("identity", "authz", "check.denied", prefix="audit")
        == "audit.identity.authz.check.denied"
    )


@pytest.mark.parametrize("prefix", ["x", "", "Audit", "audit.x", "access.>", "*"])
def test_subject_for_rejects_any_other_prefix(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        subject_for("contracts", "contract", "read", prefix=prefix)  # type: ignore[arg-type]


def test_build_envelope_adds_detail_only_when_the_row_has_a_non_null_detail() -> None:
    with_detail = build_envelope(_row(detail={"a": 1}), service="identity")
    assert with_detail["detail"] == {"a": 1}
    assert set(with_detail) == BASELINE_KEYS | {"detail"}
    # Contracts and validation outbox rows have no detail attribute: byte-identical envelopes.
    assert set(build_envelope(_row(), service="contracts")) == BASELINE_KEYS
    assert set(build_envelope(_row(detail=None), service="identity")) == BASELINE_KEYS


def test_build_access_envelope_is_json_safe_with_a_fresh_uuid7_and_no_content() -> None:
    folder = uuid.UUID(int=7)
    detail = {"format": "yaml"}
    first = build_access_envelope(
        service="contracts",
        aggregate_type="contract",
        aggregate_id="c-1",
        action="contract.read",
        actor={"type": "user", "id": "fake:alice"},
        scope_path=[folder],
        request_id="req-9",
        detail=detail,
    )
    second = build_access_envelope(
        service="contracts",
        aggregate_type="contract",
        aggregate_id="c-1",
        action="contract.read",
        actor={"type": "user", "id": "fake:alice"},
        scope_path=(),
        request_id=None,
        detail={},
    )
    assert set(first) == BASELINE_KEYS | {"category", "detail"}
    assert uuid.UUID(first["event_id"]).version == 7
    assert first["event_id"] != second["event_id"]
    assert first["category"] == "access"
    assert (first["before"], first["after"], first["aggregate_version"]) == (None, None, None)
    assert first["scope_path"] == [str(folder)]
    assert first["schema_version"] == 1
    assert datetime.fromisoformat(first["occurred_at"]).tzinfo is not None
    assert first["detail"] == detail
    assert first["detail"] is not detail  # a copy: later producer changes never leak in
    assert json.loads(encode_envelope(first)) == first


class _FakeNats:
    def __init__(self, *, closed: bool = False, fail: bool = False) -> None:
        self.is_closed = closed
        self.fail = fail
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        if self.fail:
            raise ConnectionError("already gone")


async def test_closing_a_publisher_that_never_connected_is_a_no_op() -> None:
    publisher = JetStreamPublisher("nats://localhost:1")
    await publisher.close()
    await publisher.close()
    assert publisher._nc is None


@pytest.mark.parametrize(
    ("fake", "expected_calls"),
    [(_FakeNats(), 1), (_FakeNats(fail=True), 1), (_FakeNats(closed=True), 0)],
)
async def test_close_is_best_effort_and_forgets_the_connection(
    fake: _FakeNats, expected_calls: int
) -> None:
    publisher = JetStreamPublisher("nats://localhost:1")
    publisher._nc = fake  # type: ignore[assignment]
    publisher._js = SimpleNamespace()  # type: ignore[assignment]
    await publisher.close()  # never raises, even when the client fails to close
    assert fake.close_calls == expected_calls
    assert publisher._nc is None
    assert publisher._js is None
