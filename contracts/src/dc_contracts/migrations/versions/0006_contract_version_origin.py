"""contract origin: which contract and version a contract was copied from (D-44b)

A contract created from a version or a draft of another contract is a copy; the two nullable
columns only record where it came from. The pair CHECK keeps them both set or both null.
``origin_contract_id`` references the source contract (contracts are only ever soft-deleted, so
the target always exists). ``origin_version_id`` has no foreign key: it is provenance, like
``base_version_id``, because a draft source may later be discarded and a composite foreign key to
``contract_version`` would then block the discard or dangle.

Revision ID: 0006_contract_version_origin
Revises: 0005_contract_version_lifecycle
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_contract_version_origin"
down_revision: str | None = "0005_contract_version_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contract", sa.Column("origin_contract_id", postgresql.UUID(as_uuid=True)))
    op.add_column("contract", sa.Column("origin_version_id", postgresql.UUID(as_uuid=True)))
    op.create_check_constraint(
        "version_origin_pair",
        "contract",
        "(origin_contract_id IS NULL) = (origin_version_id IS NULL)",
    )
    op.create_foreign_key(
        "fk_contract_origin_contract",
        "contract",
        "contract",
        ["origin_contract_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_contract_origin_contract", "contract", type_="foreignkey")
    # the naming convention prefixes the name again, so pass the short one
    op.drop_constraint("version_origin_pair", "contract", type_="check")
    op.drop_column("contract", "origin_version_id")
    op.drop_column("contract", "origin_contract_id")
