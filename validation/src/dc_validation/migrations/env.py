"""Alembic environment for validation (sync psycopg URL; own version table)."""

from dc_core.migrations import run_env
from dc_validation.models import ValidationBase
from dc_validation.settings import ValidationSettings

run_env(
    metadata=ValidationBase.metadata,
    default_version_table="alembic_version_validation",
    url_factory=lambda: ValidationSettings().sqlalchemy_url(),
)
