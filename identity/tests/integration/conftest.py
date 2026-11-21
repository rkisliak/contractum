"""Fixtures that build the identity app on a fresh Postgres 18 database."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dc_auth.issuers import AuthSettings, IssuerConfig
from dc_auth.testing import FakeIssuer
from dc_core.migrations import run_alembic
from dc_identity.directory.fake import FakeDirectoryClient
from dc_identity.main import create_identity_app
from dc_identity.migrate import SCRIPT_LOCATION, VERSION_TABLE
from dc_identity.settings import IdentitySettings
from fastapi import FastAPI
from psycopg.rows import dict_row

LOCAL_ISSUER = "http://identity:8000"


@dataclass
class IdentityEnv:
    app: FastAPI
    settings: IdentitySettings
    auth_settings: AuthSettings
    secret: str | None
    db_url: str
    directory: FakeDirectoryClient
    folder_usage: Any = None

    def rebuild(self) -> FastAPI:
        """A second app instance on the same database and files (a process restart)."""
        return create_identity_app(
            self.settings, self.auth_settings, self.directory, self.folder_usage
        )


def write_rsa_key(path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


@pytest.fixture
def secret_value() -> str:
    return secrets.token_urlsafe(30)[:40].ljust(40, "x")


@pytest.fixture
def make_identity(
    tmp_path: Path, fresh_db_url: str, fake_issuer: FakeIssuer, secret_value: str
) -> Callable[..., IdentityEnv]:
    def _make(
        *,
        secret: str | None = secret_value,
        directory: FakeDirectoryClient | None = None,
        folder_usage: Any = None,
        extra_issuers: Sequence[IssuerConfig] = (),
        **overrides: object,
    ) -> IdentityEnv:
        directory = directory if directory is not None else FakeDirectoryClient()
        # No directory cache unless a test opts in with make_identity(directory_cache_ttl_s=1):
        # immediate 401/503 assertions stay immediate (D-61 keeps 60 in production).
        overrides.setdefault("directory_cache_ttl_s", 0)
        secret_file = tmp_path / "bootstrap_admin_secret"
        if secret is not None:
            secret_file.write_text(secret + "\n", encoding="utf-8")
        key_file = tmp_path / "jwt_signing_key.pem"
        write_rsa_key(key_file)
        run_alembic(
            script_location=SCRIPT_LOCATION,
            version_table=VERSION_TABLE,
            url=fresh_db_url,
            command="upgrade",
        )
        settings = IdentitySettings(
            database_url=fresh_db_url,  # type: ignore[arg-type]
            bootstrap_secret_file=secret_file,
            jwt_signing_key_file=key_file,
            local_issuer=LOCAL_ISSUER,
            **overrides,  # type: ignore[arg-type]
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
                fake_issuer.entra_issuer_config(),
                *extra_issuers,
            ]
        )
        return IdentityEnv(
            app=create_identity_app(settings, auth_settings, directory, folder_usage),
            settings=settings,
            auth_settings=auth_settings,
            secret=secret,
            db_url=fresh_db_url,
            directory=directory,
            folder_usage=folder_usage,
        )

    return _make


LOGIN = "/api/v1/identity/auth/login"
PASSWORD = "/api/v1/identity/auth/password"
ADMIN_PASSWORD = "an admin password of 30 chars!"  # noqa: S105


@pytest.fixture
def sql() -> Callable[..., list[dict[str, Any]]]:
    """Run SQL against a test database with autocommit; returns rows as dicts."""

    def _run(db_url: str, query: str, *params: object) -> list[dict[str, Any]]:
        url = db_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
            cur = conn.execute(query, params)  # type: ignore[arg-type]
            return cur.fetchall() if cur.description else []

    return _run


@pytest.fixture
def bootstrap_token() -> Callable[[Any, IdentityEnv], Awaitable[str]]:
    """Log in with the one-time secret: returns the restricted password:rotate token."""

    async def _login(client: Any, env: IdentityEnv) -> str:
        resp = await client.post(
            LOGIN,
            json={"username": env.settings.bootstrap_admin_username, "password": env.secret},
        )
        assert resp.status_code == 200, resp.text
        token: str = resp.json()["access_token"]
        return token

    return _login


@pytest.fixture
def admin_token(
    bootstrap_token: Callable[[Any, IdentityEnv], Awaitable[str]],
) -> Callable[[Any, IdentityEnv], Awaitable[str]]:
    """Walk the whole bootstrap flow and return the Global admin's full access token."""

    async def _flow(client: Any, env: IdentityEnv) -> str:
        restricted = await bootstrap_token(client, env)
        rotated = await client.post(
            PASSWORD,
            json={"new_password": ADMIN_PASSWORD},
            headers={"Authorization": f"Bearer {restricted}"},
        )
        assert rotated.status_code == 204, rotated.text
        resp = await client.post(
            LOGIN,
            json={"username": env.settings.bootstrap_admin_username, "password": ADMIN_PASSWORD},
        )
        assert resp.status_code == 200, resp.text
        token: str = resp.json()["access_token"]
        return token

    return _flow


@pytest.fixture
async def session_env(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    bootstrap_token: Callable[[Any, Any], Awaitable[str]],
) -> AsyncIterator[tuple[Any, Any]]:
    """Identity with the bootstrap rotation done and no session started yet (no refresh tokens)."""
    env = make_identity()
    async with asgi_client(env.app) as client:
        restricted = await bootstrap_token(client, env)
        rotated = await client.post(
            PASSWORD,
            json={"new_password": ADMIN_PASSWORD},
            headers={"Authorization": f"Bearer {restricted}"},
        )
        assert rotated.status_code == 204, rotated.text
        yield env, client


async def _folder_chain(
    client: Any, token: str, root_id: object, prefix: str
) -> tuple[str, str, str]:
    """Create ``<prefix>`` (Organization) -> ``<prefix>-p`` (Project) -> ``<prefix>-t1`` (Team).

    Returns the three folder ids as strings; ``root_id`` is the parent of the organization.
    """
    ids: list[str] = []
    parent = str(root_id)
    for slug in (prefix, f"{prefix}-p", f"{prefix}-t1"):
        resp = await client.post(
            "/api/v1/identity/folders",
            json={"parent_id": parent, "slug": slug, "display_name": slug},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201, resp.text
        parent = resp.json()["id"]
        ids.append(parent)
    return ids[0], ids[1], ids[2]


def _grant_sql(
    db_url: str,
    principal_id: str,
    role: str,
    folder_id: object,
    principal_type: str = "user",
) -> None:
    """Insert one role assignment directly (the grant routes arrive in a later plan)."""
    url = db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO role_assignments (folder_id, principal_type, principal_id, role, "
            "created_by, updated_by) VALUES (%s, %s, %s, %s, 'test', 'test')",
            (folder_id, principal_type, principal_id, role),
        )


@pytest.fixture
def folder_chain() -> Callable[..., Awaitable[tuple[str, str, str]]]:
    """``await folder_chain(client, token, root_id, prefix)`` -> (org, project, team) ids."""
    return _folder_chain


@pytest.fixture
def grant_sql() -> Callable[..., None]:
    """``grant_sql(db_url, principal_id, role, folder_id, principal_type="user")``."""
    return _grant_sql


@dataclass
class CreatedUser:
    id: str
    principal_id: str
    username: str
    password: str


async def _create_user(
    client: Any, admin_token: str, username: str, *, display_name: str | None = None
) -> CreatedUser:
    """Create a local user as a Global admin with a random 24-character password."""
    password = secrets.token_urlsafe(18)
    resp = await client.post(
        "/api/v1/identity/users",
        json={
            "username": username,
            "password": password,
            "display_name": display_name or username,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return CreatedUser(
        id=body["id"],
        principal_id=body["principal_id"],
        username=body["username"],
        password=password,
    )


async def _login(client: Any, username: str, password: str) -> str:
    """Log in with a username and password; returns the access token."""
    resp = await client.post(LOGIN, json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["access_token"]
    return token


@pytest.fixture
def create_user() -> Callable[..., Awaitable[CreatedUser]]:
    """``await create_user(client, admin_token, "alice", display_name=None)`` -> CreatedUser."""
    return _create_user


@pytest.fixture
def login() -> Callable[..., Awaitable[str]]:
    """``await login(client, username, password)`` -> access token."""
    return _login


async def _grant(
    client: Any,
    token: str,
    folder_id: object,
    principal_id: str,
    role: str,
    principal_type: str = "user",
) -> Any:
    """POST a grant as ``token``; returns the raw response so tests can assert any status."""
    return await client.post(
        f"/api/v1/identity/folders/{folder_id}/grants",
        json={"principal_type": principal_type, "principal_id": principal_id, "role": role},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.fixture
def grant() -> Callable[..., Awaitable[Any]]:
    """``await grant(client, token, folder_id, principal_id, role, principal_type="user")``."""
    return _grant


async def _create_group(client: Any, token: str, owner_folder_id: object, name: str) -> str:
    """Create a local group as ``token`` and return its id (a bare lowercase uuid string)."""
    resp = await client.post(
        "/api/v1/identity/groups",
        json={"owner_folder_id": str(owner_folder_id), "name": name},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    group_id: str = resp.json()["id"]
    return group_id


async def _add_member(client: Any, token: str, group_id: object, username: str) -> Any:
    """POST a member by username as ``token``; returns the raw response."""
    return await client.post(
        f"/api/v1/identity/groups/{group_id}/members",
        json={"username": username},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.fixture
def create_group() -> Callable[..., Awaitable[str]]:
    """``await create_group(client, token, owner_folder_id, name)`` -> group id."""
    return _create_group


@pytest.fixture
def add_member() -> Callable[..., Awaitable[Any]]:
    """``await add_member(client, token, group_id, username)`` -> raw response."""
    return _add_member


async def _create_pat(
    client: Any, token: str, *, name: str = "p", days: float = 30
) -> dict[str, Any]:
    """Create a PAT as ``token``; returns the 201 body (with the one-time ``token``)."""
    resp = await client.post(
        "/api/v1/identity/pats",
        json={"name": name, "expires_at": (datetime.now(UTC) + timedelta(days=days)).isoformat()},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


@pytest.fixture
def create_pat() -> Callable[..., Awaitable[dict[str, Any]]]:
    """``await create_pat(client, token, name="p", days=30)`` -> the 201 body incl. ``token``."""
    return _create_pat


async def _create_service_account(
    client: Any, token: str, owner_folder_id: object, name: str
) -> dict[str, Any]:
    """Create a service account as ``token``; returns the 201 body."""
    resp = await client.post(
        "/api/v1/identity/service-accounts",
        json={"owner_folder_id": str(owner_folder_id), "name": name},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


@pytest.fixture
def create_service_account() -> Callable[..., Awaitable[dict[str, Any]]]:
    """``await create_service_account(client, token, owner_folder_id, name)`` -> the 201 body."""
    return _create_service_account
