"""contract template origin: which template version a contract was started from (CONT-02, D-38)

A contract made from a template is a copy; the two nullable columns only record where it came
from. The pair CHECK keeps them both set or both null, and the composite foreign key to
``template_version (template_id, version_no)`` means an origin can only name a version that
exists (versions are undeletable, see revision 0002).

Revision ID: 0003_contract_template_origin
Revises: 0002_templates
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_contract_template_origin"
down_revision: str | None = "0002_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contract", sa.Column("template_id", postgresql.UUID(as_uuid=True)))
    op.add_column("contract", sa.Column("template_version", sa.Integer))
    op.create_check_constraint(
        "template_origin_pair",
        "contract",
        "(template_id IS NULL) = (template_version IS NULL)",
    )
    op.create_foreign_key(
        "fk_contract_template_origin",
        "contract",
        "template_version",
        ["template_id", "template_version"],
        ["template_id", "version_no"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_contract_template_origin", "contract", type_="foreignkey")
    # the naming convention prefixes the name again, so pass the short one
    op.drop_constraint("template_origin_pair", "contract", type_="check")
    op.drop_column("contract", "template_version")
    op.drop_column("contract", "template_id")
