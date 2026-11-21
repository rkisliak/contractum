"""mandatory rule sets: the flag on the rule set and enforce_at on each version

D-119, D-120, D-142: mandatory is a flag on the rule set itself (set by a Policy Manager at the
set's folder or above). Each published version of a mandatory set carries an ``enforce_at``: the
mode is computed (``enforce`` once ``enforce_at <= now``, ``warn`` before), never scheduled.
``cleared_enforced_version_no`` remembers the version that was enforced when the flag was cleared,
so setting the flag again restores it immediately instead of dropping it into a new grace period.

``enforce_at`` is deliberately not in the immutability trigger's column list
(``trg_rule_set_version_immutable`` fires only for the content columns), so it stays writable while
the content of a published version cannot change.

Revision ID: 0003_rule_set_mandatory
Revises: 0002_rule_sets
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_rule_set_mandatory"
down_revision: str | None = "0002_rule_sets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rule_set",
        sa.Column("mandatory", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.add_column("rule_set", sa.Column("mandatory_set_at", sa.DateTime(timezone=True)))
    op.add_column("rule_set", sa.Column("mandatory_set_by", sa.Text))
    op.add_column("rule_set", sa.Column("cleared_enforced_version_no", sa.Integer))
    op.create_index(
        "ix_rule_set_mandatory",
        "rule_set",
        ["folder_id"],
        postgresql_where=sa.text("mandatory"),
    )
    op.add_column("rule_set_version", sa.Column("enforce_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("rule_set_version", "enforce_at")
    op.drop_index("ix_rule_set_mandatory", table_name="rule_set")
    op.drop_column("rule_set", "cleared_enforced_version_no")
    op.drop_column("rule_set", "mandatory_set_by")
    op.drop_column("rule_set", "mandatory_set_at")
    op.drop_column("rule_set", "mandatory")
