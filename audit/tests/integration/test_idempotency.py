"""Delivering the same event twice stores exactly one audit_event row (T-01-21, D-21)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from dc_audit.ingest import AuditEnvelope, SubjectMismatchError, handle_payload, ingest_event
from dc_core.events import encode_envelope, subject_for
from pydantic import ValidationError

pytestmark = pytest.mark.integration


def _envelope(event_id: uuid.UUID | None = None, **overrides: Any) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "event_id": str(event_id or uuid.uuid4()),
        "occurred_at": "2026-11-09T12:00:00+00:00",
        "service": "probe",
        "aggregate_type": "widget",
        "aggregate_id": "w-1",
        "aggregate_version": 3,
        "scope_path": [str(uuid.uuid4())],
        "action": "widget.updated",
        "actor": {"type": "user", "id": "fake:admin", "display": "Admin", "via": "jwt"},
        "request_id": "req-1",
        "before": {"name": "a", "tags": ["x"]},
        "after": {"name": "b", "tags": ["x", "y"]},
        "schema_version": 1,
    }
    envelope.update(overrides)
    return envelope


async def _count(audit_env: Any) -> int:
    return int((await audit_env.fetch("SELECT count(*) AS n FROM audit_event"))[0]["n"])


async def test_handle_payload_twice_stores_one_row(audit_env: Any) -> None:
    payload = encode_envelope(_envelope())
    assert await handle_payload(audit_env.session_factory, payload) is True
    assert await handle_payload(audit_env.session_factory, payload) is False
    assert await _count(audit_env) == 1


async def test_ingest_event_called_directly_is_idempotent_and_computes_the_patch(
    audit_env: Any,
) -> None:
    envelope = AuditEnvelope.model_validate(_envelope())
    async with audit_env.session_factory() as session:
        assert await ingest_event(session, envelope) is True
        assert await ingest_event(session, envelope) is False
        await session.commit()
    (row,) = await audit_env.fetch("SELECT changes, scope_path FROM audit_event")
    assert {(op["op"], op["path"]) for op in row["changes"]} == {
        ("replace", "/name"),
        ("add", "/tags/1"),
    }
    assert row["scope_path"] == envelope.scope_path


async def test_distinct_events_are_distinct_rows(audit_env: Any) -> None:
    for _ in range(3):
        assert await handle_payload(audit_env.session_factory, encode_envelope(_envelope()))
    assert await _count(audit_env) == 3


@pytest.mark.parametrize(
    "mutation",
    [
        {"unexpected": 1},
        {"schema_version": 2},
        {"event_id": "not-a-uuid"},
        {"actor": {}},
        {"actor": {"id": ""}},
        {"occurred_at": "yesterday"},
    ],
)
async def test_malformed_envelopes_are_rejected_and_store_nothing(
    audit_env: Any, mutation: dict[str, Any]
) -> None:
    payload = encode_envelope(_envelope(**mutation))
    with pytest.raises(ValidationError):
        await handle_payload(audit_env.session_factory, payload)
    assert await _count(audit_env) == 0


async def test_non_json_and_non_utf8_payloads_raise(audit_env: Any) -> None:
    with pytest.raises(ValidationError):
        await handle_payload(audit_env.session_factory, b"not json")
    with pytest.raises(UnicodeDecodeError):
        await handle_payload(audit_env.session_factory, b"\xff\xfe\x00")
    assert await _count(audit_env) == 0


async def test_an_envelope_must_match_the_subject_it_arrived_on(audit_env: Any) -> None:
    envelope = _envelope()
    payload = encode_envelope(envelope)
    right = subject_for("probe", "widget", "widget.updated")
    with pytest.raises(SubjectMismatchError):
        # A producer allowed to publish on audit.other.> cannot write events as service "probe".
        await handle_payload(
            audit_env.session_factory,
            payload,
            subject=subject_for("other", "widget", "widget.updated"),
        )
    assert await _count(audit_env) == 0
    assert await handle_payload(audit_env.session_factory, payload, subject=right) is True


async def test_jetstream_republish_of_the_same_event_id_stores_one_row(
    audit_env: Any, run_consumer: Any, eventually: Any, stream_exists: Any
) -> None:
    run_consumer(audit_env.consumer())
    await eventually(stream_exists)
    event_id = uuid.uuid4()
    payload = encode_envelope(_envelope(event_id))
    subject = subject_for("probe", "widget", "widget.updated")
    publisher = audit_env.publisher()
    try:
        first = await publisher.publish(subject, payload, msg_id=str(event_id))
        second = await publisher.publish(subject, payload, msg_id=str(event_id))
    finally:
        await publisher.close()
    assert first.duplicate is False
    assert second.duplicate is True  # JetStream's duplicate window absorbed it

    async def _stored() -> bool:
        return await _count(audit_env) >= 1

    await eventually(_stored)
    assert await _count(audit_env) == 1
