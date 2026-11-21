"""refresh_tokens.client_summary and access_jti: the session list (quick 261120-s0p)

``client_summary`` is a fixed-vocabulary summary of the sign-in User-Agent such as "Chrome on
macOS" (at most 64 characters; the raw User-Agent is never stored). ``access_jti`` is the jti of
the access token issued with the row, so the caller's own session can be recognised from the
token's jti without adding a claim. Both are nullable: rows from before this revision simply
show as an unknown device and never as the current session.

Revision ID: 0013_refresh_session_client
Revises: 0012_refresh_session_start
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_refresh_session_client"
down_revision: str | None = "0012_refresh_session_start"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("refresh_tokens", sa.Column("client_summary", sa.Text(), nullable=True))
    op.add_column("refresh_tokens", sa.Column("access_jti", sa.Text(), nullable=True))
    op.create_check_constraint(
        "client_summary_length",
        "refresh_tokens",
        "client_summary IS NULL OR char_length(client_summary) <= 64",
    )


def downgrade() -> None:
    op.drop_constraint("client_summary_length", "refresh_tokens", type_="check")
    op.drop_column("refresh_tokens", "access_jti")
    op.drop_column("refresh_tokens", "client_summary")
