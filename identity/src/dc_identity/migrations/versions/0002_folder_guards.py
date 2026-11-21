"""folder guards: reject moves, re-slugs, re-kinding and deletes in the database

D-14, D-15: folders are never moved or hard-deleted. The API has no such route; these triggers
keep it true under direct SQL as well. ``BEFORE UPDATE OF ...`` fires only when one of the listed
columns appears in the SET list, so renames, archiving and the audit-column stamps are unaffected.

Revision ID: 0002_folder_guards
Revises: 0001_identity_baseline
Create Date: 2026-11-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_folder_guards"
down_revision: str | None = "0001_identity_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION folder_reject_structural_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'folders cannot be moved, re-slugged or re-kinded'
                USING ERRCODE = 'raise_exception';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_folder_immutable
        BEFORE UPDATE OF parent_id, slug, path, kind ON folder
        FOR EACH ROW EXECUTE FUNCTION folder_reject_structural_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION folder_reject_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'folders cannot be deleted'
                USING ERRCODE = 'raise_exception';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_folder_no_delete
        BEFORE DELETE ON folder
        FOR EACH ROW EXECUTE FUNCTION folder_reject_delete()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_folder_no_delete ON folder")
    op.execute("DROP FUNCTION IF EXISTS folder_reject_delete()")
    op.execute("DROP TRIGGER IF EXISTS trg_folder_immutable ON folder")
    op.execute("DROP FUNCTION IF EXISTS folder_reject_structural_change()")
