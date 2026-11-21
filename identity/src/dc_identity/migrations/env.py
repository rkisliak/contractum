"""Alembic environment for identity (sync psycopg URL; own version table)."""

from dc_core.migrations import run_env
from dc_identity.models import IdentityBase, Ltree
from dc_identity.settings import IdentitySettings
from sqlalchemy.dialects.postgresql.base import ischema_names

# Let reflection recognise ltree so `alembic check` compares it instead of warning.
ischema_names["ltree"] = Ltree

run_env(
    metadata=IdentityBase.metadata,
    default_version_table="alembic_version_identity",
    url_factory=lambda: IdentitySettings().sqlalchemy_url(),
)
