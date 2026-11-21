"""session cutoff: sessions_valid_after on users, entra_users and entra_registrations

D-75: reactivating a principal sets ``sessions_valid_after``; a JWT whose verified iat is at or
before it and a refresh token created at or before it are refused, so nothing issued before a
reactivation works again. Local users use it from this revision; Entra users and service-principal
registrations are written by a later plan, and one revision holds every cutoff column.

Revision ID: 0007_session_cutoff
Revises: 0006_machine_credentials
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_session_cutoff"
down_revision: str | None = "0006_machine_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("users", "entra_users", "entra_registrations")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("sessions_valid_after", sa.DateTime(timezone=True)))


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "sessions_valid_after")
