"""Cross-service integration fixtures (Plan 12): identity, contracts and audit on real Postgres.

``completed_flow`` runs the whole product flow in one process (bootstrap login, password rotation,
a failed login, login, refresh, logout, a second login, an Organization -> Project -> Team chain
and a contract whose ``customProperties`` carry a secret-looking value) and yields with both apps
still running. Contracts reaches identity through an ``httpx.ASGITransport`` exactly as the
module conftests do, and no NATS broker is configured, so every outbox row stays in its
producer's database until a test replays it into a fresh audit database (``replay_to_audit``):
the same ``build_envelope -> AuditEnvelope -> ingest_event`` path the real consumer takes.
"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dc_audit.ingest import AuditEnvelope, ingest_event
from dc_audit.migrate import SCRIPT_LOCATION as AUDIT_SCRIPT_LOCATION
from dc_audit.migrate import VERSION_TABLE as AUDIT_VERSION_TABLE
from dc_auth.issuers import AuthSettings, IssuerConfig
from dc_auth.testing import FakeIssuer
from dc_contracts.main import create_contracts_app
from dc_contracts.migrate import SCRIPT_LOCATION as CONTRACTS_SCRIPT_LOCATION
from dc_contracts.migrate import VERSION_TABLE as CONTRACTS_VERSION_TABLE
from dc_contracts.settings import ContractsSettings
from dc_core.db import make_async_engine, make_session_factory
from dc_core.events import build_envelope
from dc_core.logging import configure_logging
from dc_core.migrations import run_alembic
from dc_identity.main import create_identity_app
from dc_identity.migrate import SCRIPT_LOCATION as IDENTITY_SCRIPT_LOCATION
from dc_identity.migrate import VERSION_TABLE as IDENTITY_VERSION_TABLE
from dc_identity.settings import IdentitySettings
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

LOCAL_ISSUER = "http://identity:8000"
LOGIN = "/api/v1/identity/auth/login"
REFRESH = "/api/v1/identity/auth/refresh"
LOGOUT = "/api/v1/identity/auth/logout"
PASSWORD = "/api/v1/identity/auth/password"
FOLDERS = "/api/v1/identity/folders"
NEW_PASSWORD = "another admin password 31 chars"  # noqa: S105
WRONG_PASSWORD = "definitely-the-wrong-password-Zq9x"  # noqa: S105
CONTRACT_SECRET = "sk-test-SECRET-123"  # noqa: S105

Sql = Callable[..., list[dict[str, Any]]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _plain_url(db_url: str) -> str:
    return db_url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture
def sql() -> Sql:
    """Run SQL against a test database with autocommit; returns rows as dicts."""

    def _run(db_url: str, query: str, *params: object) -> list[dict[str, Any]]:
        with psycopg.connect(_plain_url(db_url), row_factory=dict_row, autocommit=True) as conn:
            cur = conn.execute(query, params)  # type: ignore[arg-type]
            return cur.fetchall() if cur.description else []

    return _run


@dataclass
class Databases:
    identity: str
    contracts: str
    audit: str


@pytest.fixture
def migrated_databases(pg_admin_url: str) -> Iterator[Databases]:
    """Three empty databases (identity, contracts, audit), each upgraded to head."""
    names = {svc: f"t_{svc}_{uuid.uuid4().hex[:12]}" for svc in ("identity", "contracts", "audit")}
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        for name in names.values():
            admin.execute(f'CREATE DATABASE "{name}"')
    urls = {
        svc: make_url(pg_admin_url)
        .set(drivername="postgresql+psycopg", database=name)
        .render_as_string(hide_password=False)
        for svc, name in names.items()
    }
    try:
        for location, table, svc in (
            (IDENTITY_SCRIPT_LOCATION, IDENTITY_VERSION_TABLE, "identity"),
            (CONTRACTS_SCRIPT_LOCATION, CONTRACTS_VERSION_TABLE, "contracts"),
            (AUDIT_SCRIPT_LOCATION, AUDIT_VERSION_TABLE, "audit"),
        ):
            run_alembic(
                script_location=location, version_table=table, url=urls[svc], command="upgrade"
            )
        yield Databases(**urls)
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            for name in names.values():
                admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


class LogCapture(io.StringIO):
    """dc_core's structlog JSON output, captured line by line (``lines``)."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def write(self, s: str) -> int:
        self.lines.extend(line for line in s.splitlines() if line)
        return super().write(s)

    def install(self) -> None:
        """Point dc_core logging here at DEBUG, so nothing is filtered out.

        The apps call ``configure_logging`` in their lifespans: install after they started.
        """
        configure_logging("integration", "DEBUG", stream=self)


@pytest.fixture
def captured_logs() -> Iterator[LogCapture]:
    capture = LogCapture()
    try:
        yield capture
    finally:
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()


class IdentityLink(httpx.AsyncBaseTransport):
    """Contracts' route to identity: straight into the app, no network."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._inner.handle_async_request(request)


@dataclass
class Flow:
    """What the flow produced: ids, the secrets it handled and the live clients."""

    databases: Databases
    identity: httpx.AsyncClient
    contracts: httpx.AsyncClient
    admin_token: str
    admin_id: str
    root_id: str
    org: str
    project: str
    team: str
    contract_id: str
    bootstrap_secret: str
    new_password: str
    wrong_password: str
    signing_key_file: Path
    refresh_tokens: list[str] = field(default_factory=list)
    access_tokens: list[str] = field(default_factory=list)
    contract_secret: str = CONTRACT_SECRET
    wrong_login_status: int = 0
    wrong_login_body: str = ""
    rejected_token_status: int = 0
    rejected_token_body: str = ""
    logout_status: int = 0
    pat_tokens: list[str] = field(default_factory=list)
    pat_identity_status: int = 0
    pat_contracts_status: int = 0
    pat_revoke_status: int = 0
    revoked_pat_status: int = 0
    revoked_pat_body: str = ""

    def secrets_to_hide(self) -> dict[str, str]:
        """Label -> value of every exact secret the flow handled (the scan list)."""
        found = {
            "bootstrap secret": self.bootstrap_secret,
            "new password": self.new_password,
            "wrong password": self.wrong_password,
            "contract customProperties secret": CONTRACT_SECRET,
        }
        for index, token in enumerate(self.refresh_tokens):
            found[f"raw refresh token #{index}"] = token
        for index, token in enumerate(self.access_tokens):
            found[f"access token #{index}"] = token
        for index, token in enumerate(self.pat_tokens):
            secret = token.split("_", 2)[2]
            found[f"raw PAT #{index}"] = token
            found[f"PAT secret #{index}"] = secret
            found[f"PAT secret sha256 #{index}"] = hashlib.sha256(secret.encode()).hexdigest()
        return found


def contract_yaml(odcs_id: str) -> bytes:
    return f"""kind: DataContract
apiVersion: v3.2.0
id: {odcs_id}
name: Flow Orders
version: "1.0.0"
status: draft
schema:
  - name: orders
    physicalType: table
    properties:
      - name: order_id
        logicalType: string
customProperties:
  - property: apiKey
    value: {CONTRACT_SECRET}
""".encode()


def _write_rsa_key(path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


@pytest.fixture
async def completed_flow(
    tmp_path: Path,
    migrated_databases: Databases,
    fake_issuer: FakeIssuer,
    asgi_client: Callable[..., Any],
    captured_logs: LogCapture,
    sql: Sql,
) -> AsyncIterator[Flow]:
    secret = secrets.token_urlsafe(30)[:40].ljust(40, "x")
    secret_file = tmp_path / "bootstrap_admin_secret"
    secret_file.write_text(secret + "\n", encoding="utf-8")
    key_file = tmp_path / "jwt_signing_key.pem"
    _write_rsa_key(key_file)
    db = migrated_databases
    identity_settings = IdentitySettings(
        database_url=db.identity,  # type: ignore[arg-type]
        bootstrap_secret_file=secret_file,
        jwt_signing_key_file=key_file,
        local_issuer=LOCAL_ISSUER,
    )
    auth_settings = AuthSettings(
        issuers=[
            IssuerConfig(
                issuer=LOCAL_ISSUER,
                jwks_uri=f"{LOCAL_ISSUER}/.well-known/jwks.json",
                audiences=("contractum",),
                namespace="local",
            ),
            fake_issuer.issuer_config(),
        ]
    )
    identity_app = create_identity_app(identity_settings, auth_settings)
    async with asgi_client(identity_app) as identity:
        contracts_http = httpx.AsyncClient(
            transport=IdentityLink(httpx.ASGITransport(app=identity_app))
        )
        contracts_app = create_contracts_app(
            ContractsSettings(database_url=db.contracts),  # type: ignore[arg-type]
            auth_settings,
            http_client=contracts_http,
        )
        try:
            async with asgi_client(contracts_app) as contracts:
                # Both lifespans have configured logging by now: take it over.
                captured_logs.install()
                yield await _drive_flow(identity, contracts, db, secret, key_file, sql)
        finally:
            await contracts_http.aclose()


async def _drive_flow(
    identity: httpx.AsyncClient,
    contracts: httpx.AsyncClient,
    db: Databases,
    secret: str,
    key_file: Path,
    sql: Sql,
) -> Flow:
    access: list[str] = []
    refresh: list[str] = []

    restricted = await identity.post(LOGIN, json={"username": "admin", "password": secret})
    assert restricted.status_code == 200, restricted.text
    access.append(restricted.json()["access_token"])
    rotated = await identity.post(
        PASSWORD,
        json={"new_password": NEW_PASSWORD},
        headers=bearer(restricted.json()["access_token"]),
    )
    assert rotated.status_code == 204, rotated.text

    wrong = await identity.post(LOGIN, json={"username": "admin", "password": WRONG_PASSWORD})

    login = await identity.post(LOGIN, json={"username": "admin", "password": NEW_PASSWORD})
    assert login.status_code == 200, login.text
    access.append(login.json()["access_token"])
    refresh.append(login.json()["refresh_token"])

    refreshed = await identity.post(REFRESH, json={"refresh_token": refresh[0]})
    assert refreshed.status_code == 200, refreshed.text
    access.append(refreshed.json()["access_token"])
    refresh.append(refreshed.json()["refresh_token"])

    logout = await identity.post(LOGOUT, json={"refresh_token": refresh[1]})

    second = await identity.post(LOGIN, json={"username": "admin", "password": NEW_PASSWORD})
    assert second.status_code == 200, second.text
    token = str(second.json()["access_token"])
    access.append(token)
    refresh.append(second.json()["refresh_token"])

    root_id = str(sql(db.identity, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
    admin_id = str(sql(db.identity, "SELECT id FROM users WHERE username = 'admin'")[0]["id"])

    async def folder(parent: str, slug: str) -> str:
        response = await identity.post(
            FOLDERS,
            json={"parent_id": parent, "slug": slug, "display_name": slug.title()},
            headers=bearer(token),
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    org = await folder(root_id, f"flow-{uuid.uuid4().hex[:8]}")
    project = await folder(org, "payments")
    team = await folder(project, "ingest")
    created = await contracts.post(
        f"/api/v1/contracts/folders/{team}/contracts",
        content=contract_yaml(f"urn:datacontract:flow:{uuid.uuid4()}"),
        headers={**bearer(token), "Content-Type": "application/yaml"},
    )
    assert created.status_code == 201, created.text
    contract_id = str(created.json()["contract"]["id"])
    fetched = await contracts.get(
        f"/api/v1/contracts/contracts/{contract_id}/document", headers=bearer(token)
    )
    assert fetched.status_code == 200, fetched.text

    # A PAT is created, used on identity and contracts, revoked and presented once more: every
    # step of its life must leave no credential material in any store (AUTH-06, D-68).
    made = await identity.post(
        "/api/v1/identity/pats",
        json={
            "name": "flow-pat",
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
        headers=bearer(token),
    )
    assert made.status_code == 201, made.text
    pat = str(made.json()["token"])
    pat_on_identity = await identity.get(FOLDERS, params={"limit": 1}, headers=bearer(pat))
    pat_on_contracts = await contracts.get(
        f"/api/v1/contracts/contracts/{contract_id}/document", headers=bearer(pat)
    )
    pat_revoked = await identity.delete(
        f"/api/v1/identity/pats/{made.json()['id']}", headers=bearer(token)
    )
    revoked_pat = await identity.get(FOLDERS, params={"limit": 1}, headers=bearer(pat))

    # A token with a broken signature: the rejection path is where logging touches credentials.
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    access.append(tampered)
    rejected = await identity.get(FOLDERS, params={"limit": 1}, headers=bearer(tampered))

    return Flow(
        databases=db,
        identity=identity,
        contracts=contracts,
        admin_token=token,
        admin_id=admin_id,
        root_id=root_id,
        org=org,
        project=project,
        team=team,
        contract_id=contract_id,
        bootstrap_secret=secret,
        new_password=NEW_PASSWORD,
        wrong_password=WRONG_PASSWORD,
        signing_key_file=key_file,
        refresh_tokens=refresh,
        access_tokens=access,
        wrong_login_status=wrong.status_code,
        wrong_login_body=wrong.text,
        rejected_token_status=rejected.status_code,
        rejected_token_body=rejected.text,
        logout_status=logout.status_code,
        pat_tokens=[pat],
        pat_identity_status=pat_on_identity.status_code,
        pat_contracts_status=pat_on_contracts.status_code,
        pat_revoke_status=pat_revoked.status_code,
        revoked_pat_status=revoked_pat.status_code,
        revoked_pat_body=revoked_pat.text,
    )


def _outbox_rows(sql: Sql, flow: Flow) -> dict[str, list[dict[str, Any]]]:
    """Every outbox row of identity and contracts, in id order."""
    return {
        "identity": sql(flow.databases.identity, "SELECT * FROM outbox ORDER BY id"),
        "contracts": sql(flow.databases.contracts, "SELECT * FROM outbox ORDER BY id"),
    }


@pytest.fixture
def replay_to_audit(sql: Sql) -> Callable[[Flow], Any]:
    """Feed every producer outbox row through the consumer's ingest path into the audit database."""

    async def _replay(flow: Flow) -> int:
        engine = make_async_engine(flow.databases.audit)
        factory = make_session_factory(engine)
        stored = 0
        try:
            for service, rows in _outbox_rows(sql, flow).items():
                for row in rows:
                    envelope = AuditEnvelope.model_validate(
                        build_envelope(SimpleNamespace(**row), service=service)
                    )
                    async with factory() as session:
                        if await ingest_event(session, envelope):
                            stored += 1
                        await session.commit()
        finally:
            await engine.dispose()
        return stored

    return _replay


@pytest.fixture
def outbox_rows(sql: Sql) -> Callable[[Flow], dict[str, list[dict[str, Any]]]]:
    """``outbox_rows(flow)``: every outbox row of identity and contracts, in id order."""
    return lambda flow: _outbox_rows(sql, flow)


@pytest.fixture
def dump() -> Callable[[Any], str]:
    """Stable JSON text of rows for substring scans (UUIDs and datetimes via ``str``)."""
    return lambda value: json.dumps(value, default=str, sort_keys=True)
