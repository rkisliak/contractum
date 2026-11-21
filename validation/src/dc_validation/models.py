"""Validation persistence models: the outbox, the rule-set tables and stored runs (RULE-01..04).

A rule set is one editable draft (``RuleSet.draft_*``) plus immutable numbered versions
(``RuleSetVersion``); rules have no tables of their own, they are versioned only through their set
(D-109). Its holder is a ``scope``: a ``folder`` (``folder_id`` and ``folder_kind`` set) or the
``platform`` (both NULL: the seeded structural set, which cannot know identity's ROOT id).

Audit snapshots are built from ``__audit_fields__``; ``draft_rules`` and ``rules`` (conditions and
fixtures) are never listed, so an event carries rule keys and hashes only (D-117).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar

import uuid_utils.compat as uuid_compat
from dc_core.db import new_metadata
from dc_core.uow import AuditColumnsMixin, OutboxMixin
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Names are unique per folder and among platform sets, case-insensitively (D-113). Explicit so the
# service can map the IntegrityError by constraint name.
UNIQUE_RULE_SET_FOLDER_NAME_INDEX = "uq_rule_set_folder_name"
UNIQUE_RULE_SET_PLATFORM_NAME_INDEX = "uq_rule_set_platform_name"
# Version numbers are unique per rule set; the publish counter makes them contiguous.
UNIQUE_RULE_SET_VERSION_CONSTRAINT = "uq_rule_set_version_rule_set_id_version_no"

SCOPE_FOLDER = "folder"
SCOPE_PLATFORM = "platform"


class ValidationBase(DeclarativeBase):
    metadata = new_metadata()


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid_compat.uuid7,
        server_default=text("uuidv7()"),
    )


class RuleSet(ValidationBase, AuditColumnsMixin):
    """A Policy Manager's rule set: one editable draft plus numbered immutable versions."""

    __tablename__ = "rule_set"
    __table_args__ = (
        CheckConstraint("scope IN ('folder', 'platform')", name="scope_known"),
        CheckConstraint(
            "((scope = 'folder') = (folder_id IS NOT NULL))"
            " AND ((folder_id IS NULL) = (folder_kind IS NULL))",
            name="scope_folder_consistent",
        ),
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("length(description) <= 2000", name="description_length"),
        # Only a platform set can be paused (quick 261118-vpa); folder sets keep the clear/set
        # mandatory break-glass.
        CheckConstraint("paused_at IS NULL OR scope = 'platform'", name="paused_platform_only"),
        Index(
            UNIQUE_RULE_SET_FOLDER_NAME_INDEX,
            "folder_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("scope = 'folder'"),
        ),
        Index(
            UNIQUE_RULE_SET_PLATFORM_NAME_INDEX,
            text("lower(name)"),
            unique=True,
            postgresql_where=text("scope = 'platform'"),
        ),
        Index("ix_rule_set_ancestor_ids", "ancestor_ids", postgresql_using="gin"),
        Index("ix_rule_set_mandatory", "folder_id", postgresql_where=text("mandatory")),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "scope",
        "folder_id",
        "folder_kind",
        "organization_id",
        "name",
        "description",
        "publish_counter",
        "draft_revision",
        "draft_content_sha256",
        "mandatory",
        "mandatory_set_at",
        "mandatory_set_by",
        "cleared_enforced_version_no",
        "paused_at",
        "paused_by",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'folder'"))
    folder_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    folder_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    # root-first, includes the folder itself; from identity's FolderFacts, never the client.
    ancestor_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    # None for ROOT and for platform sets (neither has an organization).
    organization_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_counter: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    draft_rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    draft_content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    # D-119: mandatory is a flag on the set itself; it applies to the holder folder and below.
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    mandatory_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mandatory_set_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    # D-142: the version enforced when the flag was last cleared (restored on the next set).
    cleared_enforced_version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Pause overlay (platform sets only): while set the set still runs and reports but resolves as
    # a non-blocking warn. enforce_at is untouched, so resume needs no restamp.
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class RuleSetVersion(ValidationBase, AuditColumnsMixin):
    """One published, immutable rule-set version (database triggers reject content changes).

    Only ``deprecated_at`` / ``deprecated_by`` and ``enforce_at`` may change after insert.
    """

    __tablename__ = "rule_set_version"
    __table_args__ = (
        UniqueConstraint("rule_set_id", "version_no", name=UNIQUE_RULE_SET_VERSION_CONSTRAINT),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "rule_set_id",
        "version_no",
        "dsl_version",
        "content_sha256",
        "rule_keys",
        "deprecated_at",
        "deprecated_by",
        "enforce_at",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    rule_set_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rule_set.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    dsl_version: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    rule_keys: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deprecated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    # D-120: NULL = no enforcement date; set = ``enforce`` once ``enforce_at <= now`` else ``warn``.
    enforce_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ValidationRun(ValidationBase):
    """One stored publish-gate run (D-133): immutable, kept forever.

    A plain model on purpose (no audit mixin): a run is a derived record and the audited fact is
    ``contract.version_published``, which carries the run id (research A6). Database triggers
    refuse every UPDATE and DELETE. ``report`` holds locations, static rule messages and fixed
    texts only, never a document value (VAL-07).
    """

    __tablename__ = "validation_run"
    __table_args__ = (
        CheckConstraint("mode IN ('publish')", name="mode_known"),
        Index("ix_validation_run_contract", "contract_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    contract_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    draft_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    folder_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    dsl_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_sha: Mapped[str] = mapped_column(Text, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    publishable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class OutboxEvent(OutboxMixin, ValidationBase):
    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),
    )
