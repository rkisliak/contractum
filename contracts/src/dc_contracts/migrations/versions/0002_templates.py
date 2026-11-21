"""templates: template, template_version and the published-version immutability triggers

D-37: a template is an editable draft plus immutable numbered published versions. The triggers
make "immutable" true under direct SQL as well: ``BEFORE UPDATE OF <content columns>`` fires only
when one of the listed columns appears in the SET list, so deprecating a version
(``deprecated_at``/``deprecated_by``) and the audit-column stamps stay possible, and a version row
can never be deleted.

Revision ID: 0002_templates
Revises: 0001_contracts_baseline
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0002_templates"
down_revision: str | None = "0001_contracts_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")
    )


def upgrade() -> None:
    op.create_table(
        "template",
        _uuid_pk(),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ancestor_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("draft_raw_document", sa.LargeBinary, nullable=False),
        sa.Column("draft_media_type", sa.Text, nullable=False),
        sa.Column("draft_content_sha256", sa.Text, nullable=False),
        sa.Column("draft_size_bytes", sa.Integer, nullable=False),
        sa.Column("draft_document", postgresql.JSONB, nullable=False),
        sa.Column("draft_api_version", sa.Text, nullable=False),
        sa.Column("publish_counter", sa.Integer, nullable=False, server_default=sa.text("0")),
        *audit_columns(),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
    )
    op.create_index(
        "ix_template_ancestor_ids", "template", ["ancestor_ids"], postgresql_using="gin"
    )

    op.create_table(
        "template_version",
        _uuid_pk(),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("odcs_api_version", sa.Text, nullable=False),
        sa.Column("raw_document", sa.LargeBinary, nullable=False),
        sa.Column("media_type", sa.Text, nullable=False),
        sa.Column("content_sha256", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("document", postgresql.JSONB, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deprecated_by", sa.Text, nullable=True),
        *audit_columns(),
        sa.ForeignKeyConstraint(["template_id"], ["template.id"]),
        sa.UniqueConstraint(
            "template_id", "version_no", name="uq_template_version_template_id_version_no"
        ),
    )

    op.execute(
        """
        CREATE FUNCTION template_version_reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'published template versions are immutable'
                USING ERRCODE = 'raise_exception';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_template_version_immutable
        BEFORE UPDATE OF template_id, version_no, odcs_api_version, raw_document, media_type,
            content_sha256, size_bytes, document, name ON template_version
        FOR EACH ROW EXECUTE FUNCTION template_version_reject_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION template_version_reject_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'published template versions cannot be deleted'
                USING ERRCODE = 'raise_exception';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_template_version_no_delete
        BEFORE DELETE ON template_version
        FOR EACH ROW EXECUTE FUNCTION template_version_reject_delete()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_template_version_no_delete ON template_version")
    op.execute("DROP FUNCTION IF EXISTS template_version_reject_delete()")
    op.execute("DROP TRIGGER IF EXISTS trg_template_version_immutable ON template_version")
    op.execute("DROP FUNCTION IF EXISTS template_version_reject_change()")
    op.drop_table("template_version")
    op.drop_index("ix_template_ancestor_ids", table_name="template")
    op.drop_table("template")
