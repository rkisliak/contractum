"""entra identities: registrations, signed-in Entra users, service_principal grantee type

D-57 (admission by registration), D-58 (the (tenant, object) key), D-73/D-75 (just-in-time
service-principal rows admit nothing on their own: ``directly_registered``), D-81 (the UPN hint
is display-only). ``role_assignments`` may now be granted to a ``service_principal``.

Revision ID: 0005_entra_identities
Revises: 0004_user_email
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0005_entra_identities"
down_revision: str | None = "0004_user_email"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUID = "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
_OLD_TYPES = "principal_type IN ('user', 'local_group', 'entra_group', 'service_account')"
_NEW_TYPES = (
    "principal_type IN ('user', 'local_group', 'entra_group', 'service_account', "
    "'service_principal')"
)


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")
    )


def upgrade() -> None:
    op.create_table(
        "entra_registrations",
        _uuid_pk(),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("object_id", sa.Text, nullable=False),
        sa.Column("owner_folder_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "directly_registered", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
        *audit_columns(),
        sa.ForeignKeyConstraint(["owner_folder_id"], ["folder.id"]),
        sa.CheckConstraint("kind IN ('user', 'group', 'service_principal')", name="kind_values"),
        sa.CheckConstraint(f"tenant_id ~ {_GUID}", name="tenant_id_shape"),
        sa.CheckConstraint(f"object_id ~ {_GUID}", name="object_id_shape"),
        sa.CheckConstraint(
            "(kind = 'user') = (owner_folder_id IS NULL)", name="owner_matches_kind"
        ),
        sa.CheckConstraint("length(display_name) BETWEEN 1 AND 256", name="display_name_length"),
        sa.CheckConstraint(
            "directly_registered OR kind = 'service_principal'", name="jit_only_service_principal"
        ),
        sa.UniqueConstraint("kind", "tenant_id", "object_id"),
    )
    op.create_table(
        "entra_users",
        _uuid_pk(),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("object_id", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("preferred_username", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        *audit_columns(),
        sa.CheckConstraint(f"tenant_id ~ {_GUID}", name="tenant_id_shape"),
        sa.CheckConstraint(f"object_id ~ {_GUID}", name="object_id_shape"),
        sa.CheckConstraint(
            "preferred_username IS NULL OR length(preferred_username) <= 256",
            name="preferred_username_length",
        ),
        sa.UniqueConstraint("tenant_id", "object_id"),
    )
    op.drop_constraint("principal_type_values", "role_assignments", type_="check")
    op.create_check_constraint("principal_type_values", "role_assignments", _NEW_TYPES)


def downgrade() -> None:
    op.drop_constraint("principal_type_values", "role_assignments", type_="check")
    op.create_check_constraint("principal_type_values", "role_assignments", _OLD_TYPES)
    op.drop_table("entra_users")
    op.drop_table("entra_registrations")
