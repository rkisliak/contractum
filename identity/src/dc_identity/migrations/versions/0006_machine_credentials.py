"""machine credentials: service accounts and opaque api credentials (PATs, service-account keys)

D-66 to D-72: a personal access token is a ``dcp_<public_id>_<secret>`` bearer token resolved from
this table on every request. Only the SHA-256 of the secret is stored. ``last_used_at`` is written
only by the batched usage flush.

Revision ID: 0006_machine_credentials
Revises: 0005_entra_identities
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0006_machine_credentials"
down_revision: str | None = "0005_entra_identities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")
    )


def upgrade() -> None:
    op.create_table(
        "service_accounts",
        _uuid_pk(),
        sa.Column("owner_folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        *audit_columns(),
        sa.ForeignKeyConstraint(["owner_folder_id"], ["folder.id"]),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 100", name="name_length"),
    )
    op.create_index(
        "uq_service_accounts_owner_lower_name",
        "service_accounts",
        ["owner_folder_id", sa.text("lower(name)")],
        unique=True,
    )
    op.create_table(
        "api_credentials",
        _uuid_pk(),
        sa.Column("public_id", sa.Text, nullable=False),
        sa.Column("secret_hash", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("local_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entra_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("service_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("read_only", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        *audit_columns(),
        sa.ForeignKeyConstraint(["local_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["entra_user_id"], ["entra_users.id"]),
        sa.ForeignKeyConstraint(["service_account_id"], ["service_accounts.id"]),
        sa.UniqueConstraint("public_id"),
        sa.CheckConstraint("public_id ~ '^[0-9a-f]{16}$'", name="public_id_shape"),
        sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_shape"),
        sa.CheckConstraint("kind IN ('pat', 'service_account_key')", name="kind_values"),
        sa.CheckConstraint(
            "(kind = 'pat' AND service_account_id IS NULL "
            "AND num_nonnulls(local_user_id, entra_user_id) = 1) "
            "OR (kind = 'service_account_key' AND service_account_id IS NOT NULL "
            "AND local_user_id IS NULL AND entra_user_id IS NULL)",
            name="owner_matches_kind",
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 100", name="name_length"),
        sa.CheckConstraint("expires_at > created_at", name="expires_after_creation"),
    )
    op.create_index("ix_api_credentials_local_user_id", "api_credentials", ["local_user_id"])
    op.create_index("ix_api_credentials_entra_user_id", "api_credentials", ["entra_user_id"])
    op.create_index(
        "ix_api_credentials_live_service_account",
        "api_credentials",
        ["service_account_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_api_credentials_live_service_account", table_name="api_credentials")
    op.drop_index("ix_api_credentials_entra_user_id", table_name="api_credentials")
    op.drop_index("ix_api_credentials_local_user_id", table_name="api_credentials")
    op.drop_table("api_credentials")
    op.drop_index("uq_service_accounts_owner_lower_name", table_name="service_accounts")
    op.drop_table("service_accounts")
