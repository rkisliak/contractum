"""rbac: block-inheritance flag, local groups, group members, role check, principal index

D-27 (group ownership tables), D-29 (block flag on Organization and Team folders). The Phase 1
immutability trigger is ``BEFORE UPDATE OF parent_id, slug, path, kind``, so flipping the new
column never fires it.

Revision ID: 0003_rbac
Revises: 0002_folder_guards
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns
from sqlalchemy.dialects import postgresql

revision: str = "0003_rbac"
down_revision: str | None = "0002_folder_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLES = (
    "global_admin",
    "global_reader",
    "org_admin",
    "org_reader",
    "project_admin",
    "project_editor",
    "team_admin",
    "team_editor",
    "team_reader",
    "policy_manager",
    "consumer",
)
_ROLE_VALUES = "role IN (" + ", ".join(f"'{role}'" for role in _ROLES) + ")"


def upgrade() -> None:
    op.add_column(
        "folder",
        sa.Column("block_inheritance", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_check_constraint(
        "block_flag_kinds", "folder", "NOT block_inheritance OR kind IN ('organization', 'team')"
    )

    op.create_table(
        "local_groups",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("owner_folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        *audit_columns(),
        sa.ForeignKeyConstraint(["owner_folder_id"], ["folder.id"]),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 100", name="name_length"),
    )
    op.create_index(
        "uq_local_groups_owner_lower_name",
        "local_groups",
        ["owner_folder_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "group_members",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_type", sa.Text, nullable=False),
        sa.Column("member_id", sa.Text, nullable=False),
        *audit_columns(),
        sa.ForeignKeyConstraint(["group_id"], ["local_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", "member_type", "member_id"),
        sa.CheckConstraint("member_type IN ('user', 'service_account')", name="member_type_values"),
    )
    op.create_index("ix_group_members_member", "group_members", ["member_type", "member_id"])

    op.create_check_constraint("role_values", "role_assignments", _ROLE_VALUES)
    op.create_index(
        "ix_role_assignments_principal", "role_assignments", ["principal_type", "principal_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_role_assignments_principal", table_name="role_assignments")
    op.drop_constraint("role_values", "role_assignments", type_="check")
    op.drop_index("ix_group_members_member", table_name="group_members")
    op.drop_table("group_members")
    op.drop_index("uq_local_groups_owner_lower_name", table_name="local_groups")
    op.drop_table("local_groups")
    op.drop_constraint("block_flag_kinds", "folder", type_="check")
    op.drop_column("folder", "block_inheritance")
