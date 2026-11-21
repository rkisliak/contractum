"""Entra groups, local groups, the directory cache window and Graph outages (D-60 to D-63, AUTH-04).

Every test that needs the cache opts in with ``make_identity(directory_cache_ttl_s=1)``; the
fixture default is no cache so other tests keep immediate answers.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer
from dc_identity.directory.cache import DirectoryCache

pytestmark = pytest.mark.integration

FOLDERS = "/api/v1/identity/folders"
WINDOW_S = 1.1
AdminFlow = Callable[[Any, Any], Awaitable[str]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
GrantSql = Callable[..., None]
Sql = Callable[..., list[dict[str, Any]]]
CreateUser = Callable[..., Awaitable[Any]]
Login = Callable[..., Awaitable[str]]
Grant = Callable[..., Awaitable[Any]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(
    sql: Sql,
    env: Any,
    kind: str,
    object_id: str,
    *,
    owner: str | None = None,
    groups: tuple[str, ...] = (),
) -> None:
    if kind == "group":
        env.directory.add_group(object_id, f"group {object_id[:8]}")
    else:
        env.directory.add_principal(kind, object_id, groups=groups)
    sql(
        env.db_url,
        "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
        "display_name, created_by, updated_by) VALUES (%s, %s, %s, %s, %s, 'test', 'test')",
        kind,
        ENTRA_TENANT_ID,
        object_id,
        owner,
        f"{kind} {object_id[:8]}",
    )


async def _world(
    env: Any,
    client: Any,
    admin: str,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    prefix: str,
    role: str = "team_reader",
) -> tuple[str, str, str]:
    """Registered group G (owner ROOT) granted ``role`` on the team; returns (G, root, team)."""
    listed = await client.get(FOLDERS, headers=_bearer(admin))
    root = str(next(f["id"] for f in listed.json()["items"] if f["kind"] == "root"))
    _org, _project, team = await folder_chain(client, admin, root, prefix)
    group = str(uuid.uuid4())
    _register(sql, env, "group", group, owner=root)
    grant_sql(env.db_url, group, role, team, "entra_group")
    return group, root, team


async def test_the_fixture_default_has_no_cache_and_a_disable_is_immediate(
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
        group, _root, team = await _world(env, client, admin, folder_chain, grant_sql, sql, "fa")
        env.directory.add_principal("user", alice, groups=(group,))
        headers = _bearer(fake_issuer.mint_entra(alice))
        before = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.set_enabled(alice, False)
        after = await client.get(f"{FOLDERS}/{team}", headers=headers)
    assert env.app.state.directory is env.directory
    assert before.status_code == 200, before.text
    assert after.status_code == 401, after.text


async def test_opting_in_wraps_the_fake_in_a_cache_and_the_env_keeps_the_fake(
    make_identity: Callable[..., Any],
) -> None:
    env = make_identity(directory_cache_ttl_s=1)
    assert isinstance(env.app.state.directory, DirectoryCache)
    assert not isinstance(env.directory, DirectoryCache)


async def test_a_disable_takes_effect_after_the_cache_window_not_before(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity(directory_cache_ttl_s=1)
    alice = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, _root, team = await _world(env, client, admin, folder_chain, grant_sql, sql, "fb")
        env.directory.add_principal("user", alice, groups=(group,))
        headers = _bearer(fake_issuer.mint_entra(alice))
        first = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.set_enabled(alice, False)
        cached = await client.get(f"{FOLDERS}/{team}", headers=headers)
        await asyncio.sleep(WINDOW_S)
        disabled = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.set_enabled(alice, True)
        env.directory.remove(alice)
        await asyncio.sleep(WINDOW_S)
        deleted = await client.get(f"{FOLDERS}/{team}", headers=headers)
    assert first.status_code == 200, first.text
    assert cached.status_code == 200, cached.text
    assert disabled.status_code == 401, disabled.text
    assert deleted.status_code == 401, deleted.text


async def test_graph_down_is_served_from_the_cache_then_503_after_the_window(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity(directory_cache_ttl_s=1)
    alice = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, _root, team = await _world(env, client, admin, folder_chain, grant_sql, sql, "fc")
        env.directory.add_principal("user", alice, groups=(group,))
        headers = _bearer(fake_issuer.mint_entra(alice))
        warm = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.fail = True
        inside = await client.get(f"{FOLDERS}/{team}", headers=headers)
        await asyncio.sleep(WINDOW_S)
        expired = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.fail = False
        recovered = await client.get(f"{FOLDERS}/{team}", headers=headers)
    assert warm.status_code == 200, warm.text
    assert inside.status_code == 200, inside.text
    assert expired.status_code == 503, expired.text
    assert expired.json()["type"] == "urn:dc:problem:service-unavailable"
    assert recovered.status_code == 200, recovered.text


async def test_removal_from_the_group_loses_access_after_the_window(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity(directory_cache_ttl_s=1)
    alice = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, _root, team = await _world(env, client, admin, folder_chain, grant_sql, sql, "fd")
        env.directory.add_principal("user", alice, groups=(group,))
        headers = _bearer(fake_issuer.mint_entra(alice))
        before = await client.get(f"{FOLDERS}/{team}", headers=headers)
        env.directory.set_groups(alice, set())
        await asyncio.sleep(WINDOW_S)
        after = await client.get(f"{FOLDERS}/{team}", headers=headers)
    assert before.status_code == 200, before.text
    assert after.status_code == 403, after.text
    assert after.json()["type"] == "urn:dc:problem:principal-not-registered"


async def test_the_same_role_from_an_entra_group_and_a_local_group_is_held_once(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity(directory_cache_ttl_s=1)
    alice = str(uuid.uuid4())
    alice_id = f"entra:{ENTRA_TENANT_ID}:{alice}"
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, root, team = await _world(
            env, client, admin, folder_chain, grant_sql, sql, "fe", role="team_editor"
        )
        _register(sql, env, "user", alice, groups=(group,))  # admitted directly as well
        local = str(
            sql(
                env.db_url,
                "INSERT INTO local_groups (owner_folder_id, name, created_by, updated_by) "
                "VALUES (%s, 'adjacent', 'test', 'test') RETURNING id",
                root,
            )[0]["id"]
        )
        grant_sql(env.db_url, local, "team_editor", team, "local_group")
        sql(
            env.db_url,
            "INSERT INTO group_members (group_id, member_type, member_id, created_by, updated_by)"
            " VALUES (%s, 'user', %s, 'test', 'test')",
            local,
            alice_id,
        )
        headers = _bearer(fake_issuer.mint_entra(alice))
        url = f"{FOLDERS}/{team}/effective-access"

        both = await client.get(url, headers=headers)

        env.directory.set_groups(alice, set())  # the Entra source goes away
        await asyncio.sleep(WINDOW_S)
        via_local = await client.get(url, headers=headers)

        env.directory.set_groups(alice, (group,))  # Entra again, the local source goes away
        sql(env.db_url, "DELETE FROM group_members WHERE member_id = %s", alice_id)
        await asyncio.sleep(WINDOW_S)
        via_entra = await client.get(url, headers=headers)

        env.directory.set_groups(alice, set())  # neither source is left
        await asyncio.sleep(WINDOW_S)
        neither = await client.get(url, headers=headers)
    assert both.status_code == 200, both.text
    assert both.json()["roles"] == ["team_editor"]
    assert via_local.json()["roles"] == ["team_editor"]
    assert via_entra.json()["roles"] == ["team_editor"]
    assert neither.status_code == 404, neither.text


async def test_a_directly_registered_user_with_no_groups_holds_only_its_direct_grant(
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
        _group, root, team = await _world(env, client, admin, folder_chain, grant_sql, sql, "ff")
        _register(sql, env, "user", carol, groups=())
        grant_sql(env.db_url, f"entra:{ENTRA_TENANT_ID}:{carol}", "team_reader", team)
        sibling = (await folder_chain(client, admin, root, "ff2"))[2]
        headers = _bearer(fake_issuer.mint_entra(carol))
        direct = await client.get(f"{FOLDERS}/{team}/effective-access", headers=headers)
        elsewhere = await client.get(f"{FOLDERS}/{sibling}", headers=headers)
    assert direct.status_code == 200, direct.text
    assert direct.json()["roles"] == ["team_reader"]
    assert elsewhere.status_code == 404, elsewhere.text


async def test_token_groups_and_overage_claims_are_never_a_source_of_membership(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    mallory = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        group, _root, team = await _world(env, client, admin, folder_chain, grant_sql, sql, "fg")
        env.directory.add_principal("user", mallory, groups=())  # the directory says: no groups
        token = fake_issuer.mint_entra(
            mallory,
            extra_claims={
                "groups": [group],
                "_claim_names": {"groups": "src1"},
                "_claim_sources": {"src1": {"endpoint": "https://graph.example.test/x"}},
            },
        )
        response = await client.get(f"{FOLDERS}/{team}", headers=_bearer(token))
    assert response.status_code == 403, response.text
    assert response.json()["type"] == "urn:dc:problem:principal-not-registered"
    assert all(call[0] == "lookup" for call in env.directory.calls)


# --- Plan 06 Task 1: grants to Entra groups and Entra users (D-64, D-59, D-77, D-32, D-33) ------


def _slug(resp: Any) -> str:
    return str(resp.json()["type"]).rsplit(":", 1)[-1]


def _assignments(env: Any, sql: Sql, principal_id: str) -> list[dict[str, Any]]:
    return sql(
        env.db_url,
        "SELECT id FROM role_assignments WHERE principal_id = %s",
        principal_id,
    )


async def _org_chain(
    client: Any, admin: str, folder_chain: FolderChain, root: str, prefix: str
) -> tuple[str, str]:
    org, _project, team = await folder_chain(client, admin, root, prefix)
    return org, team


async def _root_of(client: Any, admin: str) -> str:
    listed = await client.get(FOLDERS, headers=_bearer(admin))
    return str(next(f["id"] for f in listed.json()["items"] if f["kind"] == "root"))


async def test_a_registered_entra_group_can_be_granted_a_role_and_its_member_holds_it(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant: Grant,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    alice = str(uuid.uuid4())
    group = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root_of(client, admin)
        _org, team = await _org_chain(client, admin, folder_chain, root, "ga")
        _register(sql, env, "group", group, owner=root)
        env.directory.add_principal("user", alice, groups=(group,))
        created = await grant(client, admin, team, group, "team_editor", "entra_group")
        effective = await client.get(
            f"{FOLDERS}/{team}/effective-access", headers=_bearer(fake_issuer.mint_entra(alice))
        )
    assert created.status_code == 201, created.text
    assert created.json()["principal_type"] == "entra_group"
    assert created.json()["principal_id"] == group
    assert effective.status_code == 200, effective.text
    assert effective.json()["roles"] == ["team_editor"]


async def test_a_guid_the_directory_no_longer_knows_is_422_and_writes_nothing(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant: Grant,
    sql: Sql,
) -> None:
    env = make_identity()
    group = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root_of(client, admin)
        _org, team = await _org_chain(client, admin, folder_chain, root, "gb")
        # Registered in the database, but the fake directory has never heard of it.
        sql(
            env.db_url,
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
            "display_name, created_by, updated_by) VALUES ('group', %s, %s, %s, 'gone', 't', 't')",
            ENTRA_TENANT_ID,
            group,
            root,
        )
        resp = await grant(client, admin, team, group, "team_reader", "entra_group")
    assert resp.status_code == 422, resp.text
    assert _slug(resp) == "unknown-principal"
    assert _assignments(env, sql, group) == []


async def test_a_graph_outage_refuses_the_grant_with_503_and_writes_nothing(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant: Grant,
    sql: Sql,
) -> None:
    env = make_identity()
    group = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root_of(client, admin)
        _org, team = await _org_chain(client, admin, folder_chain, root, "gc")
        _register(sql, env, "group", group, owner=root)
        env.directory.fail = True
        resp = await grant(client, admin, team, group, "team_reader", "entra_group")
    assert resp.status_code == 503, resp.text
    assert _slug(resp) == "service-unavailable"
    assert _assignments(env, sql, group) == []


async def test_an_unregistered_guid_is_404_and_graph_is_never_asked(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant: Grant,
    sql: Sql,
) -> None:
    env = make_identity()
    typo = str(uuid.uuid4())
    env.directory.add_group(typo, "exists in Entra but was never registered")
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root_of(client, admin)
        _org, team = await _org_chain(client, admin, folder_chain, root, "gd")
        resp = await grant(client, admin, team, typo, "team_reader", "entra_group")
    assert resp.status_code == 404, resp.text
    assert not [call for call in env.directory.calls if call[0] == "group"]
    assert _assignments(env, sql, typo) == []


async def test_an_org_owned_group_stays_in_its_org_except_for_a_global_admin(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    grant: Grant,
    create_user: CreateUser,
    login: Login,
    sql: Sql,
) -> None:
    env = make_identity()
    group = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root_of(client, admin)
        org_a, team_a = await _org_chain(client, admin, folder_chain, root, "ha")
        org_b, team_b = await _org_chain(client, admin, folder_chain, root, "hb")
        _register(sql, env, "group", group, owner=org_a)
        ob = await create_user(client, admin, "ob-admin")
        grant_sql(env.db_url, ob.principal_id, "org_admin", org_b)
        ob_token = await login(client, ob.username, ob.password)
        both = await create_user(client, admin, "ab-admin")
        grant_sql(env.db_url, both.principal_id, "org_admin", org_a)
        grant_sql(env.db_url, both.principal_id, "org_admin", org_b)
        both_token = await login(client, both.username, both.password)
        inside = await grant(client, admin, team_a, group, "team_reader", "entra_group")
        by_two_org_admin = await grant(
            client, both_token, team_b, group, "team_reader", "entra_group"
        )
        by_other_org = await grant(client, ob_token, team_b, group, "team_reader", "entra_group")
        by_global = await grant(client, admin, team_b, group, "team_reader", "entra_group")
    assert inside.status_code == 201, inside.text
    # An Org admin of both organizations still cannot carry an org_a group into org_b.
    assert by_two_org_admin.status_code == 422, by_two_org_admin.text
    assert _slug(by_two_org_admin) == "group-scope-mismatch"
    assert by_other_org.status_code == 404, by_other_org.text
    # A Global admin may grant it on any folder.
    assert by_global.status_code == 201, by_global.text


async def test_a_directly_registered_entra_user_can_be_granted_by_id(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant: Grant,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    frank = str(uuid.uuid4())
    frank_id = f"entra:{ENTRA_TENANT_ID}:{frank}"
    unknown = f"entra:{ENTRA_TENANT_ID}:{uuid.uuid4()}"
    env.directory.add_principal("user", frank)
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root_of(client, admin)
        _org, team = await _org_chain(client, admin, folder_chain, root, "ia")
        registered = await client.post(
            "/api/v1/identity/entra/users",
            json={"object_id": frank},
            headers=_bearer(admin),
        )
        created = await grant(client, admin, team, frank_id, "team_reader")
        refused = await grant(client, admin, team, unknown, "team_reader")
        read = await client.get(f"{FOLDERS}/{team}", headers=_bearer(fake_issuer.mint_entra(frank)))
    assert registered.status_code == 201, registered.text
    assert created.status_code == 201, created.text
    assert refused.status_code == 422, refused.text
    assert _slug(refused) == "unknown-principal"
    assert read.status_code == 200, read.text


async def test_admission_through_an_org_owned_group_does_not_confine_other_grants(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    dana = str(uuid.uuid4())
    group = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root_of(client, admin)
        org_a, _team_a = await _org_chain(client, admin, folder_chain, root, "ja")
        _org_b, team_b = await _org_chain(client, admin, folder_chain, root, "jb")
        _register(sql, env, "group", group, owner=org_a)
        env.directory.add_principal("user", dana, groups=(group,))
        grant_sql(env.db_url, f"entra:{ENTRA_TENANT_ID}:{dana}", "global_reader", root)
        headers = _bearer(fake_issuer.mint_entra(dana))
        read = await client.get(f"{FOLDERS}/{team_b}", headers=headers)
    assert read.status_code == 200, read.text


async def test_an_entra_derived_org_admin_is_bound_by_the_ceiling_and_the_self_grant_guard(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    grant_sql: GrantSql,
    grant: Grant,
    create_user: CreateUser,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    alice = str(uuid.uuid4())
    group = str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = await _root_of(client, admin)
        org_a, team_a = await _org_chain(client, admin, folder_chain, root, "ka")
        _register(sql, env, "group", group, owner=org_a)
        grant_sql(env.db_url, group, "org_admin", org_a, "entra_group")
        env.directory.add_principal("user", alice, groups=(group,))
        target = await create_user(client, admin, "target")
        token = fake_issuer.mint_entra(alice)
        below = await grant(client, token, team_a, target.principal_id, "team_editor")
        at_ceiling = await grant(client, token, org_a, target.principal_id, "org_admin")
        to_own_group = await grant(client, token, team_a, group, "team_editor", "entra_group")
    assert below.status_code == 201, below.text
    assert at_ceiling.status_code == 403, at_ceiling.text
    assert _slug(at_ceiling) == "grant-ceiling-exceeded"
    assert to_own_group.status_code == 403, to_own_group.text
    assert _slug(to_own_group) == "self-grant-forbidden"
