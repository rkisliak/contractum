"""subscriber cards: what a consuming team depends on (SUB-01, SUB-04, D-82, D-97, D-104)

One row per card. A card is owned by one team or project folder (``folder_id``; ``ancestor_ids``
and ``organization_id`` come from identity's folder facts, never from the client). Contacts are a
validated list in ``contacts``; the delivery channel columns (``email_enabled``,
``webhook_enabled``, ``webhook_url``) exist from the start so later plans only fill them in. A card
is archived, never deleted: ``archived_at`` and ``archived_by`` are set together. Names are unique
per folder among non-archived cards, compared with ``lower()`` and no Unicode normalisation (A14),
so an archived card's name can be reused. No backfill: no card exists before this revision.

Revision ID: 0009_subscriber_cards
Revises: 0008_contract_reference
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0009_subscriber_cards"
down_revision: str | None = "0008_contract_reference"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscriber_card",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ancestor_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column(
            "contacts",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("email_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("webhook_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("webhook_url", sa.Text, nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", sa.Text, nullable=True),
        *audit_columns(),
        sa.CheckConstraint("(archived_at IS NULL) = (archived_by IS NULL)", name="archived_pair"),
        sa.CheckConstraint(
            "NOT webhook_enabled OR webhook_url IS NOT NULL", name="webhook_needs_url"
        ),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="name_length"),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 2000", name="description_length"
        ),
        sa.CheckConstraint("jsonb_typeof(contacts) = 'array'", name="contacts_is_array"),
    )
    op.create_index(
        "uq_subscriber_card_folder_lower_name",
        "subscriber_card",
        ["folder_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_index(
        "ix_subscriber_card_ancestor_ids",
        "subscriber_card",
        ["ancestor_ids"],
        postgresql_using="gin",
    )
    op.create_index("ix_subscriber_card_folder_id", "subscriber_card", ["folder_id"])


def downgrade() -> None:
    op.drop_index("ix_subscriber_card_folder_id", table_name="subscriber_card")
    op.drop_index("ix_subscriber_card_ancestor_ids", table_name="subscriber_card")
    op.drop_index("uq_subscriber_card_folder_lower_name", table_name="subscriber_card")
    op.drop_table("subscriber_card")
