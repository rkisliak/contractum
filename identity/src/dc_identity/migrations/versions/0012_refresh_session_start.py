"""refresh_tokens.session_started_at: the start of a sign-in session, for an absolute cap

Quick 261120-0ye (D-39, S3): a refresh-token family slides on every rotation, so nothing bounded
the whole session. ``session_started_at`` is set at login and copied on every rotation, so the
cap ``session_max_s`` counts from the sign-in, not from the last refresh. Existing rows are
backfilled from ``created_at`` (the best known start; a family that already rotated for days is
treated as started when its newest token was issued, which is never stricter than reality).

Revision ID: 0012_refresh_session_start
Revises: 0011_platform_settings
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_refresh_session_start"
down_revision: str | None = "0011_platform_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "refresh_tokens",
        sa.Column("session_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE refresh_tokens SET session_started_at = created_at")
    op.alter_column(
        "refresh_tokens",
        "session_started_at",
        nullable=False,
        server_default=sa.func.now(),
    )


def downgrade() -> None:
    op.drop_column("refresh_tokens", "session_started_at")
