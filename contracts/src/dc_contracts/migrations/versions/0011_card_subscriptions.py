"""card subscriptions: the versions a card depends on, with history that is never lost (SUB-03)

One row per pin (D-89): ``card_subscription`` records which published version a card depends on,
who pinned it and when, and, once ended, who ended it, when, why and what replaced it. Rows are
never deleted and an ended row never changes again (D-84): a delete guard and a guard trigger that
compares whole rows minus an allow-list (so a column added later is immutable until a migration
lists it) enforce it in the database, not only in the service.

The D-83 seams are present and switched off: ``mode`` is ``pinned`` only and the new
``contract.subscription_policy`` is ``open`` only, both by CHECK constraint, so turning either on
is a reviewed migration. No backfill: no card pins anything before this revision.

Revision ID: 0011_card_subscriptions
Revises: 0010_card_members
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0011_card_subscriptions"
down_revision: str | None = "0010_card_members"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD_FUNCTION = """
CREATE FUNCTION card_subscription_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    ending_mutable text[] := ARRAY[
        'ended_at', 'ended_by', 'end_reason', 'end_note', 'replaced_by_id',
        'updated_by', 'updated_at'
    ];
BEGIN
    IF OLD.ended_at IS NOT NULL THEN
        RAISE EXCEPTION 'an ended card subscription cannot change'
            USING ERRCODE = 'raise_exception';
    END IF;
    IF (to_jsonb(NEW) - ending_mutable) IS DISTINCT FROM (to_jsonb(OLD) - ending_mutable) THEN
        RAISE EXCEPTION 'a card subscription can only be ended, never rewritten'
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NEW;
END;
$$
"""

_NO_DELETE_FUNCTION = """
CREATE FUNCTION card_subscription_no_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'card subscriptions are history and cannot be deleted'
        USING ERRCODE = 'raise_exception';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "card_subscription",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mode", sa.Text, nullable=False, server_default=sa.text("'pinned'")),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_by", sa.Text, nullable=True),
        sa.Column("end_reason", sa.Text, nullable=True),
        sa.Column("end_note", sa.Text, nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        *audit_columns(),
        sa.CheckConstraint("mode IN ('pinned')", name="mode_known"),
        sa.CheckConstraint(
            "end_reason IN ('moved', 'unsubscribed', 'removed_by_admin', 'card_archived')",
            name="end_reason_known",
        ),
        sa.CheckConstraint("char_length(end_note) <= 500", name="end_note_length"),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (ended_by IS NULL) AND (ended_at IS NULL) = (end_reason IS NULL)",
            name="ended_consistent",
        ),
        sa.ForeignKeyConstraint(["card_id"], ["subscriber_card.id"]),
        sa.ForeignKeyConstraint(["contract_id"], ["contract.id"]),
        sa.ForeignKeyConstraint(["version_id"], ["contract_version.id"]),
        sa.ForeignKeyConstraint(["replaced_by_id"], ["card_subscription.id"]),
    )
    # A card pins a given version at most once at a time; ended pins stay as history.
    op.create_index(
        "uq_card_subscription_active",
        "card_subscription",
        ["card_id", "version_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_card_subscription_active_contract",
        "card_subscription",
        ["contract_id"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_card_subscription_active_version",
        "card_subscription",
        ["version_id"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index("ix_card_subscription_card", "card_subscription", ["card_id", "id"])

    op.execute(_GUARD_FUNCTION)
    op.execute(_NO_DELETE_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_card_subscription_guard BEFORE UPDATE ON card_subscription "
        "FOR EACH ROW EXECUTE FUNCTION card_subscription_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_card_subscription_no_delete BEFORE DELETE ON card_subscription "
        "FOR EACH ROW EXECUTE FUNCTION card_subscription_no_delete()"
    )

    op.add_column(
        "contract",
        sa.Column("subscription_policy", sa.Text, nullable=False, server_default=sa.text("'open'")),
    )
    op.create_check_constraint(
        "subscription_policy_known", "contract", "subscription_policy IN ('open')"
    )


def downgrade() -> None:
    op.drop_constraint("subscription_policy_known", "contract", type_="check")
    op.drop_column("contract", "subscription_policy")
    op.execute("DROP TRIGGER trg_card_subscription_no_delete ON card_subscription")
    op.execute("DROP TRIGGER trg_card_subscription_guard ON card_subscription")
    op.execute("DROP FUNCTION card_subscription_no_delete()")
    op.execute("DROP FUNCTION card_subscription_guard()")
    op.drop_index("ix_card_subscription_card", table_name="card_subscription")
    op.drop_index("ix_card_subscription_active_version", table_name="card_subscription")
    op.drop_index("ix_card_subscription_active_contract", table_name="card_subscription")
    op.drop_index("uq_card_subscription_active", table_name="card_subscription")
    op.drop_table("card_subscription")
