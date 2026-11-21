"""Every denied permission check leaves one committed identity outbox row (AUD-04, D-172, D-173).

The row is ``authz`` / ``check.denied`` (subject ``audit.identity.authz.check.denied``) and its
envelope is what audit ingests as category ``denial``. ALLOW writes nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from dc_audit.ingest import AuditEnvelope, category_for
from dc_auth.authorizer import Outcome
from dc_auth.principal import Principal
from dc_auth.testing import ENTRA_TENANT_ID
from dc_core.db import make_session_factory
from dc_core.events import build_envelope, subject_for
from dc_core.problems import ProblemError
from dc_identity.authz import LocalAuthorizer
from dc_identity.denials import DETAIL_KEYS, DenialRecorder
from dc_identity.directory.fake import FakeDirectoryClient
from dc_identity.gate import PrincipalGate
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

CHECK = "/internal/v1/authz/check"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]


def denial_rows(sql: Sql, db_url: str) -> list[dict[str, Any]]:
    return sql(
        db_url,
        "SELECT * FROM outbox WHERE aggregate_type = 'authz' AND action = 'check.denied' "
        "ORDER BY id",
    )


def root_of(sql: Sql, db_url: str) -> uuid.UUID:
    return uuid.UUID(str(sql(db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]))


async def post_check(
    client: Any,
    token: str,
    permission: str,
    folder_id: object | None = None,
    **extra: Any,
) -> Any:
    body: dict[str, Any] = {"permission": permission, **extra}
    if folder_id is not None:
        body["folder_id"] = str(folder_id)
    return await client.post(CHECK, json=body, headers={"Authorization": f"Bearer {token}"})


async def test_a_not_found_check_writes_one_denial_row_that_audit_ingests_as_a_denial(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    sql: Sql,
) -> None:
    env = make_identity()
    target = uuid.uuid4()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        user = await create_user(client, admin, "nogrants")
        token = await login(client, user.username, user.password)
        before = len(denial_rows(sql, env.db_url))
        response = await post_check(client, token, "folder:read", target)
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "not_found"

    rows = denial_rows(sql, env.db_url)
    assert len(rows) == before + 1
    row = rows[-1]
    assert row["aggregate_id"] == str(target)
    assert row["scope_path"] == []
    assert row["actor"]["id"] == f"local:{user.id}"
    assert row["before"] is None
    assert row["after"] is None
    assert set(row["detail"]) == DETAIL_KEYS
    assert row["detail"]["permission"] == "folder:read"
    assert row["detail"]["outcome"] == "not_found"
    assert all(isinstance(row["detail"][key], str) for key in ("service", "route"))

    envelope = AuditEnvelope.model_validate(
        build_envelope(SimpleNamespace(**row), service="identity")
    )
    subject = subject_for("identity", "authz", "check.denied")
    assert subject == "audit.identity.authz.check.denied"
    assert category_for(subject, envelope) == "denial"
    assert envelope.detail == row["detail"]


async def test_an_allowed_check_writes_no_denial_row(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        before = sql(env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"]
        response = await post_check(client, admin, "folder:read", root_of(sql, env.db_url))
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "allow"
        assert sql(env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"] == before
    assert denial_rows(sql, env.db_url) == []


# --- every non-ALLOW outcome (Task 2) -------------------------------------------------------


@dataclass
class World:
    env: Any
    client: Any
    admin: str
    root: uuid.UUID
    org: uuid.UUID
    project: uuid.UUID
    team: uuid.UUID
    user: Any
    token: str

    @property
    def team_scope(self) -> list[uuid.UUID]:
        return [self.root, self.org, self.project, self.team]

    def authorizer(self, **kwargs: Any) -> LocalAuthorizer:
        state = self.env.app.state
        return LocalAuthorizer(state.session_factory, state.principal_gate, **kwargs)


@pytest.fixture
def world(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    sql: Sql,
) -> Callable[[], Any]:
    """``async with world() as w``: ROOT -> O -> P -> T and a local user with no grants."""

    @asynccontextmanager
    async def _world() -> AsyncIterator[World]:
        env = make_identity()
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            root = root_of(sql, env.db_url)
            org, project, team = await folder_chain(client, admin, root, "dn")
            user = await create_user(client, admin, "denied")
            token = await login(client, user.username, user.password)
            yield World(
                env,
                client,
                admin,
                root,
                uuid.UUID(org),
                uuid.UUID(project),
                uuid.UUID(team),
                user,
                token,
            )

    return _world


def local_principal(user_id: str, *, scopes: frozenset[str] = frozenset({"api"})) -> Principal:
    return Principal(
        id=f"local:{user_id}",
        type="user",
        display="denied",
        issuer="http://identity:8000",
        subject=user_id,
        via="jwt",
        scopes=scopes,
    )


def entra_principal(oid: str) -> Principal:
    return Principal(
        id=f"entra:{ENTRA_TENANT_ID}:{oid}",
        type="user",
        display="entra",
        issuer="https://login.microsoftonline.com/x/v2.0",
        subject=oid,
        via="jwt",
        scopes=frozenset({"api"}),
    )


def only_denial(sql: Sql, db_url: str) -> dict[str, Any]:
    rows = denial_rows(sql, db_url)
    assert len(rows) == 1, rows
    return rows[0]


async def test_a_root_permission_not_held_is_recorded_at_root(world: Any, sql: Sql) -> None:
    async with world() as w:
        response = await post_check(w.client, w.token, "user:read")
        assert response.json()["outcome"] == "forbidden"
    row = only_denial(sql, w.env.db_url)
    assert row["aggregate_id"] == "root"
    assert row["scope_path"] == [w.root]
    assert row["detail"]["outcome"] == "forbidden"
    assert row["detail"]["permission"] == "user:read"


async def test_a_folder_permission_without_a_folder_is_recorded_with_no_target(
    world: Any, sql: Sql
) -> None:
    async with world() as w:
        response = await post_check(w.client, w.token, "folder:read")
        assert response.json()["outcome"] == "forbidden"
    row = only_denial(sql, w.env.db_url)
    assert row["aggregate_id"] == "none"
    assert row["scope_path"] == []
    assert row["detail"]["outcome"] == "forbidden"


async def test_a_readable_folder_without_the_permission_is_recorded_with_its_ancestors(
    world: Any, sql: Sql, grant_sql: Callable[..., None]
) -> None:
    async with world() as w:
        grant_sql(w.env.db_url, w.user.principal_id, "team_reader", w.team)
        response = await post_check(w.client, w.token, "contract:create", w.team)
        assert response.json()["outcome"] == "forbidden"
    row = only_denial(sql, w.env.db_url)
    assert row["aggregate_id"] == str(w.team)
    assert row["scope_path"] == w.team_scope
    assert row["detail"] == {
        **row["detail"],
        "permission": "contract:create",
        "outcome": "forbidden",
    }


async def test_a_read_only_pat_on_a_principal_level_write_is_recorded(world: Any, sql: Sql) -> None:
    from datetime import UTC, datetime, timedelta

    async with world() as w:
        created = await w.client.post(
            "/api/v1/identity/pats",
            json={
                "name": "ro",
                "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                "read_only": True,
            },
            headers={"Authorization": f"Bearer {w.token}"},
        )
        assert created.status_code == 201, created.text
        response = await post_check(w.client, created.json()["token"], "pat:manage")
        assert response.json()["outcome"] == "forbidden"
    row = only_denial(sql, w.env.db_url)
    assert row["actor"]["via"] == "pat"
    assert row["detail"]["permission"] == "pat:manage"
    assert row["detail"]["outcome"] == "forbidden"


async def test_an_unknown_permission_is_recorded_and_capped_at_64_characters(
    world: Any, sql: Sql
) -> None:
    permission = "no-such:" + "x" * 100
    async with world() as w:
        response = await post_check(w.client, w.token, permission)
        assert response.json()["outcome"] == "forbidden"
    row = only_denial(sql, w.env.db_url)
    assert row["detail"]["permission"] == permission[:64]
    assert row["aggregate_id"] == "none"


async def test_a_deactivated_user_is_recorded_as_gate_refused_with_the_folder_ancestors(
    world: Any, sql: Sql
) -> None:
    async with world() as w:
        deactivated = await w.client.post(
            f"/api/v1/identity/users/{w.user.id}/deactivate",
            headers={"Authorization": f"Bearer {w.admin}"},
        )
        assert deactivated.status_code == 200, deactivated.text
        with pytest.raises(ProblemError) as raised:
            await w.authorizer().check(local_principal(w.user.id), "folder:read", w.team)
    assert raised.value.status == 401
    assert raised.value.slug == "unauthenticated"
    row = only_denial(sql, w.env.db_url)
    assert row["detail"]["outcome"] == "gate_refused"
    assert row["detail"]["permission"] == "folder:read"
    assert row["aggregate_id"] == str(w.team)
    assert row["scope_path"] == w.team_scope


async def test_a_gate_refused_root_permission_is_recorded_at_root(world: Any, sql: Sql) -> None:
    async with world() as w:
        sql(w.env.db_url, "UPDATE users SET is_active = false WHERE id = %s", w.user.id)
        with pytest.raises(ProblemError) as raised:
            await w.authorizer().check(local_principal(w.user.id), "user:read")
    assert raised.value.status == 401
    row = only_denial(sql, w.env.db_url)
    assert row["detail"]["outcome"] == "gate_refused"
    assert row["aggregate_id"] == "root"
    assert row["scope_path"] == [w.root]


async def test_an_unregistered_entra_principal_is_recorded_as_gate_refused(
    world: Any, sql: Sql
) -> None:
    oid = str(uuid.uuid4())
    async with world() as w:
        w.env.directory.add_principal("user", oid)
        with pytest.raises(ProblemError) as raised:
            await w.authorizer().check(entra_principal(oid), "folder:read", uuid.uuid4())
    assert raised.value.status == 403
    assert raised.value.slug == "principal-not-registered"
    row = only_denial(sql, w.env.db_url)
    assert row["detail"]["outcome"] == "gate_refused"
    assert row["actor"]["id"] == f"entra:{ENTRA_TENANT_ID}:{oid}"
    assert row["scope_path"] == []  # an unknown folder: visible at ROOT only


async def test_a_gate_503_is_not_a_denial_and_propagates_unchanged(world: Any, sql: Sql) -> None:
    oid = str(uuid.uuid4())
    async with world() as w:
        w.env.directory.add_principal("user", oid)
        w.env.directory.fail = True
        try:
            with pytest.raises(ProblemError) as raised:
                await w.authorizer().check(entra_principal(oid), "folder:read", w.team)
        finally:
            w.env.directory.fail = False
    assert raised.value.status == 503
    assert denial_rows(sql, w.env.db_url) == []


async def test_a_pat_on_an_interactive_only_permission_is_recorded_before_the_refusal(
    world: Any,
    sql: Sql,
    grant_sql: Callable[..., None],
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    async with world() as w:
        grant_sql(w.env.db_url, w.user.principal_id, "team_admin", w.team)
        pat = (await create_pat(w.client, w.token))["token"]
        response = await post_check(w.client, pat, "grant:manage", w.team)
    assert response.status_code == 403, response.text
    assert response.json()["type"] == "urn:dc:problem:interactive-session-required"
    row = only_denial(sql, w.env.db_url)
    assert row["detail"]["outcome"] == "interactive_required"
    assert row["detail"]["permission"] == "grant:manage"
    assert row["aggregate_id"] == str(w.team)
    assert row["scope_path"] == w.team_scope


def _failing_factory() -> Any:
    raise RuntimeError("database unavailable")


async def test_an_unrecordable_denial_is_a_503_and_never_returned(world: Any, sql: Sql) -> None:
    async with world() as w:
        recorder = DenialRecorder(cast(Any, _failing_factory))
        authorizer = w.authorizer(recorder=recorder)
        principal = local_principal(w.user.id)
        with pytest.raises(ProblemError) as missing:
            await authorizer.check(principal, "folder:read", uuid.uuid4())
        sql(w.env.db_url, "UPDATE users SET is_active = false WHERE id = %s", w.user.id)
        with pytest.raises(ProblemError) as gated:
            await authorizer.check(principal, "folder:read", w.team)
    for raised in (missing, gated):
        assert raised.value.status == 503
        assert raised.value.slug == "service-unavailable"
    assert denial_rows(sql, w.env.db_url) == []


async def test_twenty_concurrent_denials_write_twenty_rows_on_a_single_connection_pool(
    world: Any, sql: Sql
) -> None:
    async with world() as w:
        # One pooled connection and no overflow: a check that held two connections at once
        # would wait on itself and time out.
        engine = create_async_engine(w.env.db_url, pool_size=1, max_overflow=0, pool_timeout=30)
        try:
            factory = make_session_factory(engine)
            authorizer = LocalAuthorizer(factory, PrincipalGate(factory, FakeDirectoryClient()))
            principal = local_principal(w.user.id)
            decisions = await asyncio.wait_for(
                asyncio.gather(
                    *(authorizer.check(principal, "folder:read", uuid.uuid4()) for _ in range(20))
                ),
                timeout=60,
            )
            assert {d.outcome for d in decisions} == {Outcome.NOT_FOUND}
            again = await authorizer.check(principal, "folder:read", w.team)
            assert again.outcome is Outcome.NOT_FOUND
        finally:
            await engine.dispose()
    assert len(denial_rows(sql, w.env.db_url)) == 21


# --- the asking service, the route template and the caller's request id (Task 3) -----------


async def test_a_remote_check_records_the_callers_service_route_and_request_id(
    world: Any, sql: Sql
) -> None:
    request_id = str(uuid.uuid4())
    drafts = "/api/v1/contracts/contracts/{contract_id}/drafts"
    async with world() as w:
        response = await w.client.post(
            CHECK,
            json={
                "permission": "contract:update",
                "folder_id": str(uuid.uuid4()),
                "service": "contracts",
                "route": drafts,
            },
            headers={"Authorization": f"Bearer {w.token}", "X-Request-ID": request_id},
        )
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "not_found"
    row = only_denial(sql, w.env.db_url)
    assert row["request_id"] == request_id
    assert row["detail"]["service"] == "contracts"
    assert row["detail"]["route"] == drafts


async def test_a_malformed_check_context_is_recorded_as_unknown_and_never_a_422(
    world: Any, sql: Sql
) -> None:
    async with world() as w:
        response = await post_check(
            w.client,
            w.token,
            "folder:read",
            uuid.uuid4(),
            service="Contracts!",
            route="/api/v1/x?token=abc",
        )
        assert response.status_code == 200, response.text
    row = only_denial(sql, w.env.db_url)
    assert row["detail"]["service"] == "unknown"
    assert row["detail"]["route"] == "unknown"


async def test_an_identity_route_records_service_identity_and_its_route_template(
    world: Any, sql: Sql
) -> None:
    async with world() as w:
        response = await w.client.get(
            f"/api/v1/identity/folders/{uuid.uuid4()}?probe=1",
            headers={"Authorization": f"Bearer {w.token}"},
        )
        assert response.status_code == 404, response.text
    row = only_denial(sql, w.env.db_url)
    assert row["detail"]["service"] == "identity"
    assert row["detail"]["route"] == "/api/v1/identity/folders/{folder_id}"
    assert row["detail"]["outcome"] == "not_found"


async def test_a_deactivated_caller_of_the_internal_check_is_recorded_once_as_authz_check(
    world: Any, sql: Sql
) -> None:
    async with world() as w:
        sql(w.env.db_url, "UPDATE users SET is_active = false WHERE id = %s", w.user.id)
        response = await post_check(
            w.client,
            w.token,
            "folder:read",
            w.team,
            service="contracts",
            route="/api/v1/contracts/contracts",
        )
        assert response.status_code == 401, response.text
    row = only_denial(sql, w.env.db_url)
    assert row["detail"] == {
        "permission": "authz:check",
        "outcome": "gate_refused",
        "service": "identity",
        "route": "/internal/v1/authz/check",
    }
    assert row["aggregate_id"] == "none"
    assert row["scope_path"] == []
