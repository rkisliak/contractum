"""Idempotent ingestion of audit envelopes (AUD-02).

``handle_payload`` is the transport-agnostic entry (D-21): the JetStream consumer calls it, and
so would any fallback adapter (HTTP push, Postgres queue). It raises on malformed input so the
caller decides between retry and dead letter.

Categories (D-170) are derived, never trusted: the ``access.`` subject prefix means ``access``,
the triple (identity, authz, check.denied) on ``audit.`` means ``denial``, and everything else is
``change``. A producer-claimed ``category`` that disagrees is a :class:`SubjectMismatchError`.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

import jsonpatch
from dc_core.events import SUBJECT_PREFIXES, subject_for
from pydantic import BaseModel, StrictBool, StrictInt, StrictStr, field_validator
from sqlalchemy.dialects.postgresql import insert

from dc_audit.models import INGEST_ACTOR, AuditEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SubjectMismatchError(ValueError):
    """The envelope does not belong on the subject it arrived on (spoofed service or action)."""


Category = Literal["change", "access", "denial"]
DetailValue = StrictStr | StrictInt | StrictBool | None
# The only (service, aggregate_type, action) that may be stored as a denial: identity's PDP.
DENIAL_TRIPLE = ("identity", "authz", "check.denied")
# Flat detail caps (T-7-02): producers allow-list their keys, ingest caps the shape.
DETAIL_MAX_KEYS = 16
DETAIL_MAX_STRING = 512
DETAIL_KEY = re.compile(r"^[a-z_]{1,32}$")


class AuditEnvelope(BaseModel, extra="forbid"):
    event_id: uuid.UUID
    occurred_at: datetime
    service: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int | None = None
    scope_path: list[uuid.UUID] = []
    action: str
    actor: dict[str, Any]
    request_id: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    schema_version: Literal[1]
    # Additive, optional fields (Phase 7): schema_version stays 1.
    category: Category | None = None
    detail: dict[str, DetailValue] | None = None

    @field_validator("actor")
    @classmethod
    def _actor_has_id(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value.get("id"), str) or not value["id"]:
            raise ValueError("actor.id is required")
        return value

    @field_validator("detail")
    @classmethod
    def _detail_is_flat_and_small(
        cls, value: dict[str, DetailValue] | None
    ) -> dict[str, DetailValue] | None:
        # The type already refuses nested objects, lists and floats; this caps size and keys.
        if value is None:
            return value
        if len(value) > DETAIL_MAX_KEYS:
            raise ValueError(f"detail has more than {DETAIL_MAX_KEYS} keys")
        for key, item in value.items():
            if not DETAIL_KEY.fullmatch(key):
                raise ValueError("detail keys must match ^[a-z_]{1,32}$")
            if isinstance(item, str) and len(item) > DETAIL_MAX_STRING:
                raise ValueError(f"detail values are at most {DETAIL_MAX_STRING} characters")
        return value


def _refuse_content(envelope: AuditEnvelope) -> None:
    # Access and denial rows carry no snapshots, so content cannot be smuggled through them.
    if envelope.before is not None or envelope.after is not None:
        raise SubjectMismatchError("access and denial events must have null before and after")


def category_for(subject: str | None, envelope: AuditEnvelope) -> Category:
    """The category this envelope must be stored with; raises ``SubjectMismatchError``.

    With a subject, its first token is the prefix and the whole subject must equal
    ``subject_for(service, aggregate_type, action, prefix=<first token>)``, so a producer cannot
    write as another service. Without one (direct calls), the prefix follows the claimed category.
    """
    if subject is not None:
        prefix = subject.split(".", 1)[0]
        if prefix not in SUBJECT_PREFIXES:
            raise SubjectMismatchError("envelope does not match the subject it was published on")
        expected = subject_for(
            envelope.service,
            envelope.aggregate_type,
            envelope.action,
            prefix="access" if prefix == "access" else "audit",
        )
        if subject != expected:
            raise SubjectMismatchError("envelope does not match the subject it was published on")
    else:
        prefix = "access" if envelope.category == "access" else "audit"
    if prefix == "access":
        if envelope.category not in (None, "access"):
            raise SubjectMismatchError("an access subject carries only access events")
        _refuse_content(envelope)
        return "access"
    if (envelope.service, envelope.aggregate_type, envelope.action) == DENIAL_TRIPLE:
        if envelope.category not in (None, "denial"):
            raise SubjectMismatchError("identity's check.denied events are denials")
        _refuse_content(envelope)
        return "denial"
    if envelope.category not in (None, "change"):
        raise SubjectMismatchError("only identity's PDP emits denials; access events use access.")
    return "change"


async def ingest_event(
    session: AsyncSession, envelope: AuditEnvelope, *, category: Category | None = None
) -> bool:
    """Insert one event; ``False`` when this ``event_id`` was already stored (replay).

    ``category`` defaults to the one derived without a subject (:func:`category_for`).
    """
    stored_category = category if category is not None else category_for(None, envelope)
    changes = jsonpatch.make_patch(envelope.before or {}, envelope.after or {}).patch
    stmt = (
        insert(AuditEvent)
        .values(
            event_id=envelope.event_id,
            service=envelope.service,
            aggregate_type=envelope.aggregate_type,
            aggregate_id=envelope.aggregate_id,
            aggregate_version=envelope.aggregate_version,
            action=envelope.action,
            scope_path=envelope.scope_path,
            actor=envelope.actor,
            request_id=envelope.request_id,
            occurred_at=envelope.occurred_at,
            before=envelope.before,
            after=envelope.after,
            changes=changes,
            schema_version=envelope.schema_version,
            category=stored_category,
            detail=envelope.detail,
            created_by=INGEST_ACTOR,
            updated_by=INGEST_ACTOR,
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(AuditEvent.id)
    )
    inserted = (await session.execute(stmt)).scalar_one_or_none()
    return inserted is not None


async def handle_payload(
    session_factory: async_sessionmaker[AsyncSession],
    payload: bytes,
    *,
    subject: str | None = None,
) -> bool:
    """Decode, validate, ingest and commit one payload; ``True`` if a row was inserted.

    When the transport knows the subject the message arrived on, pass it: the envelope must then
    match ``subject_for(service, aggregate_type, action, prefix=<subject prefix>)`` exactly, so a
    producer cannot write events as another service (T-01-18), and the category is derived from
    it (:func:`category_for`, T-7-01). Raises ``UnicodeDecodeError``, ``pydantic.ValidationError``
    (malformed JSON or envelope) or ``SubjectMismatchError``.
    """
    envelope = AuditEnvelope.model_validate_json(payload.decode("utf-8"))
    category = category_for(subject, envelope)
    async with session_factory() as session:
        inserted = await ingest_event(session, envelope, category=category)
        await session.commit()
    return inserted
