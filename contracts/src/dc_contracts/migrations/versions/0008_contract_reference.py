"""contract reference: links from published versions to other contracts (CONT-04, D-54)

One row per recognised link in a published version, keyed by the link text (``ref_kind`` is a
contract uuid or an ODCS id). Rows are derived from an already audited publish, written once and
never updated or deleted. The link is resolved to a target only when a delete asks, so a target
created after its referrer was published is still protected. No backfill: Phase 3 ships as one
deployment, so no published version exists before this revision outside test databases.

Revision ID: 0008_contract_reference
Revises: 0007_contract_soft_delete
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0008_contract_reference"
down_revision: str | None = "0007_contract_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contract_reference",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ref_kind", sa.Text, nullable=False),
        sa.Column("ref_value", sa.Text, nullable=False),
        sa.Column("pointer", sa.Text, nullable=False),
        *audit_columns(),
        sa.ForeignKeyConstraint(["source_version_id"], ["contract_version.id"]),
        sa.ForeignKeyConstraint(["source_contract_id"], ["contract.id"]),
        sa.CheckConstraint("ref_kind IN ('contract_uuid', 'odcs_id')", name="ref_kind_known"),
        sa.UniqueConstraint(
            "source_version_id",
            "ref_kind",
            "ref_value",
            name="uq_contract_reference_version_kind_value",
        ),
    )
    op.create_index(
        "ix_contract_reference_kind_value", "contract_reference", ["ref_kind", "ref_value"]
    )


def downgrade() -> None:
    op.drop_index("ix_contract_reference_kind_value", table_name="contract_reference")
    op.drop_table("contract_reference")
