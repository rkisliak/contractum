"""card notices: what happened to the versions a card depends on, and how it was delivered (SUB-07)

``card_notice`` is the card's record of one lifecycle event (D-100): a notice per card and event,
whatever the card's channels. The unique ``uq_card_notice_card_dedupe`` (card id, ``event:<id>``)
is what makes the materialiser exactly-once under redelivery. ``card_notice_delivery`` is one row
per channel a notice must be sent on (D-98); the delivery plans drain it. ``contract_id`` and
``version_id`` carry no foreign key: a notice is a record of something that happened and must not
constrain a later operation (precedent: ``contract_version.base_version_id``).

Revision ID: 0012_card_notices
Revises: 0011_card_subscriptions
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_card_notices"
down_revision: str | None = "0011_card_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_TYPES = (
    "'contract.version.published', 'contract.latest.changed', 'contract.version.deprecated', "
    "'contract.version.sunset_approaching', 'contract.version.unpublish_blocked', "
    "'card.subscription.removed'"
)


def upgrade() -> None:
    op.create_table(
        "card_notice",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("dedupe_key", sa.Text, nullable=False),
        sa.Column("source_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(f"event_type IN ({_EVENT_TYPES})", name="event_type_known"),
        sa.ForeignKeyConstraint(["card_id"], ["subscriber_card.id"]),
        sa.UniqueConstraint("card_id", "dedupe_key", name="uq_card_notice_card_dedupe"),
    )
    op.create_index("ix_card_notice_card_id", "card_notice", ["card_id", "id"])
    op.create_index(
        "ix_card_notice_version_type",
        "card_notice",
        ["version_id", "event_type", "occurred_at"],
    )

    op.create_table(
        "card_notice_delivery",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("notice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Integer, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("channel IN ('email', 'webhook')", name="channel_known"),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed', 'skipped')", name="status_known"
        ),
        sa.CheckConstraint("char_length(last_error) <= 200", name="last_error_length"),
        sa.ForeignKeyConstraint(["notice_id"], ["card_notice.id"]),
        sa.UniqueConstraint("notice_id", "channel", name="uq_card_notice_delivery_channel"),
    )
    op.create_index(
        "ix_card_notice_delivery_due",
        "card_notice_delivery",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_card_notice_delivery_due", table_name="card_notice_delivery")
    op.drop_table("card_notice_delivery")
    op.drop_index("ix_card_notice_version_type", table_name="card_notice")
    op.drop_index("ix_card_notice_card_id", table_name="card_notice")
    op.drop_table("card_notice")
