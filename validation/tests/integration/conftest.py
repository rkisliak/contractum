"""Fixtures: the validation app on a fresh Postgres 18 database."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dc_auth.authorizer import FolderFacts
from dc_auth.issuers import AuthSettings, IssuerConfig
from dc_auth.testing import FakeIssuer
from dc_core.actor import Actor
from dc_core.db import make_async_engine, make_session_factory
from dc_core.migrations import run_alembic
from dc_core.uow import UnitOfWork
from dc_identity.main import create_identity_app
from dc_identity.migrate import SCRIPT_LOCATION as IDENTITY_SCRIPT_LOCATION
from dc_identity.migrate import VERSION_TABLE as IDENTITY_VERSION_TABLE
from dc_identity.settings import IdentitySettings
from dc_validation.main import create_validation_app
from dc_validation.migrate import SCRIPT_LOCATION, VERSION_TABLE
from dc_validation.models import OutboxEvent
from dc_validation.sandbox import Sandbox
from dc_validation.settings import ValidationSettings
from fastapi import FastAPI
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LOCAL_ISSUER = "http://identity:8000"
LOGIN = "/api/v1/identity/auth/login"
PASSWORD = "/api/v1/identity/auth/password"
FOLDERS = "/api/v1/identity/folders"
ADMIN_PASSWORD = "an admin password of 30 chars!"

AsgiClient = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]

PM = Actor(type="user", id="fake:policy-manager", display="Policy Manager", via="jwt")


@dataclass
class ValidationEnv:
    app: FastAPI
    settings: ValidationSettings
    db_url: str


@pytest.fixture
def migrate_validation(fresh_db_url: str) -> Callable[..., None]:
    def _migrate(command: str = "upgrade", revision: str = "head") -> None:
        run_alembic(
            script_location=SCRIPT_LOCATION,
            version_table=VERSION_TABLE,
            url=fresh_db_url,
            command=command,  # type: ignore[arg-type]
            revision=revision,
        )

    return _migrate


@pytest.fixture
def make_validation(
    fresh_db_url: str, migrate_validation: Callable[..., None]
) -> Callable[..., ValidationEnv]:
    def _make(*, migrate: bool = True) -> ValidationEnv:
        if migrate:
            migrate_validation()
        settings = ValidationSettings(database_url=fresh_db_url)  # type: ignore[arg-type]
        return ValidationEnv(
            app=create_validation_app(settings, AuthSettings()),
            settings=settings,
            db_url=fresh_db_url,
        )

    return _make


KINDS = ("root", "organization", "project", "team")


def drop_platform_seed(db_url: str) -> None:
    """Remove the platform rule set migration 0005 seeds, leaving the schema as it was at 0004.

    Most suites count rows and outbox events of the sets they create, so they start from a clean
    slate. ``test_platform_rule_set.py`` keeps the seed (``make_stack(platform_seed=True)``). The
    delete guard on published versions is lifted for this one statement batch only.
    """
    url = db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("ALTER TABLE rule_set_version DISABLE TRIGGER trg_rule_set_version_no_delete")
        conn.execute(
            "DELETE FROM rule_set_version WHERE rule_set_id IN"
            " (SELECT id FROM rule_set WHERE scope = 'platform')"
        )
        conn.execute("ALTER TABLE rule_set_version ENABLE TRIGGER trg_rule_set_version_no_delete")
        conn.execute("DELETE FROM rule_set WHERE scope = 'platform'")
        conn.execute(
            "DELETE FROM outbox WHERE action = 'ruleset.version_published'"
            " AND actor->>'id' = 'system:validation-seed'"
        )


@dataclass
class RuleSetEnv:
    """A migrated database, a session factory and a real sandbox (one worker) for the service."""

    session_factory: async_sessionmaker[AsyncSession]
    sandbox: Sandbox
    settings: ValidationSettings
    db_url: str
    ids: dict[str, uuid.UUID] = field(default_factory=lambda: {k: uuid.uuid4() for k in KINDS})
    actor: Actor = PM

    def facts(self, kind: str = "team", *, ids: dict[str, uuid.UUID] | None = None) -> FolderFacts:
        """Folder facts for one level of a ROOT -> Organization -> Project -> Team chain."""
        chain = ids or self.ids
        depth = KINDS.index(kind) + 1
        return FolderFacts(
            id=chain[kind],
            kind=kind,  # type: ignore[arg-type]
            slug=kind,
            path="/".join(KINDS[:depth]),
            ancestor_ids=tuple(chain[k] for k in KINDS[:depth]),
            organization_id=None if kind == "root" else chain["organization"],
            archived=False,
        )

    def team_facts(self) -> FolderFacts:
        return self.facts("team")

    def uow(self, actor: Actor | None = None) -> UnitOfWork:
        return UnitOfWork(
            self.session_factory,
            actor=actor or self.actor,
            outbox_model=OutboxEvent,
            service="validation",
        )

    def sql(self, query: str, *params: Any) -> list[dict[str, Any]]:
        url = self.db_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
            return conn.execute(query, params).fetchall()  # type: ignore[arg-type]

    def events(self, action: str | None = None) -> list[dict[str, Any]]:
        if action is None:
            return self.sql("SELECT * FROM outbox ORDER BY id")
        return self.sql("SELECT * FROM outbox WHERE action = %s ORDER BY id", action)


@pytest.fixture
async def rule_set_env(
    fresh_db_url: str, migrate_validation: Callable[..., None]
) -> AsyncIterator[RuleSetEnv]:
    migrate_validation()
    drop_platform_seed(fresh_db_url)
    settings = ValidationSettings(
        database_url=fresh_db_url,  # type: ignore[arg-type]
        eval_workers=1,
        max_concurrent_runs=4,
        # Concurrent publishes share the one worker; a short wait made them flaky with
        # 503 under load. The saturation test sets its own short wait.
        queue_wait_s=10.0,
    )
    engine = make_async_engine(fresh_db_url)
    sandbox = Sandbox(settings)
    try:
        yield RuleSetEnv(make_session_factory(engine), sandbox, settings, fresh_db_url)
    finally:
        await sandbox.aclose()
        await engine.dispose()


# -- identity + validation in one process (plan 05-07) --------------------------------------------


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
def second_db_url(pg_admin_url: str) -> Any:
    """A second per-test database (``fresh_db_url`` is the first, the validation one)."""
    name = f"t_{uuid.uuid4().hex}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_url(pg_admin_url).set(drivername="postgresql+psycopg", database=name)
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


Sql = Callable[..., list[dict[str, Any]]]


@pytest.fixture
def sql() -> Sql:
    """Run SQL against a test database with autocommit; returns rows as dicts."""

    def _run(db_url: str, query: str, *params: object) -> list[dict[str, Any]]:
        url = db_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
            cur = conn.execute(query, params)  # type: ignore[arg-type]
            return cur.fetchall() if cur.description else []

    return _run


@dataclass
class Chain:
    """An Organization -> Project -> Team chain built through identity."""

    org: str
    project: str
    team: str


@dataclass
class ValidationStack:
    """The identity app and the validation app, each on its own database, admin logged in."""

    identity_app: FastAPI
    identity: httpx.AsyncClient  # talks to identity
    app: FastAPI  # the validation app
    client: httpx.AsyncClient  # talks to validation
    validation_db_url: str
    identity_db_url: str
    admin_token: str
    root_id: str
    fake_issuer: FakeIssuer
    sql: Sql

    def token(self, sub: str) -> tuple[str, str]:
        """A token minted by the fake issuer for ``sub`` and the principal id it resolves to."""
        return self.fake_issuer.mint(sub), f"fake:{sub}"

    def grant_sql(
        self, principal_id: str, role: str, folder_id: str, principal_type: str = "user"
    ) -> str:
        """Insert a role assignment straight into identity's database; returns its id."""
        url = self.identity_db_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(url, autocommit=True) as conn:
            row = conn.execute(
                "INSERT INTO role_assignments (folder_id, principal_type, principal_id, role, "
                "created_by, updated_by) VALUES (%s, %s, %s, %s, 'system:test', 'system:test') "
                "RETURNING id",
                (folder_id, principal_type, principal_id, role),
            ).fetchone()
        assert row is not None
        return str(row[0])

    async def folder(self, parent: str, slug: str) -> str:
        response = await self.identity.post(
            FOLDERS,
            json={"parent_id": parent, "slug": slug, "display_name": slug.title()},
            headers=bearer(self.admin_token),
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    async def chain(self, prefix: str | None = None) -> Chain:
        tag = prefix or f"o{uuid.uuid4().hex[:10]}"
        org = await self.folder(self.root_id, tag)
        project = await self.folder(org, "proj")
        team = await self.folder(project, "team")
        return Chain(org=org, project=project, team=team)

    async def archive(self, folder_id: str) -> None:
        response = await self.identity.post(
            f"{FOLDERS}/{folder_id}/archive", headers=bearer(self.admin_token)
        )
        assert response.status_code == 200, response.text

    def _headers(
        self, token: str | None, if_match: str | None = None, content_type: str | None = None
    ) -> dict[str, str]:
        headers = bearer(token or self.admin_token)
        if if_match is not None:
            headers["If-Match"] = if_match
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    async def create_rule_set(
        self,
        folder_id: str,
        name: str = "Naming",
        *,
        token: str | None = None,
        description: str | None = "naming rules",
    ) -> httpx.Response:
        return await self.client.post(
            f"/api/v1/validation/folders/{folder_id}/rule-sets",
            json={"name": name, "description": description},
            headers=self._headers(token),
        )

    async def put_draft(
        self,
        rule_set_id: str,
        body: str | bytes,
        if_match: str | None,
        *,
        token: str | None = None,
        content_type: str = "application/yaml",
    ) -> httpx.Response:
        return await self.client.put(
            f"/api/v1/validation/rule-sets/{rule_set_id}/draft",
            content=body,
            headers=self._headers(token, if_match, content_type),
        )

    async def put_rule(
        self,
        rule_set_id: str,
        rule_key: str,
        body: str | bytes,
        if_match: str | None,
        *,
        token: str | None = None,
        content_type: str = "application/json",
    ) -> httpx.Response:
        return await self.client.put(
            f"/api/v1/validation/rule-sets/{rule_set_id}/draft/rules/{rule_key}",
            content=body,
            headers=self._headers(token, if_match, content_type),
        )

    async def delete_rule(
        self, rule_set_id: str, rule_key: str, if_match: str | None, *, token: str | None = None
    ) -> httpx.Response:
        return await self.client.delete(
            f"/api/v1/validation/rule-sets/{rule_set_id}/draft/rules/{rule_key}",
            headers=self._headers(token, if_match),
        )

    async def publish(
        self, rule_set_id: str, if_match: str | None, *, token: str | None = None
    ) -> httpx.Response:
        return await self.client.post(
            f"/api/v1/validation/rule-sets/{rule_set_id}/publish",
            headers=self._headers(token, if_match),
        )

    async def check(self, rule_set_id: str, *, token: str | None = None) -> httpx.Response:
        return await self.client.post(
            f"/api/v1/validation/rule-sets/{rule_set_id}/draft/check",
            headers=self._headers(token),
        )

    async def get(self, path: str, *, token: str | None = None) -> httpx.Response:
        return await self.client.get(path, headers=self._headers(token))

    def validation_sql(self, query: str, *params: object) -> list[dict[str, Any]]:
        return self.sql(self.validation_db_url, query, *params)

    def outbox_actions(self) -> list[str]:
        return [
            row["action"] for row in self.validation_sql("SELECT action FROM outbox ORDER BY id")
        ]


@pytest.fixture
def make_stack(
    tmp_path: Path,
    fresh_db_url: str,
    second_db_url: str,
    fake_issuer: FakeIssuer,
    asgi_client: AsgiClient,
    migrate_validation: Callable[..., None],
    sql: Sql,
) -> Callable[..., AbstractAsyncContextManager[ValidationStack]]:
    """``async with make_stack() as stack``: both apps running, the admin logged in.

    Validation reaches identity exactly as in compose (``RemoteAuthorizer`` plus a JWKS fetch for
    the local issuer), over an ``httpx.ASGITransport`` straight into the identity app. Keyword
    arguments override ``ValidationSettings`` (for example ``max_concurrent_runs=1``); the one
    exception is ``platform_seed``: by default the seeded platform rule set is removed after
    migrating (see ``drop_platform_seed``), ``platform_seed=True`` keeps it. The test
    module must carry ``pytest.mark.asyncio(loop_scope="session")``: the sandbox spawns
    subprocesses on the loop that starts the app.
    """

    @asynccontextmanager
    async def make_stack_context(**settings_overrides: Any) -> AsyncIterator[ValidationStack]:
        secret = secrets.token_urlsafe(30)[:40].ljust(40, "x")
        secret_file = tmp_path / "bootstrap_admin_secret"
        secret_file.write_text(secret + "\n", encoding="utf-8")
        key_file = tmp_path / "jwt_signing_key.pem"
        _write_rsa_key(key_file)
        run_alembic(
            script_location=IDENTITY_SCRIPT_LOCATION,
            version_table=IDENTITY_VERSION_TABLE,
            url=second_db_url,
            command="upgrade",
        )
        migrate_validation()
        if not settings_overrides.pop("platform_seed", False):
            drop_platform_seed(fresh_db_url)
        identity_settings = IdentitySettings(
            database_url=second_db_url,  # type: ignore[arg-type]
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
            link = httpx.AsyncClient(transport=httpx.ASGITransport(app=identity_app))
            settings_overrides.setdefault("eval_workers", 1)
            settings_overrides.setdefault("queue_wait_s", 0.5)
            validation_settings = ValidationSettings(
                database_url=fresh_db_url,  # type: ignore[arg-type]
                **settings_overrides,
            )
            app = create_validation_app(validation_settings, auth_settings, http_client=link)
            try:
                async with asgi_client(app) as client:
                    restricted = await identity.post(
                        LOGIN, json={"username": "admin", "password": secret}
                    )
                    assert restricted.status_code == 200, restricted.text
                    rotated = await identity.post(
                        PASSWORD,
                        json={"new_password": ADMIN_PASSWORD},
                        headers=bearer(restricted.json()["access_token"]),
                    )
                    assert rotated.status_code == 204, rotated.text
                    login = await identity.post(
                        LOGIN, json={"username": "admin", "password": ADMIN_PASSWORD}
                    )
                    assert login.status_code == 200, login.text
                    root = sql(second_db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
                    yield ValidationStack(
                        identity_app=identity_app,
                        identity=identity,
                        app=app,
                        client=client,
                        validation_db_url=fresh_db_url,
                        identity_db_url=second_db_url,
                        admin_token=login.json()["access_token"],
                        root_id=str(root),
                        fake_issuer=fake_issuer,
                        sql=sql,
                    )
            finally:
                await link.aclose()

    return make_stack_context
