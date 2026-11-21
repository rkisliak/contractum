"""folder purge: a narrow, transaction-scoped delete bypass for the immutable history tables

Quick 261119-0v1: deleting a folder permanently removes the contracts that were already
soft-deleted and the subscriber cards that were already archived, together with their rows.
Published contract versions and card subscriptions are immutable history, so their delete-refusing
triggers (0005, 0011) are replaced by versions that still refuse every delete **unless** the
transaction set ``dc.folder_purge`` to ``on`` (``set_config(..., true)`` is transaction-local)
**and** the row belongs to a purgeable parent:

* ``contract_version``: the parent contract is soft-deleted (``deleted_at IS NOT NULL``). A version
  of a live contract can never be deleted, whatever the setting says.
* ``card_subscription``: the pin has ended (``ended_at IS NOT NULL``) and either the contract is
  soft-deleted or the card is archived. An active pin can never be deleted.

Every other behaviour of both triggers is unchanged; the downgrade restores the original bodies.

Revision ID: 0017_folder_purge_guards
Revises: 0016_breaking_notice_type
Create Date: 2026-11-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_folder_purge_guards"
down_revision: str | None = "0016_breaking_notice_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION_DELETE_PURGE = """
CREATE OR REPLACE FUNCTION contract_version_reject_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'draft' THEN
        RETURN OLD;
    END IF;
    IF current_setting('dc.folder_purge', true) = 'on'
       AND EXISTS (
           SELECT 1 FROM contract c WHERE c.id = OLD.contract_id AND c.deleted_at IS NOT NULL
       )
    THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'published contract versions cannot be deleted'
        USING ERRCODE = 'raise_exception';
END;
$$
"""

_VERSION_DELETE_0005 = """
CREATE OR REPLACE FUNCTION contract_version_reject_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state <> 'draft' THEN
        RAISE EXCEPTION 'published contract versions cannot be deleted'
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN OLD;
END;
$$
"""

_SUBSCRIPTION_DELETE_PURGE = """
CREATE OR REPLACE FUNCTION card_subscription_no_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('dc.folder_purge', true) = 'on'
       AND OLD.ended_at IS NOT NULL
       AND (
           EXISTS (
               SELECT 1 FROM contract c WHERE c.id = OLD.contract_id AND c.deleted_at IS NOT NULL
           )
           OR EXISTS (
               SELECT 1 FROM subscriber_card s
               WHERE s.id = OLD.card_id AND s.archived_at IS NOT NULL
           )
       )
    THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'card subscriptions are history and cannot be deleted'
        USING ERRCODE = 'raise_exception';
END;
$$
"""

_SUBSCRIPTION_DELETE_0011 = """
CREATE OR REPLACE FUNCTION card_subscription_no_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'card subscriptions are history and cannot be deleted'
        USING ERRCODE = 'raise_exception';
END;
$$
"""


def upgrade() -> None:
    op.execute(_VERSION_DELETE_PURGE)
    op.execute(_SUBSCRIPTION_DELETE_PURGE)


def downgrade() -> None:
    op.execute(_SUBSCRIPTION_DELETE_0011)
    op.execute(_VERSION_DELETE_0005)
