"""Identity persistence models (users, bootstrap state, tokens, folders, grants, outbox).

Snapshots handed to the audit outbox are built from each model's ``__audit_fields__``
allow-list. Password hashes, refresh-token hashes and the bootstrap secret are never listed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar

import uuid_utils.compat as uuid_compat
from dc_auth.roles import Role
from dc_core.db import new_metadata
from dc_core.uow import AuditColumnsMixin, OutboxMixin
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

PRINCIPAL_TYPES = ("user", "local_group", "entra_group", "service_account", "service_principal")
REGISTRATION_KINDS = ("user", "group", "service_principal")
_GUID_SHAPE = "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
FOLDER_KINDS = ("root", "organization", "project", "team")
GROUP_MEMBER_TYPES = ("user", "service_account")
ROLE_VALUES = tuple(str(role) for role in Role)
CREDENTIAL_KINDS = ("pat", "service_account_key")


class Ltree(UserDefinedType[str]):
    """PostgreSQL ``ltree``; values travel as plain strings (``root.acme.payments``)."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "LTREE"


class IdentityBase(DeclarativeBase):
    metadata = new_metadata()


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid_compat.uuid7,
        server_default=text("uuidv7()"),
    )


class User(IdentityBase, AuditColumnsMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("username = lower(username)", name="username_lowercase"),
        CheckConstraint(
            "email IS NULL OR (length(email) <= 254 AND email ~ '^[^@[:space:]]+@[^@[:space:]]+$')",
            name="email_shape",
        ),
    )
    # password_set and email_set are derived: neither the hash nor the address may reach an outbox
    # row (AUD-03).
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "username",
        "display_name",
        "password_set",
        "must_rotate",
        "is_active",
        "email_set",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    must_rotate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Session cutoff (D-75): set at reactivation; a JWT whose verified iat is at or before it, and a
    # refresh token created at or before it, are refused. Not an audit field.
    sessions_valid_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def password_set(self) -> bool:
        return self.password_hash is not None

    @property
    def email_set(self) -> bool:
        return self.email is not None


class EntraRegistration(IdentityBase, AuditColumnsMixin):
    """An Entra principal or group the platform admits (D-57, D-58).

    ``user`` and ``group`` rows are always directly registered by an administrator. A
    ``service_principal`` row may instead be just-in-time (``directly_registered`` false, Plan 04):
    it records a seen principal but admits nothing on its own (D-73, D-75).
    """

    __tablename__ = "entra_registrations"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in REGISTRATION_KINDS) + ")",
            name="kind_values",
        ),
        CheckConstraint(f"tenant_id ~ {_GUID_SHAPE}", name="tenant_id_shape"),
        CheckConstraint(f"object_id ~ {_GUID_SHAPE}", name="object_id_shape"),
        CheckConstraint("(kind = 'user') = (owner_folder_id IS NULL)", name="owner_matches_kind"),
        CheckConstraint("length(display_name) BETWEEN 1 AND 256", name="display_name_length"),
        CheckConstraint(
            "directly_registered OR kind = 'service_principal'", name="jit_only_service_principal"
        ),
        UniqueConstraint("kind", "tenant_id", "object_id"),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "kind",
        "tenant_id",
        "object_id",
        "owner_folder_id",
        "display_name",
        "is_active",
        "directly_registered",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_folder_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("folder.id"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    directly_registered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # Session cutoff (D-75): set at reactivation; a JWT whose verified iat is at or before it is
    # refused. Written by a later plan for this table. Not an audit field.
    sessions_valid_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EntraUser(IdentityBase, AuditColumnsMixin):
    """The record of an Entra user who has signed in (written in a later plan).

    The UPN hint is display-only and never audited: only the derived ``preferred_username_set``
    flag reaches an outbox row, as ``User.email_set`` does.
    """

    __tablename__ = "entra_users"
    __table_args__ = (
        CheckConstraint(f"tenant_id ~ {_GUID_SHAPE}", name="tenant_id_shape"),
        CheckConstraint(f"object_id ~ {_GUID_SHAPE}", name="object_id_shape"),
        CheckConstraint(
            "preferred_username IS NULL OR length(preferred_username) <= 256",
            name="preferred_username_length",
        ),
        UniqueConstraint("tenant_id", "object_id"),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "tenant_id",
        "object_id",
        "display_name",
        "preferred_username_set",
        "is_active",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Session cutoff (D-75): set at reactivation; a JWT whose verified iat is at or before it is
    # refused. Written by a later plan for this table. Not an audit field.
    sessions_valid_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def preferred_username_set(self) -> bool:
        return self.preferred_username is not None


class BootstrapState(IdentityBase, AuditColumnsMixin):
    __tablename__ = "bootstrap_state"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)
    __audit_fields__: ClassVar[tuple[str, ...]] = ("id", "consumed_at", "consumed_by")

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class RefreshToken(IdentityBase, AuditColumnsMixin):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        CheckConstraint(
            "client_summary IS NULL OR char_length(client_summary) <= 64",
            name="client_summary_length",
        ),
    )
    # D-23: routine issuance and refresh rotation are exempt from the audit tripwire; Plan 09
    # audits every revocation explicitly through uow.audit().
    __audit_event_required__: ClassVar[bool] = False
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "user_id",
        "family_id",
        "expires_at",
        "rotated_at",
        "revoked_at",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set at login and copied on every rotation (migration 0012): the absolute session cap counts
    # from here, not from the last refresh.
    session_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Session bookkeeping (migration 0013, quick 261120-s0p): a fixed-vocabulary summary of the
    # sign-in User-Agent (never the raw string) and the jti of the access token issued with this
    # row, which is how the caller's own session is recognised. Neither is audited.
    client_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_jti: Mapped[str | None] = mapped_column(Text, nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginThrottle(IdentityBase, AuditColumnsMixin):
    __tablename__ = "login_throttle"
    # D-23: throttle counter updates are high-volume, non-business churn and are exempt.
    __audit_event_required__: ClassVar[bool] = False
    __audit_fields__: ClassVar[tuple[str, ...]] = ("key", "failures", "locked_until")

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Folder(IdentityBase, AuditColumnsMixin):
    __tablename__ = "folder"
    __table_args__ = (
        CheckConstraint("kind IN ('root', 'organization', 'project', 'team')", name="kind_values"),
        CheckConstraint("(kind = 'root') = (parent_id IS NULL)", name="root_has_no_parent"),
        CheckConstraint(
            "nlevel(path) = CASE kind WHEN 'root' THEN 1 WHEN 'organization' THEN 2 "
            "WHEN 'project' THEN 3 WHEN 'team' THEN 4 END",
            name="depth_matches_kind",
        ),
        CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(slug) <= 63", name="slug_format"
        ),
        CheckConstraint(
            "NOT block_inheritance OR kind IN ('organization', 'team')", name="block_flag_kinds"
        ),
        UniqueConstraint("parent_id", "slug"),
        UniqueConstraint("path"),
        Index("ix_folder_path_gist", "path", postgresql_using="gist"),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "parent_id",
        "kind",
        "slug",
        "display_name",
        "path",
        "archived_at",
        "block_inheritance",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("folder.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Ltree, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    # D-29: cuts every grant held on an ancestor (global_admin excepted); Org and Team only.
    block_inheritance: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class RoleAssignment(IdentityBase, AuditColumnsMixin):
    __tablename__ = "role_assignments"
    __table_args__ = (
        CheckConstraint(
            "principal_type IN (" + ", ".join(f"'{kind}'" for kind in PRINCIPAL_TYPES) + ")",
            name="principal_type_values",
        ),
        CheckConstraint(
            "role IN (" + ", ".join(f"'{role}'" for role in ROLE_VALUES) + ")", name="role_values"
        ),
        UniqueConstraint("folder_id", "principal_type", "principal_id", "role"),
        Index("ix_role_assignments_principal", "principal_type", "principal_id"),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "folder_id",
        "principal_type",
        "principal_id",
        "role",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    folder_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("folder.id"), nullable=False
    )
    principal_type: Mapped[str] = mapped_column(Text, nullable=False)
    principal_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)


class LocalGroup(IdentityBase, AuditColumnsMixin):
    """A group owned by ROOT or an Organization folder (D-27); members are platform principals."""

    __tablename__ = "local_groups"
    __table_args__ = (CheckConstraint("length(name) BETWEEN 1 AND 100", name="name_length"),)
    __audit_fields__: ClassVar[tuple[str, ...]] = ("id", "owner_folder_id", "name")

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_folder_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("folder.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)


# Group names are unique per owner, case-insensitively.
Index(
    "uq_local_groups_owner_lower_name",
    LocalGroup.owner_folder_id,
    func.lower(LocalGroup.name),
    unique=True,
)


class GroupMember(IdentityBase, AuditColumnsMixin):
    __tablename__ = "group_members"
    __table_args__ = (
        CheckConstraint("member_type IN ('user', 'service_account')", name="member_type_values"),
        Index("ix_group_members_member", "member_type", "member_id"),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = ("group_id", "member_type", "member_id")

    group_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("local_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    member_type: Mapped[str] = mapped_column(Text, primary_key=True)
    member_id: Mapped[str] = mapped_column(Text, primary_key=True)


class ServiceAccount(IdentityBase, AuditColumnsMixin):
    """A non-human principal owned by an Organization or ROOT; keys are ``api_credentials``."""

    __tablename__ = "service_accounts"
    __table_args__ = (CheckConstraint("length(name) BETWEEN 1 AND 100", name="name_length"),)
    __audit_fields__: ClassVar[tuple[str, ...]] = ("id", "owner_folder_id", "name", "is_active")

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_folder_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("folder.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


# Service account names are unique per owner, case-insensitively.
Index(
    "uq_service_accounts_owner_lower_name",
    ServiceAccount.owner_folder_id,
    func.lower(ServiceAccount.name),
    unique=True,
)


class ApiCredential(IdentityBase, AuditColumnsMixin):
    """An opaque ``dcp_`` credential: a personal access token or a service-account key (D-71).

    Only the SHA-256 of the secret is stored. ``public_id`` and ``secret_hash`` are never audited:
    ``__audit_fields__`` is an allow-list that omits both. ``last_used_at`` is written only by the
    batched usage flush (a Core UPDATE outside any unit of work) and is not an audit field.
    """

    __tablename__ = "api_credentials"
    __table_args__ = (
        CheckConstraint("public_id ~ '^[0-9a-f]{16}$'", name="public_id_shape"),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_shape"),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in CREDENTIAL_KINDS) + ")",
            name="kind_values",
        ),
        CheckConstraint(
            "(kind = 'pat' AND service_account_id IS NULL "
            "AND num_nonnulls(local_user_id, entra_user_id) = 1) "
            "OR (kind = 'service_account_key' AND service_account_id IS NOT NULL "
            "AND local_user_id IS NULL AND entra_user_id IS NULL)",
            name="owner_matches_kind",
        ),
        CheckConstraint("length(name) BETWEEN 1 AND 100", name="name_length"),
        CheckConstraint("expires_at > created_at", name="expires_after_creation"),
        Index(
            "ix_api_credentials_live_service_account",
            "service_account_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
    __audit_fields__: ClassVar[tuple[str, ...]] = (
        "id",
        "kind",
        "name",
        "read_only",
        "expires_at",
        "revoked_at",
        "local_user_id",
        "entra_user_id",
        "service_account_id",
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    public_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    local_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    entra_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("entra_users.id"), nullable=True, index=True
    )
    service_account_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("service_accounts.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformSetting(IdentityBase, AuditColumnsMixin):
    """Quick 261120-0yc (D-01): one row per settings section, holding only stored overrides.

    ``value`` never holds a secret (secrets stay Docker/Kubernetes secrets). ``revision`` counts
    effective writes and builds the section ETag.
    """

    __tablename__ = "platform_setting"
    __table_args__ = (CheckConstraint("section ~ '^[a-z][a-z_]{0,31}$'", name="section_shape"),)
    __audit_fields__: ClassVar[tuple[str, ...]] = ("section", "value", "revision")

    section: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class PlatformSettingSeed(IdentityBase, AuditColumnsMixin):
    """Quick 261120-s0p (item 6): a marker that a one-time seed already ran.

    The webhook allow-list is seeded once from the channels that exist when the list became
    mandatory; the marker row makes the seed once-only, so a host an admin later removes is never
    put back. ``name`` is the seed's name (``webhook_hosts``).
    """

    __tablename__ = "platform_setting_seed"
    __table_args__ = (CheckConstraint("name ~ '^[a-z][a-z_]{0,63}$'", name="name_shape"),)
    __audit_fields__: ClassVar[tuple[str, ...]] = ("name", "seeded_at")

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    seeded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class OutboxEvent(OutboxMixin, IdentityBase):
    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),
    )

    # D-173: only identity's outbox carries a flat ``detail`` (the denial rows written by
    # ``dc_identity.denials``); ``build_envelope`` adds it to the envelope only when not null.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
