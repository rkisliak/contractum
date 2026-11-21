"""platform_setting_seed: marker rows for one-time settings seeds

Quick 261120-s0p (item 6): the webhook allow-list became mandatory, so contracts seeds it once
from the hosts of the channels that already exist. This table records that a named seed ran, so a
host an admin later removes is never put back.

Revision ID: 0014_settings_seed
Revises: 0013_refresh_session_client
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns

revision: str = "0014_settings_seed"
down_revision: str | None = "0013_refresh_session_client"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_setting_seed",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column(
            "seeded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        *audit_columns(),
        sa.CheckConstraint("name ~ '^[a-z][a-z_]{0,63}$'", name="name_shape"),
    )


def downgrade() -> None:
    op.drop_table("platform_setting_seed")
