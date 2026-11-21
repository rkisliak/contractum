"""subscriber_card.organization_id becomes nullable (cards at ROOT)

Quick 261119-wo3: a subscriber card may be owned by a folder of any kind. A card owned by the ROOT
folder has no Organization, so ``organization_id`` is NULL for it (the column stays NOT NULL for
every other kind by the application, which copies identity's folder facts). The downgrade refuses
while a NULL row exists, because restoring NOT NULL would otherwise fail halfway.

Revision ID: 0018_card_organization_nullable
Revises: 0017_folder_purge_guards
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_card_organization_nullable"
down_revision: str | None = "0017_folder_purge_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "subscriber_card",
        "organization_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM subscriber_card WHERE organization_id IS NULL) "
            "THEN RAISE EXCEPTION 'cards owned by the ROOT folder exist; archive and purge them "
            "before downgrading'; END IF; END $$"
        )
    )
    op.alter_column(
        "subscriber_card",
        "organization_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
