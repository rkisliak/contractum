"""validation_run: the immutable record of a publish-gate validation run

D-128, D-133: every publish run is stored (resolved pins, engine and DSL versions, schema hash, the
full report, the verdict) and kept forever; Phase 6 reads the stored runs. A run is a derived
record: the audited fact is ``contract.version_published``, which carries the run id, so storing a
run emits no audit event of its own (research A6) and the table has no audit columns.

The triggers make "immutable" true under direct SQL as well: any UPDATE or DELETE is refused.

Revision ID: 0004_validation_runs
Revises: 0003_rule_set_mandatory
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_validation_runs"
down_revision: str | None = "0003_rule_set_mandatory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "validation_run",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_sha256", sa.Text, nullable=False),
        sa.Column("resolved", postgresql.JSONB, nullable=False),
        sa.Column("engine_version", sa.Text, nullable=False),
        sa.Column("dsl_version", sa.Text, nullable=False),
        sa.Column("schema_sha", sa.Text, nullable=False),
        sa.Column("report", postgresql.JSONB, nullable=False),
        sa.Column("publishable", sa.Boolean, nullable=False),
        sa.Column("requested_by", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("mode IN ('publish')", name="mode_known"),
    )
    op.create_index("ix_validation_run_contract", "validation_run", ["contract_id", "created_at"])
    op.execute(
        """
        CREATE FUNCTION validation_run_reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'validation runs are immutable'
                USING ERRCODE = 'raise_exception';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validation_run_immutable
        BEFORE UPDATE ON validation_run
        FOR EACH ROW EXECUTE FUNCTION validation_run_reject_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validation_run_reject_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'validation runs cannot be deleted'
                USING ERRCODE = 'raise_exception';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validation_run_no_delete
        BEFORE DELETE ON validation_run
        FOR EACH ROW EXECUTE FUNCTION validation_run_reject_delete()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_validation_run_no_delete ON validation_run")
    op.execute("DROP FUNCTION IF EXISTS validation_run_reject_delete()")
    op.execute("DROP TRIGGER IF EXISTS trg_validation_run_immutable ON validation_run")
    op.execute("DROP FUNCTION IF EXISTS validation_run_reject_change()")
    op.drop_index("ix_validation_run_contract", table_name="validation_run")
    op.drop_table("validation_run")
