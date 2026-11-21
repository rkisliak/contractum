"""PLAT-05 / T-01-54: one Postgres instance, but never a shared database or role.

A dedicated Postgres 18 container is initialised with the repository's real
``infra/postgres/init/10-service-databases.sh`` (placed in ``/docker-entrypoint-initdb.d``) and
generated ``<svc>_db_password`` files (placed in ``/run/secrets``); the superuser password comes
from the test, not from any repository file. Migrations run as each service's own role.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from dc_audit.migrate import SCRIPT_LOCATION as AUDIT_LOCATION
from dc_audit.migrate import VERSION_TABLE as AUDIT_TABLE
from dc_audit.models import AuditBase
from dc_contracts.migrate import SCRIPT_LOCATION as CONTRACTS_LOCATION
from dc_contracts.migrate import VERSION_TABLE as CONTRACTS_TABLE
from dc_contracts.models import ContractsBase
from dc_core.migrations import run_alembic
from dc_identity.migrate import SCRIPT_LOCATION as IDENTITY_LOCATION
from dc_identity.migrate import VERSION_TABLE as IDENTITY_TABLE
from dc_identity.models import IdentityBase
from dc_validation.migrate import SCRIPT_LOCATION as VALIDATION_LOCATION
from dc_validation.migrate import VERSION_TABLE as VALIDATION_TABLE
from dc_validation.models import ValidationBase
from psycopg import errors as pg_errors
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.integration

INIT_SCRIPT = (
    Path(__file__).resolve().parents[2] / "infra" / "postgres" / "init" / "10-service-databases.sh"
)


@dataclass(frozen=True)
class Service:
    name: str
    location: str
    version_table: str
    tables: frozenset[str]  # tables the models declare


SERVICES = (
    Service("identity", IDENTITY_LOCATION, IDENTITY_TABLE, frozenset(IdentityBase.metadata.tables)),
    Service(
        "contracts", CONTRACTS_LOCATION, CONTRACTS_TABLE, frozenset(ContractsBase.metadata.tables)
    ),
    Service(
        "validation",
        VALIDATION_LOCATION,
        VALIDATION_TABLE,
        frozenset(ValidationBase.metadata.tables),
    ),
    Service("audit", AUDIT_LOCATION, AUDIT_TABLE, frozenset(AuditBase.metadata.tables)),
)
NAMES = tuple(service.name for service in SERVICES)


@dataclass
class Cluster:
    host: str
    port: int
    superuser_password: str
    passwords: dict[str, str]

    def dsn(self, role: str, database: str) -> str:
        password = self.superuser_password if role == "postgres" else self.passwords[role]
        return f"postgresql://{role}:{password}@{self.host}:{self.port}/{database}"

    def url(self, role: str, database: str) -> str:
        return self.dsn(role, database).replace("postgresql://", "postgresql+psycopg://", 1)

    def query(self, role: str, database: str, sql: str) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.dsn(role, database), autocommit=True) as conn:
            cur = conn.execute(sql)
            return cur.fetchall() if cur.description else []


@pytest.fixture
def cluster() -> Iterator[Cluster]:
    """A fresh Postgres 18 initialised by the real init script (a new one for every test).

    The script and the password files are copied into the container (not bind-mounted), so the
    test also works where the temp directory is not a shared Docker path (Docker Desktop).
    """
    passwords = {name: secrets.token_urlsafe(24) for name in NAMES}
    superuser_password = secrets.token_urlsafe(24)
    container = PostgresContainer(
        "postgres:18",
        username="postgres",
        password=superuser_password,
        dbname="postgres",
        driver=None,
    )
    # /docker-entrypoint-initdb.d is where the image looks for init scripts; mode 0755 runs it.
    container.with_copy_into_container(
        INIT_SCRIPT, f"/docker-entrypoint-initdb.d/{INIT_SCRIPT.name}", 0o755
    )
    for name, password in passwords.items():
        container.with_copy_into_container(
            password.encode(), f"/run/secrets/{name}_db_password", 0o644
        )  # readable by the container's postgres user
    with container:
        yield Cluster(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            superuser_password=superuser_password,
            passwords=passwords,
        )


def _upgrade(cluster: Cluster, service: Service, database: str | None = None) -> None:
    run_alembic(
        script_location=service.location,
        version_table=service.version_table,
        url=cluster.url(service.name, database or service.name),
        command="upgrade",
    )


def _public_tables(cluster: Cluster, service: Service, database: str | None = None) -> set[str]:
    rows = cluster.query(
        service.name,
        database or service.name,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
    )
    return {row[0] for row in rows}


def _shape(cluster: Cluster, service: Service, database: str) -> list[tuple[Any, ...]]:
    return cluster.query(
        service.name,
        database,
        "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY table_name, column_name",
    )


def test_adjacency_each_role_reaches_only_its_own_database(cluster: Cluster) -> None:
    for role in NAMES:
        assert cluster.query(role, role, "SELECT current_user, current_database()") == [
            (role, role)
        ]
    denied: list[tuple[str, str]] = []
    for role in NAMES:
        for database in NAMES:
            if role == database:
                continue
            with pytest.raises(psycopg.OperationalError) as caught:
                cluster.query(role, database, "SELECT 1")
            assert "permission denied" in str(caught.value).lower(), (role, database)
            denied.append((role, database))
    assert len(denied) == 12
    # ltree is created by the init script (as superuser) in the identity database only.
    extensions = cluster.query("identity", "identity", "SELECT extname FROM pg_extension")
    assert ("ltree",) in extensions


def test_adjacency_a_role_cannot_use_another_roles_objects_after_migration(
    cluster: Cluster,
) -> None:
    """Even with both databases populated, a role has no path into the other database."""
    for service in SERVICES:
        _upgrade(cluster, service)
    with pytest.raises(psycopg.OperationalError):
        cluster.query("contracts", "identity", "SELECT count(*) FROM users")
    with pytest.raises(pg_errors.Error):
        cluster.query("identity", "identity", "SELECT count(*) FROM audit_event")


def test_empty_database_migrates_to_head_without_drift_and_a_second_upgrade_is_a_noop(
    cluster: Cluster,
) -> None:
    for service in SERVICES:
        url = cluster.url(service.name, service.name)
        assert _public_tables(cluster, service) == set()  # empty before the first upgrade

        _upgrade(cluster, service)
        tables = _public_tables(cluster, service)
        assert service.tables <= tables
        assert tables - service.tables == {service.version_table}
        version = cluster.query(
            service.name,
            service.name,
            f"SELECT version_num FROM {service.version_table}",  # noqa: S608
        )
        assert len(version) == 1

        run_alembic(
            script_location=service.location,
            version_table=service.version_table,
            url=url,
            command="check",
        )  # raises when the models and the migrated schema differ

        before = _shape(cluster, service, service.name)
        _upgrade(cluster, service)
        assert _public_tables(cluster, service) == tables
        assert _shape(cluster, service, service.name) == before
        assert (
            cluster.query(
                service.name,
                service.name,
                f"SELECT version_num FROM {service.version_table}",  # noqa: S608
            )
            == version
        )


def test_ordering_each_service_keeps_its_own_version_table_so_apply_order_is_irrelevant(
    cluster: Cluster,
) -> None:
    # Forward order on side databases (created like the init script does), reverse order on the
    # real ones: the resulting schemas must be identical.
    for service in SERVICES:
        cluster.query(
            "postgres", "postgres", f'CREATE DATABASE "{service.name}_fwd" OWNER "{service.name}"'
        )
        cluster.query(
            "postgres", "postgres", f'REVOKE ALL ON DATABASE "{service.name}_fwd" FROM PUBLIC'
        )
    cluster.query("postgres", "identity_fwd", "CREATE EXTENSION IF NOT EXISTS ltree")

    for service in SERVICES:
        _upgrade(cluster, service, f"{service.name}_fwd")
    for service in reversed(SERVICES):
        _upgrade(cluster, service)

    for service in SERVICES:
        assert _shape(cluster, service, service.name) == _shape(
            cluster, service, f"{service.name}_fwd"
        ), service.name
        # Only this service's version table lives in its database.
        version_tables = {
            table
            for table in _public_tables(cluster, service)
            if table.startswith("alembic_version")
        }
        assert version_tables == {service.version_table}
    assert len({service.version_table for service in SERVICES}) == len(SERVICES)
    assert {service.version_table for service in SERVICES} == {
        "alembic_version_identity",
        "alembic_version_contracts",
        "alembic_version_validation",
        "alembic_version_audit",
    }
