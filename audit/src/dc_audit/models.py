"""Audit persistence models. The audit service emits no events, so it has no outbox table."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar

from dc_core.db import new_metadata
from dc_core.uow import AuditColumnsMixin
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    Identity,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

INGEST_ACTOR = "system:audit-ingest"
CATEGORIES = ("change", "access", "denial")


class AuditBase(DeclarativeBase):
    metadata = new_metadata()


class AuditEvent(AuditBase, AuditColumnsMixin):
    """One row per delivered event, unique on ``event_id`` (the idempotency key)."""

    __tablename__ = "audit_event"
    __audit_event_required__: ClassVar[bool] = False
    __table_args__ = (
        CheckConstraint("category IN ('change', 'access', 'denial')", name="category"),
        Index("ix_audit_event_service_action", "service", "action"),
        # Keyset query shapes (D-168): newest first, id as the tie-breaker.
        Index("ix_audit_event_time", text("occurred_at DESC"), text("id DESC")),
        Index(
            "ix_audit_event_leaf_time", "leaf_folder_id", text("occurred_at DESC"), text("id DESC")
        ),
        Index(
            "ix_audit_event_aggregate_time",
            "aggregate_type",
            "aggregate_id",
            text("occurred_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_audit_event_actor_time",
            text("(actor ->> 'id')"),
            text("occurred_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), unique=True, nullable=False)
    service: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    scope_path: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    actor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # none_as_null: a create event (before=None) is SQL NULL, not the JSON value null.
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Derived at ingest from the subject, never trusted from the producer (D-170).
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'change'"))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    # STORED explicitly: PostgreSQL 18 defaults generated columns to virtual (not indexable).
    leaf_folder_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        Computed("scope_path[array_upper(scope_path, 1)]", persisted=True),
        nullable=True,
    )


class AuditDeadLetter(AuditBase, AuditColumnsMixin):
    """A message that failed ``max_deliver`` times: kept for inspection, then acknowledged."""

    __tablename__ = "audit_dead_letter"
    __audit_event_required__: ClassVar[bool] = False

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    deliveries: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
