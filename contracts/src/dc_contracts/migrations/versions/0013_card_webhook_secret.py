"""card webhook secret: the per-card signing secret, encrypted at rest (SUB-07, D-98, D-107)

One row per card that has ever been given a webhook secret. ``secret_enc`` is a Fernet token of the
``whsec_`` value; ``previous_enc`` and ``previous_valid_until`` keep the one earlier secret that is
still accepted during the rotation overlap; ``generation`` counts issued secrets. The table is plain
(no audit columns) and is never passed to ``UnitOfWork.snapshot``: no plaintext, key part or token
may reach an audit event. No backfill: no card has a secret before this revision.

Revision ID: 0013_card_webhook_secret
Revises: 0012_card_notices
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_card_webhook_secret"
down_revision: str | None = "0012_card_notices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "card_webhook_secret",
        sa.Column("card_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("secret_enc", sa.Text, nullable=False),
        sa.Column("previous_enc", sa.Text, nullable=True),
        sa.Column("previous_valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generation", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("generation >= 1", name="generation_positive"),
        sa.CheckConstraint(
            "(previous_enc IS NULL) = (previous_valid_until IS NULL)", name="previous_pair"
        ),
        sa.ForeignKeyConstraint(["card_id"], ["subscriber_card.id"]),
    )


def downgrade() -> None:
    op.drop_table("card_webhook_secret")
