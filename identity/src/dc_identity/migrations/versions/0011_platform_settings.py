"""platform settings: one row per section of stored overrides

Quick 261120-0yc (D-01): Global-admin settings live in identity's database. A row holds only the
overrides an admin saved; an absent field means the owning service's Helm/env value applies.
``revision`` counts effective writes and builds each section's ETag. Nothing secret is stored.

Revision ID: 0011_platform_settings
Revises: 0010_folder_delete_guard
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0011_platform_settings"
down_revision: str | None = "0010_folder_delete_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_setting",
        sa.Column("section", sa.Text, primary_key=True),
        sa.Column(
            "value",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("revision", sa.Integer, nullable=False, server_default=sa.text("1")),
        *audit_columns(),
        sa.CheckConstraint("section ~ '^[a-z][a-z_]{0,31}$'", name="section_shape"),
    )


def downgrade() -> None:
    op.drop_table("platform_setting")
