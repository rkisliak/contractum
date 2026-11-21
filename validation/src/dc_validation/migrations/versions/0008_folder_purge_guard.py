"""folder purge: a narrow, transaction-scoped delete bypass for rule-set versions

Quick 261119-0v1: deleting a folder permanently removes its rule sets that are not in use (not
mandatory) together with their published versions. ``rule_set_version_reject_delete`` (0002) still
refuses every delete **unless** the transaction set ``dc.folder_purge`` to ``on``
(``set_config(..., true)``, transaction-local) **and** the version's rule set is folder-scoped and
not mandatory. A version of a mandatory or a platform rule set can never be deleted. The update
trigger (content immutability) is untouched. The downgrade restores the 0002 body.

Revision ID: 0008_folder_purge_guard
Revises: 0007_rule_set_pause
Create Date: 2026-11-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_folder_purge_guard"
down_revision: str | None = "0007_rule_set_pause"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURGE = """
CREATE OR REPLACE FUNCTION rule_set_version_reject_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('dc.folder_purge', true) = 'on'
       AND EXISTS (
           SELECT 1 FROM rule_set r
           WHERE r.id = OLD.rule_set_id AND r.scope = 'folder' AND NOT r.mandatory
       )
    THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'published rule-set versions cannot be deleted'
        USING ERRCODE = 'raise_exception';
END;
$$
"""

_ORIGINAL = """
CREATE OR REPLACE FUNCTION rule_set_version_reject_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'published rule-set versions cannot be deleted'
        USING ERRCODE = 'raise_exception';
END;
$$
"""


def upgrade() -> None:
    op.execute(_PURGE)


def downgrade() -> None:
    op.execute(_ORIGINAL)
