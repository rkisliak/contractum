"""Alembic environment for contracts (sync psycopg URL; own version table)."""

from dc_contracts.models import ContractsBase
from dc_contracts.settings import ContractsSettings
from dc_core.migrations import run_env

run_env(
    metadata=ContractsBase.metadata,
    default_version_table="alembic_version_contracts",
    url_factory=lambda: ContractsSettings().sqlalchemy_url(),
)
