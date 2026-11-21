"""contract soft delete: who removed a contract and when (CONT-04, D-55)

An editor removes a contract by setting ``deleted_at`` and ``deleted_by``; no row is ever deleted
or rewritten otherwise. The pair CHECK keeps both set or both null. The unique constraint
``uq_contract_organization_id_odcs_id`` is deliberately left alone, so the ODCS id of a deleted
contract stays taken in its Organization (D-55). An Organization or Global admin restores a
contract by clearing both columns (D-56).

Revision ID: 0007_contract_soft_delete
Revises: 0006_contract_version_origin
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_contract_soft_delete"
down_revision: str | None = "0006_contract_version_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contract", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column("contract", sa.Column("deleted_by", sa.Text()))
    op.create_check_constraint(
        "deleted_pair",
        "contract",
        "(deleted_at IS NULL) = (deleted_by IS NULL)",
    )


def downgrade() -> None:
    # the naming convention prefixes the name again, so pass the short one
    op.drop_constraint("deleted_pair", "contract", type_="check")
    op.drop_column("contract", "deleted_by")
    op.drop_column("contract", "deleted_at")
