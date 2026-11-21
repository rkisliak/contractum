"""rule sets: rule_set, rule_set_version and the published-version immutability triggers

D-109, D-113, D-114: a rule set is an editable draft plus immutable numbered published versions;
rules are versioned only through their set. The holder is a ``scope`` (``folder`` or ``platform``)
so the seeded platform set needs no ROOT folder id (research Pitfall 5): the check constraint
``scope_folder_consistent`` refuses a platform row with a folder and a folder row without one.

The triggers make "immutable" true under direct SQL as well: ``BEFORE UPDATE OF <content
columns>`` fires only when one of the listed columns appears in the SET list, so deprecating a
version (``deprecated_at``/``deprecated_by``) and the audit-column stamps stay possible, and a
version row can never be deleted.

Revision ID: 0002_rule_sets
Revises: 0001_validation_baseline
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0002_rule_sets"
down_revision: str | None = "0001_validation_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")
    )


def upgrade() -> None:
    op.create_table(
        "rule_set",
        _uuid_pk(),
        sa.Column("scope", sa.Text, nullable=False, server_default=sa.text("'folder'")),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("folder_kind", sa.Text, nullable=True),
        sa.Column(
            "ancestor_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("publish_counter", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "draft_rules",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("draft_revision", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("draft_content_sha256", sa.Text, nullable=False),
        *audit_columns(),
        sa.CheckConstraint("scope IN ('folder', 'platform')", name="scope_known"),
        sa.CheckConstraint(
            "((scope = 'folder') = (folder_id IS NOT NULL))"
            " AND ((folder_id IS NULL) = (folder_kind IS NULL))",
            name="scope_folder_consistent",
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        sa.CheckConstraint("length(description) <= 2000", name="description_length"),
    )
    op.create_index(
        "uq_rule_set_folder_name",
        "rule_set",
        ["folder_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("scope = 'folder'"),
    )
    op.create_index(
        "uq_rule_set_platform_name",
        "rule_set",
        [sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("scope = 'platform'"),
    )
    op.create_index(
        "ix_rule_set_ancestor_ids", "rule_set", ["ancestor_ids"], postgresql_using="gin"
    )

    op.create_table(
        "rule_set_version",
        _uuid_pk(),
        sa.Column("rule_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("dsl_version", sa.Text, nullable=False),
        sa.Column("rules", postgresql.JSONB, nullable=False),
        sa.Column("content_sha256", sa.Text, nullable=False),
        sa.Column("rule_keys", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deprecated_by", sa.Text, nullable=True),
        *audit_columns(),
        sa.ForeignKeyConstraint(["rule_set_id"], ["rule_set.id"]),
        sa.UniqueConstraint(
            "rule_set_id", "version_no", name="uq_rule_set_version_rule_set_id_version_no"
        ),
    )

    op.execute(
        """
        CREATE FUNCTION rule_set_version_reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'published rule-set versions are immutable'
                USING ERRCODE = 'raise_exception';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_rule_set_version_immutable
        BEFORE UPDATE OF rule_set_id, version_no, dsl_version, rules, content_sha256, rule_keys
        ON rule_set_version
        FOR EACH ROW EXECUTE FUNCTION rule_set_version_reject_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION rule_set_version_reject_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'published rule-set versions cannot be deleted'
                USING ERRCODE = 'raise_exception';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_rule_set_version_no_delete
        BEFORE DELETE ON rule_set_version
        FOR EACH ROW EXECUTE FUNCTION rule_set_version_reject_delete()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_rule_set_version_no_delete ON rule_set_version")
    op.execute("DROP FUNCTION IF EXISTS rule_set_version_reject_delete()")
    op.execute("DROP TRIGGER IF EXISTS trg_rule_set_version_immutable ON rule_set_version")
    op.execute("DROP FUNCTION IF EXISTS rule_set_version_reject_change()")
    op.drop_table("rule_set_version")
    op.drop_index("ix_rule_set_ancestor_ids", table_name="rule_set")
    op.drop_index("uq_rule_set_platform_name", table_name="rule_set")
    op.drop_index("uq_rule_set_folder_name", table_name="rule_set")
    op.drop_table("rule_set")
