"""Phase tracer: an Entra token configured only through ``DC_AUTH_ISSUERS`` reaches identity's PDP.

An Entra v2 access token is verified by the shared issuer registry (``kind: entra``), becomes the
principal ``entra:<tid>:<oid>`` and holds exactly the role granted to that id through the
unchanged effective-roles SQL (AUTH-02, AUTH-03, D-58). Nothing under ``identity/src`` knows about
Entra in this plan.

Plan 02 inserts the admission gate (D-57) in front of the PDP: every case below that expects a
route to answer for an Entra principal registers that principal first (by SQL; the registration
API arrives in a later plan). An unregistered principal gets 403 ``principal-not-registered``
and no row is written; registration alone grants no role.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from dc_auth.issuers import IssuerRegistry
from dc_auth.jwks import JwksCache
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer
from dc_auth.verify import TokenVerifier
from dc_core.http import make_client

pytestmark = pytest.mark.integration

FOLDERS = "/api/v1/identity/folders"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
GrantSql = Callable[..., None]
Sql = Callable[..., list[dict[str, Any]]]

OTHER_TENANT = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
CHECK = "/internal/v1/authz/check"
ACCESSIBLE = "/internal/v1/authz/accessible-folders"
NOT_REGISTERED = "urn:dc:problem:principal-not-registered"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(
    sql: Sql,
    env: Any,
    kind: str,
    object_id: str,
    *,
    tenant_id: str = ENTRA_TENANT_ID,
    owner: str | None = None,
    directly_registered: bool = True,
    is_active: bool = True,
    groups: tuple[str, ...] = (),
    in_directory: bool = True,
) -> None:
    """Insert one entra_registrations row (users carry no owner folder, the rest need one).

    A user or service principal is also added to the fake directory (enabled, with ``groups``);
    a group is added as a directory group.
    """
    if in_directory:
        if kind == "group":
            env.directory.add_group(object_id, f"group {object_id[:8]}", tenant_id=tenant_id)
        else:
            env.directory.add_principal(kind, object_id, tenant_id=tenant_id, groups=groups)
    sql(
        env.db_url,
        "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
        "display_name, is_active, directly_registered, created_by, updated_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'test', 'test')",
        kind,
        tenant_id,
        object_id,
        owner,
        f"{kind} {object_id[:8]}",
        is_active,
        directly_registered,
    )


async def _root_id(client: Any, admin: str) -> str:
    resp = await client.get(FOLDERS, headers=_bearer(admin))
    assert resp.status_code == 200, resp.text
    return str(next(f["id"] for f in resp.json()["items"] if f["kind"] == "root"))


async def test_granted_entra_principal_holds_exactly_its_role(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    alice = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        _org, project, team = await folder_chain(client, admin, await _root_id(client, admin), "ea")
        _register(sql, env, "user", alice)
        grant_sql(env.db_url, f"entra:{ENTRA_TENANT_ID}:{alice}", "team_reader", team)
        token = fake_issuer.mint_entra(alice)
        granted = await client.get(f"{FOLDERS}/{team}", headers=_bearer(token))
        elsewhere = await client.get(f"{FOLDERS}/{project}", headers=_bearer(token))
    assert granted.status_code == 200, granted.text
    assert elsewhere.status_code == 404, elsewhere.text


async def test_registered_entra_principal_without_a_grant_never_reads_a_folder(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    dave = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        _org, _project, team = await folder_chain(
            client, admin, await _root_id(client, admin), "eb"
        )
        _register(sql, env, "user", dave)
        token = fake_issuer.mint_entra(dave)
        response = await client.get(f"{FOLDERS}/{team}", headers=_bearer(token))
    assert response.status_code == 404, response.text


async def test_entra_token_claims_grant_nothing(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    erin = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        _org, _project, team = await folder_chain(
            client, admin, await _root_id(client, admin), "ec"
        )
        _register(sql, env, "user", erin)
        token = fake_issuer.mint_entra(
            erin,
            extra_claims={"roles": ["global_admin"], "groups": ["x"], "ptype": "service_account"},
        )
        response = await client.get(f"{FOLDERS}/{team}", headers=_bearer(token))
    assert response.status_code == 404, response.text


async def test_token_of_an_unconfigured_tenant_is_401(
    make_identity: Callable[..., Any], asgi_client: Any, fake_issuer: FakeIssuer
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = fake_issuer.mint_entra(str(uuid.uuid4()), tid=OTHER_TENANT)
        response = await client.get(FOLDERS, headers=_bearer(token))
    assert response.status_code == 401, response.text
    assert response.json()["type"] == "urn:dc:problem:unauthenticated"


async def test_local_admin_token_still_works(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        response = await client.get(FOLDERS, headers=_bearer(admin))
    assert response.status_code == 200, response.text


async def test_verified_principal_carries_a_display_only_username_hint(
    make_identity: Callable[..., Any], fake_issuer: FakeIssuer
) -> None:
    env = make_identity()
    oid = str(uuid.uuid4())
    hint = "alice.example@corp.test"
    http = make_client()
    verifier = TokenVerifier(IssuerRegistry(env.auth_settings.issuers), JwksCache(http))
    principal = await verifier.verify(fake_issuer.mint_entra(oid, preferred_username=hint))
    await http.aclose()
    assert principal.username_hint == hint
    assert principal.id == f"entra:{ENTRA_TENANT_ID}:{oid}"
    assert hint not in principal.id
    assert "alice" not in principal.id


async def test_directly_registered_user_with_a_grant_reads_its_team(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    carol = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        _org, _project, team = await folder_chain(
            client, admin, await _root_id(client, admin), "ed"
        )
        _register(sql, env, "user", carol)
        grant_sql(env.db_url, f"entra:{ENTRA_TENANT_ID}:{carol}", "team_reader", team)
        response = await client.get(
            f"{FOLDERS}/{team}", headers=_bearer(fake_issuer.mint_entra(carol))
        )
    assert response.status_code == 200, response.text


async def test_unregistered_entra_principal_is_403_and_leaves_no_record(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    bob = str(uuid.uuid4())
    env.directory.add_principal("user", bob)
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        _org, _project, team = await folder_chain(
            client, admin, await _root_id(client, admin), "ee"
        )
        response = await client.get(
            f"{FOLDERS}/{team}", headers=_bearer(fake_issuer.mint_entra(bob))
        )
    assert response.status_code == 403, response.text
    assert response.json()["type"] == NOT_REGISTERED
    assert sql(env.db_url, "SELECT count(*) AS n FROM entra_users WHERE object_id = %s", bob) == [
        {"n": 0}
    ]


async def test_jit_only_service_principal_row_does_not_admit_on_its_own(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    sp9 = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root_id(client, admin)
        _org, _project, team = await folder_chain(client, admin, root, "ef")
        _register(sql, env, "service_principal", sp9, owner=root, directly_registered=False)
        response = await client.get(
            f"{FOLDERS}/{team}", headers=_bearer(fake_issuer.mint_entra(sp9, token_type="app"))
        )
    assert response.status_code == 403, response.text
    assert response.json()["type"] == NOT_REGISTERED


async def test_unregistered_principal_never_gets_allow_or_a_scope_from_the_internal_api(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    bob = str(uuid.uuid4())
    env.directory.add_principal("user", bob)
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        _org, _project, team = await folder_chain(
            client, admin, await _root_id(client, admin), "eg"
        )
        token = fake_issuer.mint_entra(bob)
        checked = await client.post(
            CHECK,
            json={"permission": "folder:read", "folder_id": team},
            headers=_bearer(token),
        )
        scoped = await client.post(
            ACCESSIBLE, json={"permission": "folder:read"}, headers=_bearer(token)
        )
    for response in (checked, scoped):
        assert response.status_code == 403, response.text
        assert response.json()["type"] == NOT_REGISTERED


async def test_a_deactivated_local_users_old_access_token_stops_working(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Any],
    login: Callable[..., Any],
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        user = await create_user(client, admin, "frank")
        token = await login(client, user.username, user.password)
        before = await client.get(FOLDERS, headers=_bearer(token))
        sql(env.db_url, "UPDATE users SET is_active = false WHERE username = 'frank'")
        listed = await client.get(FOLDERS, headers=_bearer(token))
        checked = await client.post(
            CHECK, json={"permission": "folder:list"}, headers=_bearer(token)
        )
    assert before.status_code == 200, before.text
    assert listed.status_code == 401, listed.text
    assert listed.json()["type"] == "urn:dc:problem:unauthenticated"
    assert checked.status_code == 401, checked.text


async def _group_world(
    env: Any,
    client: Any,
    admin: str,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    prefix: str,
) -> tuple[str, str, str, str]:
    """Group G registered under ROOT and granted team_reader on the team; returns G and ids."""
    root = await _root_id(client, admin)
    _org, project, team = await folder_chain(client, admin, root, prefix)
    group = str(uuid.uuid4())
    _register(sql, env, "group", group, owner=root)
    grant_sql(env.db_url, group, "team_reader", team, "entra_group")
    return group, root, project, team


async def test_user_admitted_through_a_registered_group_holds_exactly_that_groups_roles(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    alice = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, _root, project, team = await _group_world(
            env, client, admin, folder_chain, grant_sql, sql, "ga"
        )
        env.directory.add_principal("user", alice, groups=(group,))
        token = fake_issuer.mint_entra(alice)
        granted = await client.get(f"{FOLDERS}/{team}", headers=_bearer(token))
        elsewhere = await client.get(f"{FOLDERS}/{project}", headers=_bearer(token))
    assert granted.status_code == 200, granted.text
    assert elsewhere.status_code == 404, elsewhere.text
    assert [call[:2] for call in env.directory.calls if call[2] == alice][0] == (
        "lookup",
        ENTRA_TENANT_ID,
    )


async def test_outside_the_group_is_403_and_direct_registration_needs_no_group(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    bob, carol = str(uuid.uuid4()), str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        _group, _root, _project, team = await _group_world(
            env, client, admin, folder_chain, grant_sql, sql, "gb"
        )
        env.directory.add_principal("user", bob)
        _register(sql, env, "user", carol)
        grant_sql(env.db_url, f"entra:{ENTRA_TENANT_ID}:{carol}", "team_reader", team)
        refused = await client.get(
            f"{FOLDERS}/{team}", headers=_bearer(fake_issuer.mint_entra(bob))
        )
        admitted = await client.get(
            f"{FOLDERS}/{team}", headers=_bearer(fake_issuer.mint_entra(carol))
        )
    assert refused.status_code == 403, refused.text
    assert refused.json()["type"] == NOT_REGISTERED
    assert admitted.status_code == 200, admitted.text


async def test_a_member_of_a_registered_group_whose_grant_was_removed_reads_nothing(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    dave = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, _root, _project, team = await _group_world(
            env, client, admin, folder_chain, grant_sql, sql, "gc"
        )
        env.directory.add_principal("user", dave, groups=(group,))
        sql(env.db_url, "DELETE FROM role_assignments WHERE principal_id = %s", group)
        response = await client.get(
            f"{FOLDERS}/{team}", headers=_bearer(fake_issuer.mint_entra(dave))
        )
    assert response.status_code == 404, response.text


async def test_group_admission_is_tenant_scoped(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    bob, group2 = str(uuid.uuid4()), str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root_id(client, admin)
        _org, _project, team = await folder_chain(client, admin, root, "gd")
        _register(sql, env, "group", group2, tenant_id=OTHER_TENANT, owner=root)
        env.directory.add_principal("user", bob, groups=(group2,))
        response = await client.get(
            f"{FOLDERS}/{team}", headers=_bearer(fake_issuer.mint_entra(bob))
        )
    assert response.status_code == 403, response.text
    assert response.json()["type"] == NOT_REGISTERED


async def test_disabled_principal_is_401_and_a_directory_outage_is_503(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    alice = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, _root, _project, team = await _group_world(
            env, client, admin, folder_chain, grant_sql, sql, "ge"
        )
        env.directory.add_principal("user", alice, groups=(group,))
        headers = _bearer(fake_issuer.mint_entra(alice))
        ok = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.set_enabled(alice, False)
        disabled = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.set_enabled(alice, True)
        env.directory.fail = True
        down = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.fail = False
        removed_ok = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.remove(alice)
        gone = await client.get(f"{FOLDERS}/{team}", headers=headers)
    assert ok.status_code == 200, ok.text
    assert disabled.status_code == 401, disabled.text
    assert disabled.json()["type"] == "urn:dc:problem:unauthenticated"
    assert down.status_code == 503, down.text
    assert down.json()["type"] == "urn:dc:problem:service-unavailable"
    assert removed_ok.status_code == 200, removed_ok.text
    assert gone.status_code == 401, gone.text


async def test_a_directly_built_authorizer_without_a_gate_fails_closed_for_entra(
    make_identity: Callable[..., Any],
    sql: Sql,
) -> None:
    from dc_auth.principal import Principal
    from dc_core.db import make_async_engine, make_session_factory
    from dc_core.problems import ProblemError
    from dc_identity.authz import LocalAuthorizer

    env = make_identity()
    oid = str(uuid.uuid4())
    sql(env.db_url, "SELECT 1")
    principal = Principal(
        id=f"entra:{ENTRA_TENANT_ID}:{oid}",
        type="user",
        display="x",
        issuer="i",
        subject=oid,
        via="jwt",
        scopes=frozenset({"api"}),
    )
    engine = make_async_engine(env.db_url)
    try:
        authorizer = LocalAuthorizer(make_session_factory(engine))
        with pytest.raises(ProblemError) as checked:
            await authorizer.check(principal, "folder:list")
        with pytest.raises(ProblemError) as scoped:
            await authorizer.accessible_folders(principal, "folder:read")
    finally:
        await engine.dispose()
    for raised in (checked, scoped):
        assert raised.value.status == 503
        assert raised.value.slug == "service-unavailable"


async def test_grantees_for_requires_the_gate_to_have_resolved_first(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    sql: Sql,
) -> None:
    from dc_auth.principal import Principal
    from dc_core.db import make_async_engine, make_session_factory
    from dc_identity.access import grantees_for
    from dc_identity.gate import PrincipalGate

    env = make_identity()
    async with asgi_client(env.app) as client:
        await admin_token(client, env)  # bootstraps ROOT
    oid, group = str(uuid.uuid4()), str(uuid.uuid4())
    root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
    _register(sql, env, "group", group, owner=str(root))
    env.directory.add_principal("user", oid, groups=(group,))

    def make(id_: str, type_: str = "user", via: str = "jwt") -> Principal:
        return Principal.model_construct(
            id=id_,
            type=type_,
            display="x",
            issuer="i",
            subject="s",
            via=via,
            scopes=frozenset({"api"}),
            credential_id=None,
            on_behalf_of=None,
            username_hint=None,
        )

    entra = make(f"entra:{ENTRA_TENANT_ID}:{oid}")
    unresolved = [
        entra,
        make(f"sa:{uuid.uuid4()}", "service_account"),
        make(f"local:{uuid.uuid4()}", "user", "pat"),
        make("fake:u1", "user", "api_key"),
    ]
    for principal in unresolved:
        with pytest.raises(RuntimeError, match="PrincipalGate.resolve must run before"):
            grantees_for(principal)

    engine = make_async_engine(env.db_url)
    try:
        gate = PrincipalGate(make_session_factory(engine), env.directory)
        await gate.resolve(entra)
        grantees = grantees_for(entra)
    finally:
        await engine.dispose()
    assert (grantees.principal_type, grantees.principal_id) == ("user", entra.id)
    assert grantees.entra_groups == (group,)
    # local and generic-OIDC principals keep the unresolved fallback
    local = grantees_for(make(f"local:{uuid.uuid4()}"))
    assert local.entra_groups == ()
    assert grantees_for(make("fake:u1")).principal_id == "fake:u1"


# --- Plan 04: just-in-time records (D-58, D-73, D-75, D-76, D-81) ---------------------------------

SECRET_HINT = "alice.secret@corp.test"


async def _team_world(
    env: Any,
    client: Any,
    admin: str,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    prefix: str,
) -> tuple[str, str, str]:
    """Group G (owner ROOT) with team_reader on the team; returns (group, root, team)."""
    group, root, _project, team = await _group_world(
        env, client, admin, folder_chain, grant_sql, sql, prefix
    )
    return group, root, team


def _outbox(sql: Sql, env: Any, action: str) -> list[dict[str, Any]]:
    return sql(
        env.db_url,
        "SELECT aggregate_id, before, after, actor FROM outbox WHERE action = %s ORDER BY id",
        action,
    )


async def test_first_request_creates_one_entra_user_row_and_one_created_event(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    alice = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, _root, team = await _team_world(
            env, client, admin, folder_chain, grant_sql, sql, "ja"
        )
        env.directory.add_principal("user", alice, groups=(group,))
        token = fake_issuer.mint_entra(alice, name="Alice A", preferred_username=SECRET_HINT)
        first = await client.get(f"{FOLDERS}/{team}", headers=_bearer(token))
        second = await client.get(f"{FOLDERS}/{team}", headers=_bearer(token))
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    rows = sql(
        env.db_url,
        "SELECT tenant_id, object_id, display_name, preferred_username, is_active "
        "FROM entra_users WHERE object_id = %s",
        alice,
    )
    assert rows == [
        {
            "tenant_id": ENTRA_TENANT_ID,
            "object_id": alice,
            "display_name": "Alice A",
            "preferred_username": SECRET_HINT,
            "is_active": True,
        }
    ]
    created = _outbox(sql, env, "entra_user.created")
    assert len(created) == 1
    assert created[0]["before"] is None
    assert created[0]["after"]["preferred_username_set"] is True
    assert created[0]["after"]["display_name"] == "Alice A"
    assert "preferred_username" not in created[0]["after"]
    assert _outbox(sql, env, "entra_user.updated") == []
    leaked = sql(
        env.db_url,
        "SELECT count(*) AS n FROM outbox WHERE (coalesce(before::text, '') || "
        "coalesce(after::text, '') || actor::text) LIKE %s",
        f"%{SECRET_HINT}%",
    )
    assert leaked == [{"n": 0}]


async def test_twenty_concurrent_first_requests_make_one_entra_user_and_one_event(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    erin = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, _root, team = await _team_world(
            env, client, admin, folder_chain, grant_sql, sql, "jb"
        )
        env.directory.add_principal("user", erin, groups=(group,))
        headers = _bearer(fake_issuer.mint_entra(erin, name="Erin"))
        responses = await asyncio.gather(
            *(client.get(f"{FOLDERS}/{team}", headers=headers) for _ in range(20))
        )
    assert [r.status_code for r in responses] == [200] * 20
    assert sql(env.db_url, "SELECT count(*) AS n FROM entra_users WHERE object_id = %s", erin) == [
        {"n": 1}
    ]
    assert len(_outbox(sql, env, "entra_user.created")) == 1


async def test_a_later_token_refreshes_the_display_name_with_one_updated_event(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    alice = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, _root, team = await _team_world(
            env, client, admin, folder_chain, grant_sql, sql, "jc"
        )
        env.directory.add_principal("user", alice, groups=(group,))
        url = f"{FOLDERS}/{team}"
        await client.get(url, headers=_bearer(fake_issuer.mint_entra(alice, name="Alice A")))
        renamed = fake_issuer.mint_entra(alice, name="Alice Renamed")
        await client.get(url, headers=_bearer(renamed))
        await client.get(url, headers=_bearer(renamed))
        await client.get(url, headers=_bearer(fake_issuer.mint_entra(alice, name="Alice Renamed")))
    rows = sql(env.db_url, "SELECT display_name FROM entra_users WHERE object_id = %s", alice)
    assert rows == [{"display_name": "Alice Renamed"}]
    updated = _outbox(sql, env, "entra_user.updated")
    assert len(updated) == 1
    assert updated[0]["before"]["display_name"] == "Alice A"
    assert updated[0]["after"]["display_name"] == "Alice Renamed"
    assert len(_outbox(sql, env, "entra_user.created")) == 1


async def test_a_locally_deactivated_entra_user_is_401_although_the_directory_says_enabled(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    alice = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, _root, team = await _team_world(
            env, client, admin, folder_chain, grant_sql, sql, "jd"
        )
        env.directory.add_principal("user", alice, groups=(group,))
        headers = _bearer(fake_issuer.mint_entra(alice))
        before = await client.get(f"{FOLDERS}/{team}", headers=headers)
        sql(env.db_url, "UPDATE entra_users SET is_active = false WHERE object_id = %s", alice)
        after = await client.get(f"{FOLDERS}/{team}", headers=headers)
    assert before.status_code == 200, before.text
    assert after.status_code == 401, after.text
    assert after.json()["type"] == "urn:dc:problem:unauthenticated"


async def test_an_unregistered_user_never_gets_a_row_or_an_event(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    bob = str(uuid.uuid4())
    env.directory.add_principal("user", bob)
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        _org, _project, team = await folder_chain(
            client, admin, await _root_id(client, admin), "je"
        )
        response = await client.get(
            f"{FOLDERS}/{team}", headers=_bearer(fake_issuer.mint_entra(bob))
        )
    assert response.status_code == 403, response.text
    assert sql(env.db_url, "SELECT count(*) AS n FROM entra_users") == [{"n": 0}]
    assert _outbox(sql, env, "entra_user.created") == []


async def _jit_world(
    env: Any,
    client: Any,
    admin: str,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    prefix: str,
) -> tuple[str, str, str, str, str]:
    """GA (owner org A, registered first) and G0 (owner ROOT, later); both grant team_reader.

    Returns (ga, g0, org_a, root, team).
    """
    root = await _root_id(client, admin)
    org_a, _project, team = await folder_chain(client, admin, root, prefix)
    ga, g0 = str(uuid.uuid4()), str(uuid.uuid4())
    _register(sql, env, "group", ga, owner=org_a)
    _register(sql, env, "group", g0, owner=root)
    grant_sql(env.db_url, ga, "team_reader", team, "entra_group")
    return ga, g0, org_a, root, team


async def test_a_group_admitted_service_principal_gets_one_jit_row_owned_by_its_oldest_group(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    sp2 = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        ga, g0, org_a, _root, team = await _jit_world(
            env, client, admin, folder_chain, grant_sql, sql, "jf"
        )
        env.directory.add_principal(
            "service_principal", sp2, groups=(g0, ga), display_name="Pipeline SP2"
        )
        headers = _bearer(fake_issuer.mint_entra(sp2, token_type="app"))
        first = await client.get(f"{FOLDERS}/{team}", headers=headers)
        second = await client.get(f"{FOLDERS}/{team}", headers=headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    rows = sql(
        env.db_url,
        "SELECT kind, directly_registered, owner_folder_id, display_name, is_active "
        "FROM entra_registrations WHERE object_id = %s",
        sp2,
    )
    assert len(rows) == 1
    assert rows[0]["kind"] == "service_principal"
    assert rows[0]["directly_registered"] is False
    assert str(rows[0]["owner_folder_id"]) == org_a
    assert rows[0]["display_name"] == "Pipeline SP2"
    assert rows[0]["is_active"] is True
    created = _outbox(sql, env, "entra_registration.created")
    assert len(created) == 1
    assert created[0]["after"]["directly_registered"] is False


async def test_twenty_concurrent_first_requests_make_one_jit_service_principal_row(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    sp3 = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        ga, _g0, _org_a, _root, team = await _jit_world(
            env, client, admin, folder_chain, grant_sql, sql, "jg"
        )
        env.directory.add_principal("service_principal", sp3, groups=(ga,))
        headers = _bearer(fake_issuer.mint_entra(sp3, token_type="app"))
        responses = await asyncio.gather(
            *(client.get(f"{FOLDERS}/{team}", headers=headers) for _ in range(20))
        )
    assert [r.status_code for r in responses] == [200] * 20
    assert sql(
        env.db_url, "SELECT count(*) AS n FROM entra_registrations WHERE object_id = %s", sp3
    ) == [{"n": 1}]
    assert len(_outbox(sql, env, "entra_registration.created")) == 1


async def test_a_directly_registered_service_principal_gets_no_jit_row_or_event(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    sp1 = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root_id(client, admin)
        _org, _project, team = await folder_chain(client, admin, root, "jh")
        _register(sql, env, "service_principal", sp1, owner=root)
        grant_sql(
            env.db_url,
            f"entra:{ENTRA_TENANT_ID}:{sp1}",
            "team_reader",
            team,
            "service_principal",
        )
        response = await client.get(
            f"{FOLDERS}/{team}", headers=_bearer(fake_issuer.mint_entra(sp1, token_type="app"))
        )
    assert response.status_code == 200, response.text
    assert sql(
        env.db_url, "SELECT count(*) AS n FROM entra_registrations WHERE object_id = %s", sp1
    ) == [{"n": 1}]
    assert _outbox(sql, env, "entra_registration.created") == []


async def test_a_jit_service_principal_row_never_admits_once_no_registered_group_does(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    sp2 = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        ga, _g0, _org_a, _root, team = await _jit_world(
            env, client, admin, folder_chain, grant_sql, sql, "ji"
        )
        env.directory.add_principal("service_principal", sp2, groups=(ga,))
        headers = _bearer(fake_issuer.mint_entra(sp2, token_type="app"))
        admitted = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.set_groups(sp2, ())
        refused = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.set_groups(sp2, (ga,))
        restored = await client.get(f"{FOLDERS}/{team}", headers=headers)
        sql(
            env.db_url,
            "UPDATE entra_registrations SET is_active = false WHERE object_id = %s",
            sp2,
        )
        deactivated = await client.get(f"{FOLDERS}/{team}", headers=headers)
    assert admitted.status_code == 200, admitted.text
    assert refused.status_code == 403, refused.text
    assert refused.json()["type"] == NOT_REGISTERED
    assert sql(
        env.db_url, "SELECT count(*) AS n FROM entra_registrations WHERE object_id = %s", sp2
    ) == [{"n": 1}]
    assert restored.status_code == 200, restored.text
    # The owner can deactivate the service principal in-system although GA still admits it.
    assert deactivated.status_code == 401, deactivated.text
    assert deactivated.json()["type"] == "urn:dc:problem:unauthenticated"
