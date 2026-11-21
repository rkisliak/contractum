"""pause overlay for platform rule sets: rule_set.paused_at, rule_set.paused_by

Quick 261118-vpa: a Global admin can pause a built-in platform rule set. A paused set still runs
and reports every finding but resolves as a non-blocking ``warn``. The pause is an overlay on the
computed mode (``enforce_at`` is not touched), so resuming restores the mode exactly.

The CHECK constraint keeps the pause to platform sets: folder sets keep the clear/set mandatory
break-glass (D-119, D-142). Every set starts unpaused, so there is no data migration.

Revision ID: 0007_rule_set_pause
Revises: 0006_seed_semantic_rule_sets
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_rule_set_pause"
down_revision: str | None = "0006_seed_semantic_rule_sets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("rule_set", sa.Column("paused_at", sa.DateTime(timezone=True)))
    op.add_column("rule_set", sa.Column("paused_by", sa.Text))
    op.create_check_constraint(
        "paused_platform_only", "rule_set", "paused_at IS NULL OR scope = 'platform'"
    )


def downgrade() -> None:
    op.drop_constraint("paused_platform_only", "rule_set", type_="check")
    op.drop_column("rule_set", "paused_by")
    op.drop_column("rule_set", "paused_at")
