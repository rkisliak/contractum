"""audit query: category, detail, STORED leaf_folder_id and the query indexes

Revision ID: 0002_audit_query
Revises: 0001_audit_baseline
Create Date: 2026-11-14

Phase 7 (D-168, D-170). ``category`` defaults to ``change``, so every row written before this
revision reads ``change`` (adding a column with a constant default is metadata-only).
``leaf_folder_id`` is the last element of ``scope_path`` (NULL for an empty path) and must be
``STORED``: PostgreSQL 18 makes generated columns virtual by default, and a virtual column cannot
be indexed. The two single-column indexes on ``occurred_at`` and ``aggregate_id`` are superseded by
the composite keyset indexes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_audit_query"
down_revision: str | None = "0001_audit_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_event",
        sa.Column("category", sa.Text, server_default=sa.text("'change'"), nullable=False),
    )
    op.create_check_constraint(
        "category", "audit_event", "category IN ('change', 'access', 'denial')"
    )
    op.add_column("audit_event", sa.Column("detail", postgresql.JSONB, nullable=True))
    op.add_column(
        "audit_event",
        sa.Column(
            "leaf_folder_id",
            postgresql.UUID(as_uuid=True),
            sa.Computed("scope_path[array_upper(scope_path, 1)]", persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_audit_event_time", "audit_event", [sa.text("occurred_at DESC"), sa.text("id DESC")]
    )
    op.create_index(
        "ix_audit_event_leaf_time",
        "audit_event",
        ["leaf_folder_id", sa.text("occurred_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_audit_event_aggregate_time",
        "audit_event",
        ["aggregate_type", "aggregate_id", sa.text("occurred_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_audit_event_actor_time",
        "audit_event",
        [sa.text("(actor ->> 'id')"), sa.text("occurred_at DESC"), sa.text("id DESC")],
    )
    op.drop_index("ix_audit_event_occurred_at", table_name="audit_event")
    op.drop_index("ix_audit_event_aggregate_id", table_name="audit_event")


def downgrade() -> None:
    op.create_index("ix_audit_event_aggregate_id", "audit_event", ["aggregate_id"])
    op.create_index("ix_audit_event_occurred_at", "audit_event", ["occurred_at"])
    op.drop_index("ix_audit_event_actor_time", table_name="audit_event")
    op.drop_index("ix_audit_event_aggregate_time", table_name="audit_event")
    op.drop_index("ix_audit_event_leaf_time", table_name="audit_event")
    op.drop_index("ix_audit_event_time", table_name="audit_event")
    op.drop_column("audit_event", "leaf_folder_id")
    op.drop_column("audit_event", "detail")
    op.drop_constraint("category", "audit_event", type_="check")
    op.drop_column("audit_event", "category")
