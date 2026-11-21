"""Fixtures: identity and contracts apps in one process, each on its own fresh database.

Contracts reaches identity exactly as in compose (``RemoteAuthorizer`` plus a JWKS fetch for the
local issuer), but the HTTP client is an ``httpx.ASGITransport`` straight into the identity app,
so no network and no second container are needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dc_auth.authorizer import Authorizer, AuthorizerUnavailable, Decision, FolderScope
from dc_auth.issuers import AuthSettings, IssuerConfig
from dc_auth.principal import Principal
from dc_auth.testing import FakeIssuer
from dc_contracts.main import create_contracts_app
from dc_contracts.migrate import SCRIPT_LOCATION, VERSION_TABLE
from dc_contracts.settings import ContractsSettings
from dc_core.migrations import run_alembic
from dc_identity.main import create_identity_app
from dc_identity.migrate import SCRIPT_LOCATION as IDENTITY_SCRIPT_LOCATION
from dc_identity.migrate import VERSION_TABLE as IDENTITY_VERSION_TABLE
from dc_identity.settings import IdentitySettings
from dc_validation.main import create_validation_app
from dc_validation.migrate import SCRIPT_LOCATION as VALIDATION_SCRIPT_LOCATION
from dc_validation.migrate import VERSION_TABLE as VALIDATION_VERSION_TABLE
from dc_validation.settings import ValidationSettings
from fastapi import FastAPI
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy.engine import make_url

LOCAL_ISSUER = "http://identity:8000"
LOGIN = "/api/v1/identity/auth/login"
# The hosts the card and delivery tests send webhooks to (quick 261120-s0p: the list is mandatory).
DEFAULT_WEBHOOK_HOSTS = [
    "*.example.test",
    "example.test",
    "localhost",
    "*.example.net",
    "*.example.org",
]
PASSWORD = "/api/v1/identity/auth/password"
FOLDERS = "/api/v1/identity/folders"
ADMIN_PASSWORD = "an admin password of 30 chars!"  # noqa: S105

AsgiClient = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]
Sql = Callable[..., list[dict[str, Any]]]

# Must return zero rows after any lifecycle or concurrency scenario (research invariant probe):
# the Latest pointer and the flag agree for every contract, and no two non-draft rows of a
# contract share a typed semver. Pins (Phase 04.1): no active pin on a version that is not
# published, on a deleted contract or on an archived card, and never two active pins of one
# version on one card.
INVARIANT_SQL = """
SELECT c.id::text AS contract_id, 'latest-pointer-flag-disagree' AS problem FROM contract c
LEFT JOIN LATERAL (SELECT count(*) AS n, min(id::text) AS only_id
                   FROM contract_version v WHERE v.contract_id = c.id AND v.is_latest) l ON true
WHERE (c.latest_version_id IS NULL AND l.n <> 0)
   OR (c.latest_version_id IS NOT NULL AND (l.n <> 1 OR l.only_id <> c.latest_version_id::text))
UNION ALL
SELECT contract_id::text, 'duplicate-semver' FROM contract_version WHERE state <> 'draft'
GROUP BY contract_id, semver_major, semver_minor, semver_patch, semver_prerelease
HAVING count(*) > 1
UNION ALL
SELECT s.id::text, 'pin-on-unpublished-version' FROM card_subscription s
JOIN contract_version v ON v.id = s.version_id
WHERE s.ended_at IS NULL AND v.state <> 'published'
UNION ALL
SELECT s.id::text, 'pin-on-deleted-contract' FROM card_subscription s
JOIN contract c ON c.id = s.contract_id
WHERE s.ended_at IS NULL AND c.deleted_at IS NOT NULL
UNION ALL
SELECT s.id::text, 'pin-on-archived-card' FROM card_subscription s
JOIN subscriber_card k ON k.id = s.card_id
WHERE s.ended_at IS NULL AND k.archived_at IS NOT NULL
UNION ALL
SELECT card_id::text, 'duplicate-active-pin' FROM card_subscription WHERE ended_at IS NULL
GROUP BY card_id, version_id HAVING count(*) > 1
"""


def odcs_doc(odcs_id: str, *, version: str) -> bytes:
    """A minimal valid ODCS document with the given declared version."""
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        "status: draft\nname: Orders\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class IdentityLink(httpx.AsyncBaseTransport):
    """Contracts' route to identity: straight into the app, with a switch to cut it (an outage)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.down = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("identity is unreachable", request=request)
        return await self._inner.handle_async_request(request)


class ValidationLink(httpx.AsyncBaseTransport):
    """Contracts' route to validation: straight into the app, with switches to break it.

    ``down`` cuts the link (a connection error); ``delay_s`` waits before every call (a slow
    answer, so the client's timeout fires); ``status_override`` (set with ``force(status, ...)``)
    answers every call with a canned response instead; ``rewrite`` changes the JSON body of the
    real answer before contracts reads it; ``requests`` records what contracts sent (headers and
    body), so a test can prove the caller's own bearer was forwarded and nothing else.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.down = False
        self.delay_s = 0.0
        self.requests: list[httpx.Request] = []
        self.status_override: tuple[int, dict[str, str], bytes] | None = None
        self.rewrite: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        # Awaited with each request before it is answered (a test probes contracts' database).
        self.on_request: Callable[[httpx.Request], Awaitable[None]] | None = None

    def force(self, status: int, headers: dict[str, str] | None = None, body: bytes = b"") -> None:
        self.status_override = (status, headers or {}, body)

    def restore(self) -> None:
        self.down = False
        self.delay_s = 0.0
        self.status_override = None
        self.rewrite = None
        self.on_request = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        if self.on_request is not None:
            await self.on_request(request)
        if self.down:
            raise httpx.ConnectError("validation is unreachable", request=request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.status_override is not None:
            status, headers, body = self.status_override
            return httpx.Response(status, headers=headers, content=body, request=request)
        response = await self._inner.handle_async_request(request)
        if self.rewrite is None:
            return response
        await response.aread()
        rewritten = self.rewrite(json.loads(response.content))
        return httpx.Response(
            response.status_code,
            headers={"Content-Type": "application/json"},
            content=json.dumps(rewritten).encode(),
            request=request,
        )


class FlakyAuthorizer:
    """Delegates to the real authorizer; raises ``AuthorizerUnavailable`` for chosen calls.

    It defines ``grantees`` and ``check_members`` so it still satisfies ``GranteeAuthorizer``.
    """

    def __init__(
        self,
        inner: Authorizer,
        *,
        fail_permissions: frozenset[str],
        fail_grantees: bool,
        fail_check_members: bool,
    ) -> None:
        self._inner: Any = inner
        self._fail = fail_permissions
        self._fail_grantees = fail_grantees
        self._fail_check_members = fail_check_members

    async def check(
        self,
        principal: Principal,
        permission: str,
        folder_id: uuid.UUID | None = None,
        *,
        fresh: bool = False,
    ) -> Decision:
        if permission in self._fail:
            raise AuthorizerUnavailable("injected failure")
        return await self._inner.check(principal, permission, folder_id, fresh=fresh)

    async def accessible_folders(self, principal: Principal, permission: str) -> FolderScope:
        # A failing permission fails its scope too: card_capability reads the folder:read scope
        # of a linked member instead of the folder check (WR-02).
        if permission in self._fail:
            raise AuthorizerUnavailable("injected failure")
        return await self._inner.accessible_folders(principal, permission)

    async def grantees(self, principal: Principal) -> frozenset[tuple[str, str]]:
        if self._fail_grantees:
            raise AuthorizerUnavailable("injected failure")
        found: frozenset[tuple[str, str]] = await self._inner.grantees(principal)
        return found

    async def check_members(
        self, principal: Principal, folder_id: uuid.UUID, members: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], bool]:
        if self._fail_check_members:
            raise AuthorizerUnavailable("injected failure")
        answer: dict[tuple[str, str], bool] = await self._inner.check_members(
            principal, folder_id, members
        )
        return answer


UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def rule_body(
    message: str = "Property names must be unique.",
    *,
    condition: str = UNIQUE_NAMES,
    severity: str = "error",
    target: str = "$.schema[*]",
) -> str:
    """A rule-set rule (JSON) with the pass and fail fixtures a publish needs."""
    return json.dumps(
        {
            "target": target,
            "condition": condition,
            "severity": severity,
            "message": message,
            "fixtures": {
                "pass": [{"document": table("id", "name")}],
                "fail": [{"document": table("id", "id")}],
            },
        }
    )


def drop_platform_seed(db_url: str) -> None:
    """Remove the platform rule set validation's migration 0005 seeds (leaving the schema as is).

    Most contracts suites do not care about governance; ``make_stack(platform_seed=True)`` keeps
    the seed. The delete guard on published versions is lifted for this one statement batch only.
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
class Chain:
    """An Organization -> Project -> Team chain built through identity."""

    org: str
    project: str
    team: str


@dataclass
class Stack:
    identity_app: FastAPI
    identity: httpx.AsyncClient  # talks to identity
    contracts_app: FastAPI
    client: httpx.AsyncClient  # talks to contracts
    contracts_db_url: str
    identity_db_url: str
    admin_token: str
    root_id: str
    identity_link: IdentityLink
    fake_issuer: FakeIssuer
    validation_app: FastAPI
    validation: httpx.AsyncClient  # talks to validation's public API
    validation_link: ValidationLink  # contracts' (switchable) route to validation
    validation_db_url: str
    # Highest outbox id ``pump_notices`` has already handled (its own high-water mark).
    notice_mark: int = field(default=0)

    rule_body = staticmethod(rule_body)

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

    async def team_in_org(self, org: str, slug: str) -> str:
        project = await self.folder(org, f"p-{slug}")
        return await self.folder(project, f"t-{slug}")

    async def post(
        self,
        folder_id: str,
        body: bytes,
        media_type: str = "application/yaml",
        *,
        token: str | None = None,
    ) -> httpx.Response:
        return await self.client.post(
            f"/api/v1/contracts/folders/{folder_id}/contracts",
            content=body,
            headers={**bearer(token or self.admin_token), "Content-Type": media_type},
        )

    async def create_contract(
        self,
        team: str,
        body: bytes,
        media_type: str = "application/yaml",
        *,
        token: str | None = None,
    ) -> tuple[str, str, str]:
        """Create a contract; returns (contract id, draft id, draft ETag) from the 201 body."""
        response = await self.post(team, body, media_type, token=token)
        assert response.status_code == 201, response.text
        created = response.json()
        return created["contract"]["id"], created["draft"]["id"], created["draft"]["etag"]

    async def publish(
        self,
        contract_id: str,
        draft_id: str,
        etag: str | None,
        *,
        token: str | None = None,
    ) -> httpx.Response:
        """POST the publish route; ``etag=None`` sends no If-Match header."""
        headers = bearer(token or self.admin_token)
        if etag is not None:
            headers["If-Match"] = etag
        return await self.client.post(
            f"/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/publish",
            headers=headers,
        )

    async def create_card(
        self,
        folder_id: str,
        *,
        token: str | None = None,
        name: str | None = None,
        description: str = "d",
        contacts: list[dict[str, Any]] | None = None,
        email_enabled: bool = False,
    ) -> dict[str, Any]:
        """POST a card and assert 201; returns the body."""
        response = await self.client.post(
            f"/api/v1/contracts/folders/{folder_id}/cards",
            json={
                "name": name or f"card-{uuid.uuid4().hex[:8]}",
                "description": description,
                "contacts": contacts or [],
                "email_enabled": email_enabled,
            },
            headers=bearer(token or self.admin_token),
        )
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    @contextmanager
    def flaky_authz(
        self,
        *,
        fail_permissions: frozenset[str] = frozenset(),
        fail_grantees: bool = False,
        fail_check_members: bool = False,
    ) -> Iterator[None]:
        """Swap the contracts app's authorizer factory for one that fails the chosen calls."""
        state = self.contracts_app.state
        original = state.authorizer_factory
        state.authorizer_factory = lambda request: FlakyAuthorizer(
            original(request),
            fail_permissions=fail_permissions,
            fail_grantees=fail_grantees,
            fail_check_members=fail_check_members,
        )
        try:
            yield
        finally:
            state.authorizer_factory = original

    async def local_group(self, owner_folder_id: str, name: str) -> str:
        """Create a local group through identity as the Global admin; returns its id."""
        response = await self.identity.post(
            "/api/v1/identity/groups",
            json={"owner_folder_id": owner_folder_id, "name": name},
            headers=bearer(self.admin_token),
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    def group_member_sql(self, group_id: str, principal_id: str) -> None:
        """Put a user principal into a local group straight in identity's database."""
        url = self.identity_db_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO group_members (group_id, member_type, member_id, created_by, "
                "updated_by) VALUES (%s, 'user', %s, 'system:test', 'system:test')",
                (group_id, principal_id),
            )

    async def service_account_key(self, owner_folder_id: str, name: str) -> tuple[str, str]:
        """Create a service account and one key; returns (key token, ``sa:<uuid>``)."""
        created = await self.identity.post(
            "/api/v1/identity/service-accounts",
            json={"owner_folder_id": owner_folder_id, "name": name},
            headers=bearer(self.admin_token),
        )
        assert created.status_code == 201, created.text
        account = created.json()
        key = await self.identity.post(
            f"/api/v1/identity/service-accounts/{account['id']}/keys",
            json={"name": "k", "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            headers=bearer(self.admin_token),
        )
        assert key.status_code == 201, key.text
        return str(key.json()["token"]), str(account["principal_id"])

    async def link_member(
        self, card_id: str, member_type: str, member_id: str, *, token: str | None = None
    ) -> httpx.Response:
        return await self.client.post(
            f"/api/v1/contracts/cards/{card_id}/members",
            json={"member_type": member_type, "member_id": member_id},
            headers=bearer(token or self.admin_token),
        )

    def policy_manager(self, folder_id: str, sub: str | None = None) -> str:
        """A token for a new Policy Manager holding the role at ``folder_id``."""
        token, principal_id = self.token(sub or f"pm-{uuid.uuid4().hex[:8]}")
        self.grant_sql(principal_id, "policy_manager", folder_id)
        return token

    async def new_rule_set(
        self,
        folder_id: str,
        token: str,
        rules: dict[str, str],
        *,
        name: str = "Org naming",
    ) -> str:
        """Create a rule set holding ``rules`` (key -> rule JSON) and publish version 1."""
        headers = bearer(token)
        created = await self.validation.post(
            f"/api/v1/validation/folders/{folder_id}/rule-sets",
            json={"name": name, "description": "rules"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        rule_set_id = str(created.json()["id"])
        await self.publish_rule_version(rule_set_id, token, rules)
        return rule_set_id

    async def publish_rule_version(
        self, rule_set_id: str, token: str, rules: dict[str, str]
    ) -> None:
        """Put each rule into the draft and publish it as the next version."""
        base = f"/api/v1/validation/rule-sets/{rule_set_id}"
        current = await self.validation.get(base, headers=bearer(token))
        assert current.status_code == 200, current.text
        etag = current.headers["ETag"]
        for key, body in rules.items():
            put = await self.validation.put(
                f"{base}/draft/rules/{key}",
                content=body,
                headers={**bearer(token), "If-Match": etag, "Content-Type": "application/json"},
            )
            assert put.status_code == 200, put.text
            etag = put.headers["ETag"]
        published = await self.validation.post(
            f"{base}/publish", headers={**bearer(token), "If-Match": etag}
        )
        assert published.status_code == 201, published.text

    async def mandatory_rule_set(
        self,
        folder_id: str,
        rules: dict[str, str] | None = None,
        *,
        enforce: bool = True,
        name: str = "Org naming",
        token: str | None = None,
    ) -> str:
        """A Policy Manager publishes a rule set at ``folder_id`` and makes it mandatory.

        ``enforce=True`` also enforces version 1 now (otherwise it is in its warn window).
        Returns the rule set id.
        """
        pm = token or self.policy_manager(folder_id)
        rule_set_id = await self.new_rule_set(
            folder_id, pm, rules or {"unique-property-names": rule_body()}, name=name
        )
        base = f"/api/v1/validation/rule-sets/{rule_set_id}"
        mandated = await self.validation.put(
            f"{base}/mandatory", json={"mandatory": True}, headers=bearer(pm)
        )
        assert mandated.status_code == 200, mandated.text
        if enforce:
            enforced = await self.validation.post(
                f"{base}/versions/1/enforce-now", headers=bearer(pm)
            )
            assert enforced.status_code == 200, enforced.text
        return rule_set_id

    async def publish_versions(
        self, team: str, semvers: Sequence[str], *, odcs_id: str | None = None
    ) -> tuple[str, dict[str, str]]:
        """Publish ``semvers`` (ascending) as one contract; returns (contract id, semver -> id).

        The first is published as the Latest; every later one is a Preview.
        """
        ident = odcs_id or f"urn:dc:subs:{uuid.uuid4().hex}"
        contract, draft, etag = await self.create_contract(
            team, odcs_doc(ident, version=semvers[0])
        )
        first = await self.publish(contract, draft, etag)
        assert first.status_code == 201, first.text
        ids = {semvers[0]: draft}
        base = f"/api/v1/contracts/contracts/{contract}"
        for previous, semver in zip(semvers, semvers[1:], strict=False):
            created = await self.client.post(
                f"{base}/drafts",
                json={"from_version": previous, "bump": "none"},
                headers=bearer(self.admin_token),
            )
            assert created.status_code == 201, created.text
            new_draft = created.json()["draft"]
            saved = await self.client.put(
                f"{base}/drafts/{new_draft['id']}",
                content=odcs_doc(ident, version=semver),
                headers={
                    **bearer(self.admin_token),
                    "Content-Type": "application/yaml",
                    "If-Match": new_draft["etag"],
                },
            )
            assert saved.status_code == 200, saved.text
            published = await self.publish(contract, new_draft["id"], saved.headers["etag"])
            assert published.status_code == 201, published.text
            ids[semver] = new_draft["id"]
        return contract, ids

    async def _handle_outbox_row(self, row: Any) -> int:
        """Hand one outbox row to ``handle_payload`` exactly as the relay would publish it."""
        from dc_contracts.notices import handle_payload
        from dc_core.events import build_envelope, encode_envelope, subject_for

        state = self.contracts_app.state
        return await handle_payload(
            state.session_factory,
            encode_envelope(build_envelope(row, service="contracts")),
            subject=subject_for("contracts", row.aggregate_type, row.action),
            settings=state.settings,
        )

    async def pump_notices(self) -> int:
        """Handle every contracts outbox row above this stack's high-water mark, in id order.

        Returns the number of notices created. Stands in for the JetStream consumer (Plan 09).
        """
        from dc_contracts.models import OutboxEvent
        from sqlalchemy import select

        async with self.contracts_app.state.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(OutboxEvent)
                        .where(OutboxEvent.id > self.notice_mark)
                        .order_by(OutboxEvent.id)
                    )
                )
                .scalars()
                .all()
            )
        created = 0
        for row in rows:
            created += await self._handle_outbox_row(row)
            self.notice_mark = row.id
        return created

    async def replay_event(self, event_id: str | uuid.UUID) -> int:
        """Handle one outbox row again (redelivery); returns the notices created."""
        from dc_contracts.models import OutboxEvent
        from sqlalchemy import select

        async with self.contracts_app.state.session_factory() as session:
            row = (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.event_id == uuid.UUID(str(event_id)))
                )
            ).scalar_one()
        return await self._handle_outbox_row(row)

    async def subscribe(
        self, card_id: str, contract_id: str, version: str, *, token: str | None = None
    ) -> httpx.Response:
        return await self.client.post(
            f"/api/v1/contracts/cards/{card_id}/subscriptions",
            json={"contract_id": contract_id, "version": version},
            headers=bearer(token or self.admin_token),
        )


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
    """A second per-test database (``fresh_db_url`` is the first)."""
    name = f"t_{uuid.uuid4().hex}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_url(pg_admin_url).set(drivername="postgresql+psycopg", database=name)
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def third_db_url(pg_admin_url: str) -> Any:
    """A third per-test database: validation's (identity and contracts own the other two)."""
    name = f"t_{uuid.uuid4().hex}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_url(pg_admin_url).set(drivername="postgresql+psycopg", database=name)
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


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
class SinkRequest:
    """One request the webhook sink received: raw bytes and lower-cased header names."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class WebhookSink:
    """A threaded stdlib HTTP server on 127.0.0.1 standing in for a consumer's receiver.

    Answers 200 unless told otherwise: ``fail_next(n, status)`` queues failing answers,
    ``respond_json(status, body)`` queues a JSON answer, ``answer_always(status)`` changes the
    default. ``stop()`` closes the port so later requests are refused.
    """

    def __init__(self) -> None:
        self.requests: list[SinkRequest] = []
        self._queue: deque[tuple[int, bytes]] = deque()
        self._default_status = 200
        self._lock = threading.Lock()
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with sink._lock:
                    sink.requests.append(
                        SinkRequest(
                            method=self.command,
                            path=self.path,
                            headers={k.lower(): v for k, v in self.headers.items()},
                            body=body,
                        )
                    )
                    status, payload = (
                        sink._queue.popleft() if sink._queue else (sink._default_status, b"")
                    )
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                if payload:
                    self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            do_POST = _handle  # noqa: N815 - the http.server hook name
            do_GET = _handle  # noqa: N815

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._stopped = False

    def url(self, path: str = "/hooks") -> str:
        return f"http://localhost:{self.port}{path}"

    def fail_next(self, count: int, status: int = 500) -> None:
        with self._lock:
            self._queue.extend([(status, b"")] * count)

    def respond_json(self, status: int, body: Any) -> None:
        with self._lock:
            self._queue.append((status, json.dumps(body).encode()))

    def answer_always(self, status: int) -> None:
        with self._lock:
            self._default_status = status

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def webhook_sink() -> Iterator[WebhookSink]:
    sink = WebhookSink()
    try:
        yield sink
    finally:
        sink.stop()


@pytest.fixture
def invariant(sql: Sql) -> Callable[[str], list[dict[str, Any]]]:
    """``invariant(db_url)`` runs ``INVARIANT_SQL``; an empty list means every invariant holds."""

    def _check(db_url: str) -> list[dict[str, Any]]:
        return sql(db_url, INVARIANT_SQL)

    return _check


@pytest.fixture
def make_stack(
    tmp_path: Path,
    fresh_db_url: str,
    second_db_url: str,
    third_db_url: str,
    fake_issuer: FakeIssuer,
    asgi_client: AsgiClient,
    sql: Sql,
) -> Callable[..., AbstractAsyncContextManager[Stack]]:
    """``async with make_stack() as stack``: identity, validation and contracts running, the admin
    logged in.

    Contracts publishes through the real validation app (``ValidationPublishGate`` over a
    switchable ``ValidationLink``), exactly as in compose. Keyword arguments override
    ``ContractsSettings``, except ``platform_seed`` (default False: the seeded ``odcs-structural``
    platform rule set is removed after migrating; True keeps it) and ``validation_overrides`` (a
    dict of ``ValidationSettings`` overrides). A module that publishes should carry
    ``pytest.mark.asyncio(loop_scope="session")`` when its rules run in the sandbox.
    """

    @asynccontextmanager
    async def _make(
        **settings_overrides: Any,
    ) -> AsyncIterator[Stack]:
        secret = secrets.token_urlsafe(30)[:40].ljust(40, "x")
        secret_file = tmp_path / "bootstrap_admin_secret"
        secret_file.write_text(secret + "\n", encoding="utf-8")
        key_file = tmp_path / "jwt_signing_key.pem"
        _write_rsa_key(key_file)
        run_alembic(
            script_location=IDENTITY_SCRIPT_LOCATION,
            version_table=IDENTITY_VERSION_TABLE,
            url=fresh_db_url,
            command="upgrade",
        )
        run_alembic(
            script_location=SCRIPT_LOCATION,
            version_table=VERSION_TABLE,
            url=second_db_url,
            command="upgrade",
        )
        run_alembic(
            script_location=VALIDATION_SCRIPT_LOCATION,
            version_table=VALIDATION_VERSION_TABLE,
            url=third_db_url,
            command="upgrade",
        )
        if not settings_overrides.pop("platform_seed", False):
            drop_platform_seed(third_db_url)
        validation_overrides: dict[str, Any] = settings_overrides.pop("validation_overrides", {})
        # Quick 261120-s0p: the webhook allow-list is mandatory, so the stack stores one through
        # identity after the admin login (``webhook_hosts=None`` stores none) and the start-up seed
        # is off unless a test turns it on; ``identity_overrides`` are ``IdentitySettings``
        # overrides (a seed test provisions the notifier key with ``notifier_credential_file``).
        webhook_hosts: list[str] | None = settings_overrides.pop(
            "webhook_hosts", DEFAULT_WEBHOOK_HOSTS
        )
        settings_overrides.setdefault("webhook_host_seed_enabled", False)
        identity_overrides: dict[str, Any] = settings_overrides.pop("identity_overrides", {})
        if settings_overrides.pop("with_notifier", False):
            # A real notifier: identity provisions the service account from a credential file and
            # contracts presents its key, so the worker reads the stored allow-list like in compose.
            public_id, notifier_secret = secrets.token_hex(8), secrets.token_urlsafe(32)
            credential = tmp_path / "notifier_credential"
            credential.write_text(
                f"{public_id}:{hashlib.sha256(notifier_secret.encode()).hexdigest()}\n",
                encoding="utf-8",
            )
            identity_overrides.setdefault("notifier_credential_file", credential)
            settings_overrides.setdefault(
                "mail_api_key", SecretStr(f"dcp_{public_id}_{notifier_secret}")
            )
        identity_settings = IdentitySettings(
            database_url=fresh_db_url,  # type: ignore[arg-type]
            bootstrap_secret_file=secret_file,
            jwt_signing_key_file=key_file,
            local_issuer=LOCAL_ISSUER,
            **identity_overrides,
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
            link = IdentityLink(httpx.ASGITransport(app=identity_app))
            contracts_http = httpx.AsyncClient(transport=link)
            # Validation asks identity too, through its own client straight into the same app.
            validation_identity = httpx.AsyncClient(transport=httpx.ASGITransport(app=identity_app))
            validation_overrides.setdefault("eval_workers", 1)
            validation_overrides.setdefault("queue_wait_s", 0.5)
            validation_app = create_validation_app(
                ValidationSettings(
                    database_url=third_db_url,  # type: ignore[arg-type]
                    **validation_overrides,
                ),
                auth_settings,
                http_client=validation_identity,
            )
            # Tests drive ``app.state.card_notifier.run_once()`` by hand; the loop stays off.
            settings_overrides.setdefault("notify_worker_enabled", False)
            # Tests drive ``app.state.sunset_scheduler.scan_once()`` by hand too.
            settings_overrides.setdefault("sunset_scheduler_enabled", False)
            contracts_settings = ContractsSettings(
                database_url=second_db_url,  # type: ignore[arg-type]
                **settings_overrides,
            )
            try:
                async with asgi_client(validation_app) as validation:
                    validation_link = ValidationLink(httpx.ASGITransport(app=validation_app))
                    validation_http = httpx.AsyncClient(
                        transport=validation_link, base_url="http://validation"
                    )
                    contracts_app = create_contracts_app(
                        contracts_settings,
                        auth_settings,
                        http_client=contracts_http,
                        validation_http_client=validation_http,
                    )
                    try:
                        async with asgi_client(contracts_app) as client:
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
                            root = sql(fresh_db_url, "SELECT id FROM folder WHERE kind = 'root'")[
                                0
                            ]["id"]
                            if webhook_hosts:
                                stored = await identity.put(
                                    "/api/v1/identity/settings/notifications",
                                    json={"webhook_allowed_hosts": webhook_hosts},
                                    headers={
                                        **bearer(login.json()["access_token"]),
                                        "If-Match": '"notifications-r0"',
                                    },
                                )
                                assert stored.status_code == 200, stored.text
                            yield Stack(
                                identity_app=identity_app,
                                identity=identity,
                                contracts_app=contracts_app,
                                client=client,
                                contracts_db_url=second_db_url,
                                identity_db_url=fresh_db_url,
                                admin_token=login.json()["access_token"],
                                root_id=str(root),
                                identity_link=link,
                                fake_issuer=fake_issuer,
                                validation_app=validation_app,
                                validation=validation,
                                validation_link=validation_link,
                                validation_db_url=third_db_url,
                            )
                    finally:
                        await validation_http.aclose()
            finally:
                await contracts_http.aclose()
                await validation_identity.aclose()

    return _make
