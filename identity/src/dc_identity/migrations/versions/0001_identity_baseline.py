"""identity baseline: users, bootstrap state, tokens, folders, grants, outbox

Revision ID: 0001_identity_baseline
Revises:
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns, outbox_table
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType

revision: str = "0001_identity_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class _Ltree(UserDefinedType[str]):
    """Frozen copy of the model's ltree type: a migration must not import live models' types."""

    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "LTREE"


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")
    )


def upgrade() -> None:
    # A no-op in compose, where the init script pre-creates it as superuser.
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree")

    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=True),
        sa.Column("must_rotate", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        *audit_columns(),
        sa.UniqueConstraint("username"),
        sa.CheckConstraint("username = lower(username)", name="username_lowercase"),
    )

    op.create_table(
        "bootstrap_state",
        sa.Column("id", sa.SmallInteger, primary_key=True, autoincrement=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", sa.Text, nullable=True),
        *audit_columns(),
        sa.CheckConstraint("id = 1", name="singleton"),
    )

    op.create_table(
        "refresh_tokens",
        _uuid_pk(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *audit_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    op.create_table(
        "login_throttle",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("failures", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        *audit_columns(),
    )

    op.create_table(
        "folder",
        _uuid_pk(),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("path", _Ltree(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", sa.Text, nullable=True),
        *audit_columns(),
        sa.ForeignKeyConstraint(["parent_id"], ["folder.id"]),
        sa.CheckConstraint(
            "kind IN ('root', 'organization', 'project', 'team')", name="kind_values"
        ),
        sa.CheckConstraint("(kind = 'root') = (parent_id IS NULL)", name="root_has_no_parent"),
        sa.CheckConstraint(
            "nlevel(path) = CASE kind WHEN 'root' THEN 1 WHEN 'organization' THEN 2 "
            "WHEN 'project' THEN 3 WHEN 'team' THEN 4 END",
            name="depth_matches_kind",
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(slug) <= 63", name="slug_format"
        ),
        sa.UniqueConstraint("parent_id", "slug"),
        sa.UniqueConstraint("path"),
    )
    op.create_index("ix_folder_path_gist", "folder", ["path"], postgresql_using="gist")

    op.create_table(
        "role_assignments",
        _uuid_pk(),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_type", sa.Text, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        *audit_columns(),
        sa.ForeignKeyConstraint(["folder_id"], ["folder.id"]),
        sa.CheckConstraint(
            "principal_type IN ('user', 'local_group', 'entra_group', 'service_account')",
            name="principal_type_values",
        ),
        sa.UniqueConstraint("folder_id", "principal_type", "principal_id", "role"),
    )

    outbox_table(op)


def downgrade() -> None:
    op.drop_index("ix_outbox_unpublished", table_name="outbox")
    op.drop_table("outbox")
    op.drop_table("role_assignments")
    op.drop_index("ix_folder_path_gist", table_name="folder")
    op.drop_table("folder")
    op.drop_table("login_throttle")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("bootstrap_state")
    op.drop_table("users")
