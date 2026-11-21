"""folder delete guard: only ROOT is undeletable; every other delete is guarded by the FKs

Quick 261119-0v1: the user reversed the earlier planner rule that folders are never deleted
(D-14/D-15 were planner choices). ``trg_folder_no_delete`` refused every delete; it is replaced by
``trg_folder_no_root_delete``, which refuses only the ROOT folder. A folder with children, owned
local groups, service accounts or Entra registrations, or role grants is still refused by the NO
ACTION foreign keys, so under direct SQL only an empty folder whose grants were removed first can
go. Moves, re-slugs and re-kinding stay refused (``trg_folder_immutable``, migration 0002).

Revision ID: 0010_folder_delete_guard
Revises: 0009_outbox_detail
Create Date: 2026-11-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_folder_delete_guard"
down_revision: str | None = "0009_outbox_detail"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_folder_no_delete ON folder")
    op.execute("DROP FUNCTION IF EXISTS folder_reject_delete()")
    op.execute(
        """
        CREATE FUNCTION folder_reject_root_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.kind = 'root' THEN
                RAISE EXCEPTION 'the ROOT folder cannot be deleted'
                    USING ERRCODE = 'raise_exception';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_folder_no_root_delete
        BEFORE DELETE ON folder
        FOR EACH ROW EXECUTE FUNCTION folder_reject_root_delete()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_folder_no_root_delete ON folder")
    op.execute("DROP FUNCTION IF EXISTS folder_reject_root_delete()")
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
