"""Category derivation refusals that need no database (D-170, T-7-01, T-7-02)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from dc_audit.ingest import AuditEnvelope, SubjectMismatchError, category_for

pytestmark = pytest.mark.unit


def _envelope(**fields: Any) -> AuditEnvelope:
    base: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": "2026-11-21T08:00:00+00:00",
        "service": "contracts",
        "aggregate_type": "contract",
        "aggregate_id": "c-1",
        "action": "read",
        "actor": {"type": "user", "id": "fake:reader"},
        "schema_version": 1,
    }
    return AuditEnvelope.model_validate({**base, **fields})


def test_an_explicit_null_detail_is_accepted_as_no_detail() -> None:
    envelope = _envelope(detail=None)
    assert envelope.detail is None
    assert category_for(None, envelope) == "change"


def test_a_subject_outside_the_audit_and_access_families_is_refused() -> None:
    with pytest.raises(SubjectMismatchError, match="does not match the subject"):
        category_for("events.contracts.contract.read", _envelope())


def test_an_access_subject_refuses_an_envelope_claiming_another_category() -> None:
    with pytest.raises(SubjectMismatchError, match="carries only access events"):
        category_for("access.contracts.contract.read", _envelope(category="change"))


def test_an_access_subject_with_a_matching_envelope_is_an_access_event() -> None:
    assert category_for("access.contracts.contract.read", _envelope()) == "access"
