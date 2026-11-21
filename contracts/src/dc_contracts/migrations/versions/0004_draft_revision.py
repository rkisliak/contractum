"""draft revision: each contract version row carries a revision for optimistic concurrency (D-42)

A draft is edited in place; every accepted save bumps ``revision``, and the HTTP layer turns it
into the strong ETag ``"r<revision>"`` that ``If-Match`` is compared against. Existing rows start
at revision 1. The CHECK keeps the counter positive.

Revision ID: 0004_draft_revision
Revises: 0003_contract_template_origin
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_draft_revision"
down_revision: str | None = "0003_contract_template_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contract_version",
        sa.Column("revision", sa.Integer, nullable=False, server_default=sa.text("1")),
    )
    op.create_check_constraint("revision_positive", "contract_version", "revision >= 1")


def downgrade() -> None:
    # the naming convention prefixes the name again, so pass the short one
    op.drop_constraint("revision_positive", "contract_version", type_="check")
    op.drop_column("contract_version", "revision")
