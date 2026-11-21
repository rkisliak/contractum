"""Alembic environment for audit (sync psycopg URL; own version table)."""

from dc_audit.models import AuditBase
from dc_audit.settings import AuditSettings
from dc_core.migrations import run_env

run_env(
    metadata=AuditBase.metadata,
    default_version_table="alembic_version_audit",
    url_factory=lambda: AuditSettings().sqlalchemy_url(),
)
