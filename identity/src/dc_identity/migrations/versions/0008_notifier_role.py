"""notifier role: the role check on role_assignments also accepts ``notifier``

D-105: the contracts delivery worker authenticates to the mail service as a ROOT-owned service
account holding the ``notifier`` role (permission ``mail:send`` only). Role names are stored in
``role_assignments.role``, so the check constraint is replaced to list the new name. Downgrading
refuses while a ``notifier`` grant exists, because the old check would reject the row.

Revision ID: 0008_notifier_role
Revises: 0007_session_cutoff
Create Date: 2026-11-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_notifier_role"
down_revision: str | None = "0007_session_cutoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIOUS_ROLES = (
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
_ROLES = (*_PREVIOUS_ROLES, "notifier")


def _role_check(roles: Sequence[str]) -> str:
    return "role IN (" + ", ".join(f"'{role}'" for role in roles) + ")"


def upgrade() -> None:
    op.drop_constraint("role_values", "role_assignments", type_="check")
    op.create_check_constraint("role_values", "role_assignments", _role_check(_ROLES))


def downgrade() -> None:
    held = op.get_bind().exec_driver_sql(
        "SELECT 1 FROM role_assignments WHERE role = 'notifier' LIMIT 1"
    )
    if held.first() is not None:
        raise RuntimeError("remove notifier grants before downgrading")
    op.drop_constraint("role_values", "role_assignments", type_="check")
    op.create_check_constraint("role_values", "role_assignments", _role_check(_PREVIOUS_ROLES))
