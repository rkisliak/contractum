"""user email: optional contact address on local users, with a shape CHECK

RBAC-10 (a Global admin creates local users). The address is optional, never used for login and
never copied into audit events (only the derived ``email_set`` flag is). The CHECK is a cheap
shape guard, not a deliverability check; no email-validator dependency is added.

Revision ID: 0004_user_email
Revises: 0003_rbac
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_user_email"
down_revision: str | None = "0003_rbac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMAIL_SHAPE = (
    "email IS NULL OR (length(email) <= 254 AND email ~ '^[^@[:space:]]+@[^@[:space:]]+$')"
)


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.Text, nullable=True))
    op.create_check_constraint("email_shape", "users", _EMAIL_SHAPE)


def downgrade() -> None:
    op.drop_constraint("email_shape", "users", type_="check")
    op.drop_column("users", "email")
