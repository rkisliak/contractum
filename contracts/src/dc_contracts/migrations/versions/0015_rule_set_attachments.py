"""rule-set attachments: the extra published rule sets a contract owner attaches (RULE-06, D-123)

``contract_ruleset_attachment`` holds one row per (contract, rule set): the rule set id and a name
snapshot (no foreign key: the rule sets live in the validation service's database) and the version
spec, ``latest`` or an exact version number. ``contract.attachments_revision`` counts every attach,
re-pin and detach, so a publish can prove the attachments it validated are the ones it publishes
(D-124, D-131). Nothing is backfilled: no contract has attachments before this revision.

Revision ID: 0015_rule_set_attachments
Revises: 0014_contract_version_pins
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0015_rule_set_attachments"
down_revision: str | None = "0014_contract_version_pins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contract",
        sa.Column("attachments_revision", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.create_table(
        "contract_ruleset_attachment",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_set_name", sa.Text, nullable=False),
        sa.Column("version_spec", sa.Text, nullable=False),
        *audit_columns(),
        sa.CheckConstraint(
            "version_spec = 'latest' OR version_spec ~ '^[1-9][0-9]{0,9}$'",
            name="version_spec_known",
        ),
        sa.ForeignKeyConstraint(["contract_id"], ["contract.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "contract_id", "rule_set_id", name="uq_contract_ruleset_attachment_contract_rule_set"
        ),
    )


def downgrade() -> None:
    op.drop_table("contract_ruleset_attachment")
    op.drop_column("contract", "attachments_revision")
