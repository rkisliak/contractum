"""validation baseline: outbox table (shared helper)

Revision ID: 0001_validation_baseline
Revises:
Create Date: 2026-11-09
"""

from collections.abc import Sequence

from alembic import op
from dc_core.migrations import outbox_table

revision: str = "0001_validation_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    outbox_table(op)


def downgrade() -> None:
    op.drop_index("ix_outbox_unpublished", table_name="outbox")
    op.drop_table("outbox")
