"""contracts baseline: contract, contract_version, outbox

Revision ID: 0001_contracts_baseline
Revises:
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns, outbox_table
from sqlalchemy.dialects import postgresql

revision: str = "0001_contracts_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")
    )


def upgrade() -> None:
    op.create_table(
        "contract",
        _uuid_pk(),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ancestor_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("odcs_id", sa.Text, nullable=False),
        sa.Column("notation", sa.Text, nullable=False, server_default=sa.text("'odcs'")),
        *audit_columns(),
        sa.UniqueConstraint(
            "organization_id", "odcs_id", name="uq_contract_organization_id_odcs_id"
        ),
    )
    op.create_index(
        "ix_contract_ancestor_ids", "contract", ["ancestor_ids"], postgresql_using="gin"
    )

    op.create_table(
        "contract_version",
        _uuid_pk(),
        sa.Column("contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'draft'")),
        sa.Column("odcs_api_version", sa.Text, nullable=False),
        sa.Column("raw_document", sa.LargeBinary, nullable=False),
        sa.Column("media_type", sa.Text, nullable=False),
        sa.Column("content_sha256", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("document", postgresql.JSONB, nullable=False),
        sa.Column("name", sa.Text, nullable=True),
        sa.Column("domain", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=True),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("description_text", sa.Text, nullable=True),
        sa.Column("odcs_version", sa.Text, nullable=True),
        *audit_columns(),
        sa.ForeignKeyConstraint(["contract_id"], ["contract.id"]),
        sa.UniqueConstraint("contract_id", "version_no"),
    )
    op.create_index(
        "ix_contract_version_tags", "contract_version", ["tags"], postgresql_using="gin"
    )
    op.create_index("ix_contract_version_lower_name", "contract_version", [sa.text("lower(name)")])
    op.create_index(
        "ix_contract_version_lower_domain", "contract_version", [sa.text("lower(domain)")]
    )

    outbox_table(op)


def downgrade() -> None:
    op.drop_index("ix_outbox_unpublished", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_contract_version_lower_domain", table_name="contract_version")
    op.drop_index("ix_contract_version_lower_name", table_name="contract_version")
    op.drop_index("ix_contract_version_tags", table_name="contract_version")
    op.drop_table("contract_version")
    op.drop_index("ix_contract_ancestor_ids", table_name="contract")
    op.drop_table("contract")
