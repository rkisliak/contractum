"""Contracts persistence models.

A contract is split in two (research Pattern 6): ``Contract`` is the stable identity (server
uuidv7, folder, tenant, ODCS id) and ``ContractVersion`` holds one immutable document version.
A version row is a draft, then published (the same row, same id and bytes), then possibly
unpublished; published rows are immutable in the database (D-40).
A draft carries a ``revision`` (starts at 1, +1 per accepted save): the HTTP layer exposes it as
the strong ETag ``"r<revision>"`` for ``If-Match`` optimistic concurrency (D-42).

``raw_document`` (bytea) is the fidelity store and what the default fetch returns. ``document``
(jsonb) is for validation, filtering and search only: jsonb drops key order and whitespace.

Audit snapshots are built from ``__audit_fields__``; neither the raw bytes nor the parsed
document (which may carry secrets in ``customProperties``) is ever listed.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, ClassVar

import uuid_utils.compat as uuid_compat
from dc_core.db import new_metadata
from dc_core.uow import AuditColumnsMixin, OutboxMixin
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# D-09: the ODCS id is unique within the Organization (tenant boundary). The name is explicit so
# the API can map the IntegrityError by constraint name (the naming convention would produce
# ``uq_contract_organization_id`` and hide the second column).
UNIQUE_ODCS_ID_CONSTRAINT = "uq_contract_organization_id_odcs_id"
# Latest pointer and the one-Latest / one-semver indexes (D-40); explicit so tests and the
# trigger messages can name them.
UNIQUE_ONE_LATEST_INDEX = "uq_contract_version_one_latest"
UNIQUE_SEMVER_INDEX = "uq_contract_version_semver"
# Template version numbers are unique per template (D-37); explicit for the same reason.
UNIQUE_TEMPLATE_VERSION_CONSTRAINT = "uq_template_version_template_id_version_no"
# Card names are unique per folder among non-archived cards, case-insensitively (A14); the partial
# unique index is named so the API maps its IntegrityError to 409 ``card-name-taken``.
UNIQUE_CARD_NAME_INDEX = "uq_subscriber_card_folder_lower_name"
# A card holds at most one active pin per version (D-89); the contract lock makes the service never
# hit it, so it is only the database backstop.
UNIQUE_ACTIVE_PIN_INDEX = "uq_card_subscription_active"


class ContractsBase(DeclarativeBase):
    metadata = new_metadata()


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid_compat.uuid7,
        server_default=text("uuidv7()"),
    )


class Contract(ContractsBase, AuditColumnsMixin):
    __tablename__ = "contract"
    __table_args__ = (
        UniqueConstraint("organization_id", "odcs_id", name=UNIQUE_ODCS_ID_CONSTRAINT),
        Index("ix_contract_ancestor_ids", "ancestor_ids", postgresql_using="gin"),
        # The Folders tree lists one folder's own contracts per expanded folder (Phase 16, D-07):
        # EXPLAIN on 10,000 contracts over 1,000 folders showed a sequential scan without it.
        Index("ix_contract_folder_id", "folder_id", "id"),
        # Both origin columns are set or both null (CONT-02), and a set pair names a template
        # version that exists: published versions are undeletable (D-38, T-02-52).
        CheckConstraint(
            "(template_id IS NULL) = (template_version IS NULL)", name="template_origin_pair"
        ),
        # Both origin columns are set or both null (D-44b). The contract exists for the life of
        # the copy (contracts are never hard-deleted); the version id is provenance only and has
        # no foreign key, because a draft source may later be discarded.
        CheckConstraint(
            "(origin_contract_id IS NULL) = (origin_version_id IS NULL)", name="version_origin_pair"
        ),
        ForeignKeyConstraint(
            ["origin_contract_id"], ["contract.id"], name="fk_contract_origin_contract"
        ),
        CheckConstraint("version_counter >= 1", name="version_counter_positive"),
        # Soft delete (D-55): both columns are set or both null; the row and its versions stay.
        CheckConstraint("(deleted_at IS NULL) = (deleted_by IS NULL)", name="deleted_pair"),
        # D-83 seam, switched off: only the open policy exists. Turning another one on is a
        # reviewed migration plus a code change, never a setting.
        CheckConstraint("subscription_policy IN ('open')", name="subscription_policy_known"),
        ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["template_version.template_id", "template_version.version_no"],
            name="fk_contract_template_origin",
        ),
        # The Latest pointer names a version row of this same contract (D-40). MATCH SIMPLE: a
        # null pointer skips the check. ``use_alter`` because contract_version references contract.
        ForeignKeyConstraint(
            ["id", "latest_version_id"],
            ["contract_version.contract_id", "contract_version.id"],
            name="fk_contract_latest_version",
            use_alter=True,
        ),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "folder_id",
        "organization_id",
        "odcs_id",
        "notation",
        "template_id",
        "template_version",
        "origin_contract_id",
        "origin_version_id",
        "latest_version_id",
        "version_counter",
        "deleted_at",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    folder_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # root-first, includes the folder itself; taken from identity's FolderFacts, never the client.
    ancestor_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False
    )
    # The ODCS-id scope key (repo.odcs_scope_id): the Organization id, or the ROOT folder id for a
    # contract directly at ROOT. The API exposes null for the ROOT case (public_organization_id).
    organization_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    odcs_id: Mapped[str] = mapped_column(Text, nullable=False)
    notation: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'odcs'"))
    # The template version this contract was copied from (CONT-02, D-38); null for a plain create.
    template_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The contract and version this contract was copied from (D-44b); both or neither. The
    # version id is provenance only: it may name a draft that is later published or discarded.
    origin_contract_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    origin_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    # The one Latest version (VER-03); derived statuses (preview, older) hang off it. Null until
    # the first publish.
    latest_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    # Source of the internal per-contract row number ``ContractVersion.version_no``.
    version_counter: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # Soft delete (D-55): a deleted contract is hidden from everyone, keeps its rows and its ODCS
    # id; an Organization or Global admin restores it (D-56). Both set or both null.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Who may subscribe a card to this contract (D-83). Only ``open`` exists: any principal that
    # can read a published version may pin it. Not audited: it cannot change in this release.
    subscription_policy: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'open'")
    )
    # Counts every attach, re-pin and detach of an explicit rule set (D-124): a publish compares
    # the value it validated against with the one under the contract lock. Not audited here: the
    # attachment events carry it.
    attachments_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


class ContractVersion(ContractsBase, AuditColumnsMixin):
    __tablename__ = "contract_version"
    __table_args__ = (
        UniqueConstraint("contract_id", "version_no"),
        # Target of the Latest pointer's composite foreign key.
        UniqueConstraint("contract_id", "id", name="uq_contract_version_contract_id_id"),
        Index(
            UNIQUE_ONE_LATEST_INDEX,
            "contract_id",
            unique=True,
            postgresql_where=text("is_latest"),
        ),
        Index(
            UNIQUE_SEMVER_INDEX,
            "contract_id",
            "semver_major",
            "semver_minor",
            "semver_patch",
            "semver_prerelease",
            unique=True,
            postgresql_where=text("state <> 'draft'"),
        ),
        Index("ix_contract_version_tags", "tags", postgresql_using="gin"),
        Index("ix_contract_version_lower_name", text("lower(name)")),
        Index("ix_contract_version_lower_domain", text("lower(domain)")),
        CheckConstraint("revision >= 1", name="revision_positive"),
        # D-40: lifecycle shape rules the database enforces under direct SQL as well.
        CheckConstraint("state IN ('draft', 'published', 'unpublished')", name="state_known"),
        CheckConstraint(
            "state = 'draft' OR (semver IS NOT NULL AND semver_major IS NOT NULL "
            "AND semver_minor IS NOT NULL AND semver_patch IS NOT NULL "
            "AND semver_prerelease IS NOT NULL AND published_at IS NOT NULL "
            "AND published_by IS NOT NULL)",
            name="published_has_semver",
        ),
        CheckConstraint("NOT is_latest OR state = 'published'", name="latest_is_published"),
        CheckConstraint("(deprecated_at IS NULL) = (sunset_date IS NULL)", name="deprecation_pair"),
        CheckConstraint("deprecated_at IS NULL OR state <> 'draft'", name="deprecation_not_draft"),
        CheckConstraint(
            "(state = 'unpublished') = (unpublished_at IS NOT NULL)", name="unpublished_pair"
        ),
        CheckConstraint(
            "coalesce(semver_major, 0) >= 0 AND coalesce(semver_minor, 0) >= 0 "
            "AND coalesce(semver_patch, 0) >= 0",
            name="semver_parts_non_negative",
        ),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "contract_id",
        "version_no",
        "revision",
        "state",
        "odcs_api_version",
        "media_type",
        "content_sha256",
        "size_bytes",
        "semver",
        "is_latest",
        "base_version_id",
        "published_at",
        "published_by",
        "unpublished_at",
        "deprecated_at",
        "sunset_date",
        "validation_run_id",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    contract_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    odcs_api_version: Mapped[str] = mapped_column(Text, nullable=False)
    raw_document: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    description_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    odcs_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Lifecycle (Phase 3). ``semver`` is the document's own ``version`` text; the typed parts back
    # the uniqueness and ordering. ``semver_prerelease`` is '' for "no prerelease" once published
    # (so the plain unique index covers releases); every semver column is null on a draft.
    semver: Mapped[str | None] = mapped_column(Text, nullable=True)
    semver_major: Mapped[int | None] = mapped_column(Integer, nullable=True)
    semver_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    semver_patch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    semver_prerelease: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_latest: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Provenance only (the row this draft was started from); deliberately no foreign key so
    # discarding a base never conflicts with the immutability trigger.
    base_version_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    unpublished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unpublished_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deprecated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    sunset_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # What governance validated this version against (RULE-07, D-132). Set once, in the publish
    # transaction, and immutable afterwards (the guard trigger); null on a draft. The run id is the
    # validation service's run (no foreign key: another service's database).
    validation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    pinned_rule_sets: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    engine_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_sha: Mapped[str | None] = mapped_column(Text, nullable=True)


class ContractReference(ContractsBase, AuditColumnsMixin):
    """One link from a published contract version to another contract (CONT-04, D-54).

    Rows are derived from an already audited publish (``publish_draft`` records the one event) and
    are never updated or deleted. ``ref_value`` is the link as text (a lowercased contract uuid or
    an ODCS id) and is resolved to a target only when a delete asks, so a contract created after
    its referrer was published is still protected.
    """

    __tablename__ = "contract_reference"
    __audit_event_required__: ClassVar[bool] = False
    __audit_fields__: ClassVar[tuple[str, ...]] = ()
    __table_args__ = (
        UniqueConstraint(
            "source_version_id",
            "ref_kind",
            "ref_value",
            name="uq_contract_reference_version_kind_value",
        ),
        Index("ix_contract_reference_kind_value", "ref_kind", "ref_value"),
        CheckConstraint("ref_kind IN ('contract_uuid', 'odcs_id')", name="ref_kind_known"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract_version.id"), nullable=False
    )
    source_contract_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract.id"), nullable=False
    )
    # The referrer's Organization, used to prefer a same-Organization namesake for ODCS-id links.
    organization_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ref_kind: Mapped[str] = mapped_column(Text, nullable=False)
    ref_value: Mapped[str] = mapped_column(Text, nullable=False)
    pointer: Mapped[str] = mapped_column(Text, nullable=False)


class Template(ContractsBase, AuditColumnsMixin):
    """A Policy Manager's template: one editable draft plus numbered immutable versions (D-37).

    ``draft_raw_document`` is the byte-faithful draft; ``publish_counter`` is the highest version
    number handed out (the publish route locks this row, so numbers have no gaps or duplicates).
    """

    __tablename__ = "template"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        Index("ix_template_ancestor_ids", "ancestor_ids", postgresql_using="gin"),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "folder_id",
        "organization_id",
        "name",
        "draft_api_version",
        "draft_media_type",
        "draft_content_sha256",
        "draft_size_bytes",
        "publish_counter",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    folder_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # root-first, includes the folder itself; from identity's FolderFacts, never the client.
    ancestor_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False
    )
    # None for a template at ROOT (ROOT has no organization).
    organization_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    draft_raw_document: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    draft_media_type: Mapped[str] = mapped_column(Text, nullable=False)
    draft_content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    draft_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    draft_api_version: Mapped[str] = mapped_column(Text, nullable=False)
    publish_counter: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class TemplateVersion(ContractsBase, AuditColumnsMixin):
    """One published, immutable template version (database triggers reject content changes).

    Only ``deprecated_at`` / ``deprecated_by`` may change after insert.
    """

    __tablename__ = "template_version"
    __table_args__ = (
        UniqueConstraint("template_id", "version_no", name=UNIQUE_TEMPLATE_VERSION_CONSTRAINT),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "template_id",
        "version_no",
        "odcs_api_version",
        "media_type",
        "content_sha256",
        "size_bytes",
        "deprecated_at",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    template_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("template.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    odcs_api_version: Mapped[str] = mapped_column(Text, nullable=False)
    raw_document: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deprecated_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class OutboxEvent(OutboxMixin, ContractsBase):
    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),
    )


class SubscriberCard(ContractsBase, AuditColumnsMixin):
    """A consuming team's card: contacts, delivery channels and (later) pinned versions (D-82).

    Owned by one folder of any kind; ``ancestor_ids`` (root-first, includes the folder) and
    ``organization_id`` come from identity's folder facts, never from the client. ``contacts`` is a
    validated list of ``{name, kind, value}`` kept exactly as submitted (D-97). ``webhook_url`` is
    deliberately not audited: provider URLs can embed tokens.
    """

    __tablename__ = "subscriber_card"
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "folder_id",
        "organization_id",
        "name",
        "description",
        "contacts",
        "email_enabled",
        "webhook_enabled",
        "archived_at",
        "archived_by",
    )
    __table_args__ = (
        CheckConstraint("(archived_at IS NULL) = (archived_by IS NULL)", name="archived_pair"),
        CheckConstraint("NOT webhook_enabled OR webhook_url IS NOT NULL", name="webhook_needs_url"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("char_length(description) BETWEEN 1 AND 2000", name="description_length"),
        CheckConstraint("jsonb_typeof(contacts) = 'array'", name="contacts_is_array"),
        Index(
            UNIQUE_CARD_NAME_INDEX,
            "folder_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index("ix_subscriber_card_ancestor_ids", "ancestor_ids", postgresql_using="gin"),
        Index("ix_subscriber_card_folder_id", "folder_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    folder_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ancestor_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False
    )
    # Null for a card owned by the ROOT folder (no Organization); migration 0018.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    contacts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    email_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    webhook_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class ContractRuleSetAttachment(ContractsBase, AuditColumnsMixin):
    """An extra published rule set a contract owner attached to one contract (RULE-06, D-123).

    ``rule_set_name`` is a snapshot taken when the attachment was made; the rule set itself lives
    in the validation service, so there is no foreign key to it. ``version_spec`` is ``latest`` or
    an exact version number as text. ``updated_by`` and ``updated_at`` are who attached it last.
    """

    __tablename__ = "contract_ruleset_attachment"
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "contract_id",
        "rule_set_id",
        "rule_set_name",
        "version_spec",
    )
    __table_args__ = (
        CheckConstraint(
            "version_spec = 'latest' OR version_spec ~ '^[1-9][0-9]{0,9}$'",
            name="version_spec_known",
        ),
        UniqueConstraint(
            "contract_id", "rule_set_id", name="uq_contract_ruleset_attachment_contract_rule_set"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    contract_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract.id"), nullable=False
    )
    rule_set_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    rule_set_name: Mapped[str] = mapped_column(Text, nullable=False)
    version_spec: Mapped[str] = mapped_column(Text, nullable=False)


class CardMember(ContractsBase, AuditColumnsMixin):
    """A group or service account linked to a card, so its members can act for the card (D-85).

    ``member_id`` is the identity service's id for the member: a bare lowercase uuid for a local
    group, a directory group GUID, or ``sa:<uuid>`` for a service account. Identity decides who is
    linkable (D-27, D-69, D-64); this table only records the link.
    """

    __tablename__ = "card_member"
    __audit_fields__: ClassVar[tuple[str, ...]] = ("card_id", "member_type", "member_id")
    __table_args__ = (
        CheckConstraint(
            "member_type IN ('local_group', 'entra_group', 'service_account')",
            name="member_type_known",
        ),
        CheckConstraint("char_length(member_id) BETWEEN 1 AND 200", name="member_id_length"),
    )

    card_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("subscriber_card.id"), primary_key=True
    )
    member_type: Mapped[str] = mapped_column(Text, primary_key=True)
    member_id: Mapped[str] = mapped_column(Text, primary_key=True)


class CardSubscription(ContractsBase, AuditColumnsMixin):
    """One pin of a card to one published contract version (SUB-03, D-89).

    ``created_by`` and ``created_at`` are who pinned the version and when. An ended pin keeps who
    and when it ended, why and, for a move, the pin that replaced it. Rows are history: the
    database refuses deletes and refuses every change except ending a row once (D-84), and a pin
    records a dependency, never access (D-87, D-88).
    """

    __tablename__ = "card_subscription"
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "card_id",
        "contract_id",
        "version_id",
        "mode",
        "ended_at",
        "ended_by",
        "end_reason",
        "end_note",
        "replaced_by_id",
    )
    __table_args__ = (
        # D-83 seam, switched off: only pinned subscriptions exist.
        CheckConstraint("mode IN ('pinned')", name="mode_known"),
        CheckConstraint(
            "end_reason IN ('moved', 'unsubscribed', 'removed_by_admin', 'card_archived')",
            name="end_reason_known",
        ),
        CheckConstraint("char_length(end_note) <= 500", name="end_note_length"),
        CheckConstraint(
            "(ended_at IS NULL) = (ended_by IS NULL) AND (ended_at IS NULL) = (end_reason IS NULL)",
            name="ended_consistent",
        ),
        Index(
            UNIQUE_ACTIVE_PIN_INDEX,
            "card_id",
            "version_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_card_subscription_active_contract",
            "contract_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_card_subscription_active_version",
            "version_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index("ix_card_subscription_card", "card_id", "id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    card_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("subscriber_card.id"), nullable=False
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract.id"), nullable=False
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract_version.id"), nullable=False
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pinned'"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("card_subscription.id"), nullable=True
    )


# The notice kinds (D-100, plus the breaking-change notice of D-162); the same list is the
# ``event_type_known`` CHECK constraint.
NOTICE_EVENT_TYPES: tuple[str, ...] = (
    "contract.version.published",
    "contract.version.breaking_published",
    "contract.latest.changed",
    "contract.version.deprecated",
    "contract.version.sunset_approaching",
    "contract.version.unpublish_blocked",
    "card.subscription.removed",
)
UNIQUE_NOTICE_DEDUPE_CONSTRAINT = "uq_card_notice_card_dedupe"


class CardNotice(ContractsBase):
    """One lifecycle event recorded on one card, whatever its channels (SUB-07, D-100).

    Plain (no audit mixin): the consumer, the delivery worker and the scheduler write it with no
    acting principal. ``dedupe_key`` (``event:<event id>``) with the card makes the materialiser
    exactly-once. ``contract_id`` and ``version_id`` are plain ids without foreign keys: a notice
    is a record of what happened and never constrains a later operation. The payload carries
    identifiers, SemVer and event fields only, never document content or other cards' data.
    """

    __tablename__ = "card_notice"
    __table_args__ = (
        CheckConstraint(
            "event_type IN (" + ", ".join(f"'{t}'" for t in NOTICE_EVENT_TYPES) + ")",
            name="event_type_known",
        ),
        UniqueConstraint("card_id", "dedupe_key", name=UNIQUE_NOTICE_DEDUPE_CONSTRAINT),
        Index("ix_card_notice_card_id", "card_id", "id"),
        Index("ix_card_notice_version_type", "version_id", "event_type", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    card_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("subscriber_card.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    contract_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    version_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CardNoticeDelivery(ContractsBase):
    """One channel a notice must be sent on, with its attempt record (SUB-07, D-98).

    Plain (no audit mixin), like ``CardNotice``. ``last_error`` is a short, scrubbed reason (at
    most 200 characters), never a response body. ``ix_card_notice_delivery_due`` serves the
    delivery workers' "what is due" scan.
    """

    __tablename__ = "card_notice_delivery"
    __table_args__ = (
        CheckConstraint("channel IN ('email', 'webhook')", name="channel_known"),
        CheckConstraint(
            "status IN ('pending', 'delivered', 'failed', 'skipped')", name="status_known"
        ),
        CheckConstraint("char_length(last_error) <= 200", name="last_error_length"),
        UniqueConstraint("notice_id", "channel", name="uq_card_notice_delivery_channel"),
        Index(
            "ix_card_notice_delivery_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    notice_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("card_notice.id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CardWebhookSecret(ContractsBase):
    """The signing secret of one card's webhook channel, Fernet-encrypted at rest (D-98, D-107).

    Plain (no audit mixin) and never passed to ``UnitOfWork.snapshot``: ``__audit_fields__`` is
    empty so nothing of it can reach an audit event. ``previous_enc`` is the one earlier secret
    that stays valid until ``previous_valid_until`` (the rotation overlap); ``generation`` counts
    the secrets issued, and is the only part an audit event may carry.
    """

    __tablename__ = "card_webhook_secret"
    __audit_fields__: ClassVar[tuple[str, ...]] = ()
    __table_args__ = (
        CheckConstraint("generation >= 1", name="generation_positive"),
        CheckConstraint(
            "(previous_enc IS NULL) = (previous_valid_until IS NULL)", name="previous_pair"
        ),
    )

    card_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("subscriber_card.id"), primary_key=True
    )
    secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    previous_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
