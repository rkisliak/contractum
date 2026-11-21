"""breaking-change notice type: ``contract.version.breaking_published`` is a known event (SUB-08)

The notice kinds are a CHECK constraint (``ck_card_notice_event_type_known``), so a new kind needs a
new revision (D-162). The breaking notice is sent in addition to ``contract.version.published``,
never instead of it. The downgrade deletes the notices of the new kind (and their deliveries) before
it restores the previous constraint, because a row of the new kind would violate it.

Revision ID: 0016_breaking_notice_type
Revises: 0015_rule_set_attachments
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_breaking_notice_type"
down_revision: str | None = "0015_rule_set_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_card_notice_event_type_known"
_NEW_TYPE = "'contract.version.breaking_published'"
_PREVIOUS = (
    "'contract.version.published', 'contract.latest.changed', 'contract.version.deprecated', "
    "'contract.version.sunset_approaching', 'contract.version.unpublish_blocked', "
    "'card.subscription.removed'"
)


def _replace_check(types: str) -> None:
    op.execute(f"ALTER TABLE card_notice DROP CONSTRAINT {_CONSTRAINT}")  # noqa: S608
    op.execute(
        f"ALTER TABLE card_notice ADD CONSTRAINT {_CONSTRAINT} CHECK (event_type IN ({types}))"
    )


def upgrade() -> None:
    _replace_check(f"{_PREVIOUS}, {_NEW_TYPE}")


def downgrade() -> None:
    # ``_NEW_TYPE`` is a module constant, never input.
    op.execute(
        "DELETE FROM card_notice_delivery WHERE notice_id IN "  # noqa: S608
        f"(SELECT id FROM card_notice WHERE event_type = {_NEW_TYPE})"
    )
    op.execute(f"DELETE FROM card_notice WHERE event_type = {_NEW_TYPE}")  # noqa: S608
    _replace_check(_PREVIOUS)
