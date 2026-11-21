"""card members: groups and service accounts linked to a subscriber card (SUB-02, D-85)

One row per link, keyed ``(card_id, member_type, member_id)`` so linking the same member twice is
one row. ``member_id`` is identity's id for the member (a bare local-group uuid, a directory
group GUID or ``sa:<uuid>``); identity decides who may be linked, so there is no foreign key to its
database. Links are deleted when removed (the audit event records the change). No backfill.

Revision ID: 0010_card_members
Revises: 0009_subscriber_cards
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0010_card_members"
down_revision: str | None = "0009_subscriber_cards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "card_member",
        sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_type", sa.Text, nullable=False),
        sa.Column("member_id", sa.Text, nullable=False),
        *audit_columns(),
        sa.CheckConstraint(
            "member_type IN ('local_group', 'entra_group', 'service_account')",
            name="member_type_known",
        ),
        sa.CheckConstraint("char_length(member_id) BETWEEN 1 AND 200", name="member_id_length"),
        sa.ForeignKeyConstraint(["card_id"], ["subscriber_card.id"]),
        sa.PrimaryKeyConstraint("card_id", "member_type", "member_id"),
    )


def downgrade() -> None:
    op.drop_table("card_member")
