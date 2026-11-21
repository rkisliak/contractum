"""Alembic helpers shared by every service (one ``alembic/`` dir and version table each)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import Any, Literal

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import MetaData

from dc_core.settings import CoreSettings

OUTBOX_UNPUBLISHED_INDEX = "ix_outbox_unpublished"

Command = Literal["upgrade", "downgrade", "check", "current"]


def audit_columns() -> list[sa.Column[Any]]:
    """The four audit columns (same shape as ``AuditColumnsMixin``)."""
    return [
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_by", sa.Text, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def outbox_table(op: Any, table_name: str = "outbox") -> None:
    """Create the outbox table (same columns as ``OutboxMixin``) and its partial index.

    ``op`` is ``alembic.op`` inside a migration, or an ``Operations`` instance.
    """
    op.create_table(
        table_name,
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.Text, nullable=False),
        sa.Column("aggregate_id", sa.Text, nullable=False),
        sa.Column("aggregate_version", sa.Integer, nullable=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column(
            "scope_path",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("actor", postgresql.JSONB, nullable=False),
        sa.Column("request_id", sa.Text, nullable=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("before", postgresql.JSONB, nullable=True),
        sa.Column("after", postgresql.JSONB, nullable=True),
        sa.Column("schema_version", sa.Integer, server_default=sa.text("1"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table_name}"),
        sa.UniqueConstraint("event_id", name=f"uq_{table_name}_event_id"),
    )
    op.create_index(
        OUTBOX_UNPUBLISHED_INDEX,
        table_name,
        ["id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def run_alembic(
    *,
    script_location: str,
    version_table: str,
    url: str,
    command: Command,
    revision: str = "head",
) -> None:
    """Run one Alembic command in code.

    ``script_location`` may be a filesystem path or a package resource such as
    ``dc_identity:migrations``. The service's ``env.py`` reads ``version_table`` from
    ``config.attributes`` and the database URL from ``sqlalchemy.url``.
    """
    config = Config()
    config.set_main_option("script_location", script_location)
    # configparser treats '%' as interpolation: escape it so URL-encoded passwords survive.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["version_table"] = version_table
    if command == "upgrade":
        _alembic.upgrade(config, revision)
    elif command == "downgrade":
        _alembic.downgrade(config, revision)
    elif command == "check":
        _alembic.check(config)
    elif command == "current":
        _alembic.current(config)
    else:  # pragma: no cover - guarded by the Literal type
        raise ValueError(f"unknown command {command!r}")


def run_env(
    *,
    metadata: MetaData,
    default_version_table: str,
    url_factory: Callable[[], str],
) -> None:
    """The body of a service's ``migrations/env.py``: offline SQL or an online upgrade.

    The version table comes from ``config.attributes["version_table"]`` (set by ``run_alembic``)
    or ``default_version_table``. The URL is ``sqlalchemy.url`` of the Alembic config (with the
    ``%%`` interpolation escape undone) or, when that is unset, ``url_factory()``: the service
    settings are only read then. Online runs use an injected ``connection`` attribute when there
    is one, else a short-lived ``NullPool`` engine.
    """
    from alembic import context  # only importable inside an Alembic run
    from sqlalchemy import create_engine, pool

    config = context.config
    version_table = str(config.attributes.get("version_table", default_version_table))

    def url() -> str:
        configured = config.get_main_option("sqlalchemy.url")
        if configured:
            return configured.replace("%%", "%")
        return url_factory()

    def run(connection: Any) -> None:
        context.configure(
            connection=connection,
            target_metadata=metadata,
            version_table=version_table,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    if context.is_offline_mode():
        context.configure(
            url=url(),
            target_metadata=metadata,
            literal_binds=True,
            version_table=version_table,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = config.attributes.get("connection")
    if connectable is not None:
        run(connectable)
        return
    engine = create_engine(url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()


_alembic = command


def migration_cli(
    *,
    script_location: str,
    version_table: str,
    settings_factory: Callable[[], CoreSettings],
    argv: Sequence[str] | None = None,
) -> None:
    """``python -m <service>.migrate upgrade|downgrade|check|current [revision]``."""
    parser = argparse.ArgumentParser(prog="migrate")
    parser.add_argument("command", choices=["upgrade", "downgrade", "check", "current"])
    parser.add_argument("revision", nargs="?", default=None)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command == "downgrade" and args.revision is None:
        parser.error("downgrade requires a revision")
    run_alembic(
        script_location=script_location,
        version_table=version_table,
        url=settings_factory().sqlalchemy_url(),
        command=args.command,
        revision=args.revision or "head",
    )
