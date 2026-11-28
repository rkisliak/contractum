"""contract (folder_id, id) index for the Folders tree

Phase 16, D-07: the Folders tree lists the contracts of each expanded folder with one list
statement per folder (``folder_id = :f`` ordered by the name key and the id). EXPLAIN on a seeded
10,000-contract / 1,000-folder dataset showed a sequential scan of ``contract`` for that statement
(``contract.folder_id`` had no index), so this adds ``ix_contract_folder_id (folder_id, id)``.
Additive only: the downgrade drops the index and changes no data.

Revision ID: 0019_contract_folder_id_index
Revises: 0018_card_organization_nullable
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019_contract_folder_id_index"
down_revision: str | None = "0018_card_organization_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_contract_folder_id", "contract", ["folder_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_contract_folder_id", table_name="contract")
