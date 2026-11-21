"""outbox detail: a nullable jsonb ``detail`` column on identity's outbox

D-172, D-173: every denied permission check writes one ``authz`` / ``check.denied`` outbox row
whose flat ``detail`` (permission, outcome, asking service, route template) travels in the audit
envelope. Only identity's outbox gets the column; contracts' and validation's outboxes are
unchanged, and ``build_envelope`` adds ``detail`` only when a row carries a non-null one.

Revision ID: 0009_outbox_detail
Revises: 0008_notifier_role
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_outbox_detail"
down_revision: str | None = "0008_notifier_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("detail", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox", "detail")
