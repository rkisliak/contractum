"""audit baseline: audit_event and audit_dead_letter

Revision ID: 0001_audit_baseline
Revises:
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0001_audit_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service", sa.Text, nullable=False),
        sa.Column("aggregate_type", sa.Text, nullable=False),
        sa.Column("aggregate_id", sa.Text, nullable=False),
        sa.Column("aggregate_version", sa.Integer, nullable=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column(
            "scope_path",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("actor", postgresql.JSONB, nullable=False),
        sa.Column("request_id", sa.Text, nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("before", postgresql.JSONB, nullable=True),
        sa.Column("after", postgresql.JSONB, nullable=True),
        sa.Column("changes", postgresql.JSONB, nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_audit_event"),
        sa.UniqueConstraint("event_id", name="uq_audit_event_event_id"),
    )
    op.create_index("ix_audit_event_aggregate_id", "audit_event", ["aggregate_id"])
    op.create_index("ix_audit_event_service_action", "audit_event", ["service", "action"])
    op.create_index("ix_audit_event_occurred_at", "audit_event", ["occurred_at"])

    op.create_table(
        "audit_dead_letter",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("error", sa.Text, nullable=False),
        sa.Column("deliveries", sa.Integer, nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_audit_dead_letter"),
    )


def downgrade() -> None:
    op.drop_table("audit_dead_letter")
    op.drop_index("ix_audit_event_occurred_at", table_name="audit_event")
    op.drop_index("ix_audit_event_service_action", table_name="audit_event")
    op.drop_index("ix_audit_event_aggregate_id", table_name="audit_event")
    op.drop_table("audit_event")
