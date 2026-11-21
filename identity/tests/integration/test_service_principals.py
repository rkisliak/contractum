"""A pipeline runs as an Entra service principal with exactly its grants (AUTH-05, D-73, D-74).

A service principal presents a client-credentials token (``token_type="app"``). It is admitted only
when registered directly or when a transitive member of a registered Entra group, holds nothing
until granted, gets exactly what its direct or group grants allow, and loses everything the moment
the directory disables it. An Organization-owned service principal is grantable only inside its
Organization (D-59, D-77).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
SERVICE_PRINCIPALS = f"{IDENTITY}/entra/service-principals"
Sql = Callable[..., list[dict[str, Any]]]
AdminFlow = Callable[[Any, Any], Awaitable[str]]
FolderChain = Callable[..., Awaitable[tuple[str, str, str]]]
CreateUser = Callable[..., Awaitable[Any]]
Login = Callable[..., Awaitable[str]]
GrantSql = Callable[..., None]
Grant = Callable[..., Awaitable[Any]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _slug(resp: Any) -> str:
    return str(resp.json()["type"]).rsplit(":", 1)[-1]


def _sp_id(object_id: str) -> str:
    return f"entra:{ENTRA_TENANT_ID}:{object_id}"


@dataclass
class Pipeline:
    env: Any
    client: Any
    admin: str
    root: str
    org_a: str
    team_a: str
    org_b: str
    team_b: str
    ob: str  # Organization admin of B (token)
    issuer: FakeIssuer
    sql: Sql
    grant_sql: GrantSql

    def sp_token(self, object_id: str) -> str:
        return self.issuer.mint_entra(object_id, token_type="app")  # noqa: S106

    async def get_folder(self, folder: str, object_id: str) -> Any:
        return await self.client.get(
            f"{FOLDERS}/{folder}", headers=_bearer(self.sp_token(object_id))
        )

    async def register(self, object_id: str, owner: str | None = None) -> Any:
        return await self.client.post(
            SERVICE_PRINCIPALS,
            json={"owner_folder_id": owner or self.root, "object_id": object_id},
            headers=_bearer(self.admin),
        )

    def register_group(self, owner: str) -> str:
        group = str(uuid.uuid4())
        self.env.directory.add_group(group, "pipelines")
        self.sql(
            self.env.db_url,
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
            "display_name, created_by, updated_by) VALUES ('group', %s, %s, %s, 'pipelines', "
            "'test', 'test')",
            ENTRA_TENANT_ID,
            group,
            owner,
        )
        return group


@pytest.fixture
def open_pipeline(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: FolderChain,
    create_user: CreateUser,
    login: Login,
    grant_sql: GrantSql,
    sql: Sql,
    fake_issuer: FakeIssuer,
) -> Callable[[], Any]:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _open() -> Any:
        env = make_identity()
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
            org_a, _project_a, team_a = await folder_chain(client, admin, root, "pa")
            org_b, _project_b, team_b = await folder_chain(client, admin, root, "pb")
            ob = await create_user(client, admin, "pb-admin")
            grant_sql(env.db_url, ob.principal_id, "org_admin", org_b)
            yield Pipeline(
                env=env,
                client=client,
                admin=admin,
                root=root,
                org_a=org_a,
                team_a=team_a,
                org_b=org_b,
                team_b=team_b,
                ob=await login(client, ob.username, ob.password),
                issuer=fake_issuer,
                sql=sql,
                grant_sql=grant_sql,
            )

    return _open


async def test_an_unregistered_service_principal_is_403_and_a_registered_one_holds_nothing(
    open_pipeline: Callable[[], Any],
) -> None:
    async with open_pipeline() as p:
        sp1 = str(uuid.uuid4())
        p.env.directory.add_principal("service_principal", sp1)
        unregistered = await p.get_folder(p.team_a, sp1)
        registered = await p.register(sp1)
        no_grant = await p.get_folder(p.team_a, sp1)
    assert unregistered.status_code == 403, unregistered.text
    assert _slug(unregistered) == "principal-not-registered"
    assert registered.status_code == 201, registered.text
    assert no_grant.status_code == 404, no_grant.text


async def test_a_direct_grant_to_a_registered_service_principal_gives_exactly_that_access(
    open_pipeline: Callable[[], Any], grant: Grant
) -> None:
    async with open_pipeline() as p:
        sp1 = str(uuid.uuid4())
        p.env.directory.add_principal("service_principal", sp1)
        assert (await p.register(sp1)).status_code == 201
        created = await grant(
            p.client, p.admin, p.team_a, _sp_id(sp1), "consumer", "service_principal"
        )
        granted = await p.get_folder(p.team_a, sp1)
        elsewhere = await p.get_folder(p.team_b, sp1)
    assert created.status_code == 201, created.text
    assert created.json()["principal_type"] == "service_principal"
    assert granted.status_code == 200, granted.text
    assert elsewhere.status_code == 404, elsewhere.text


async def test_a_group_member_service_principal_gets_the_groups_grant(
    open_pipeline: Callable[[], Any],
) -> None:
    async with open_pipeline() as p:
        group = p.register_group(p.root)
        p.grant_sql(p.env.db_url, group, "team_reader", p.team_a, "entra_group")
        sp2 = str(uuid.uuid4())
        p.env.directory.add_principal("service_principal", sp2, groups=(group,))
        granted = await p.get_folder(p.team_a, sp2)
        elsewhere = await p.get_folder(p.team_b, sp2)
    assert granted.status_code == 200, granted.text
    assert elsewhere.status_code == 404, elsewhere.text


async def test_a_just_in_time_row_is_grantable_inside_its_owners_organization_only(
    open_pipeline: Callable[[], Any], grant: Grant
) -> None:
    async with open_pipeline() as p:
        group = p.register_group(p.org_a)
        p.grant_sql(p.env.db_url, group, "team_reader", p.team_a, "entra_group")
        sp2 = str(uuid.uuid4())
        p.env.directory.add_principal("service_principal", sp2, groups=(group,))
        admitted = await p.get_folder(p.team_a, sp2)  # creates the just-in-time row
        inside = await grant(
            p.client, p.admin, p.team_a, _sp_id(sp2), "consumer", "service_principal"
        )
        by_global = await grant(
            p.client, p.admin, p.team_b, _sp_id(sp2), "consumer", "service_principal"
        )
        by_other_org = await grant(
            p.client, p.ob, p.team_b, _sp_id(sp2), "consumer", "service_principal"
        )
        rows = p.sql(
            p.env.db_url,
            "SELECT directly_registered FROM entra_registrations WHERE object_id = %s",
            sp2,
        )
    assert admitted.status_code == 200, admitted.text
    assert rows == [{"directly_registered": False}]
    assert inside.status_code == 201, inside.text
    # A Global admin may grant it outside its owner Organization; the other org's admin may not.
    assert by_global.status_code == 201, by_global.text
    assert by_other_org.status_code == 404, by_other_org.text


async def test_a_global_admin_grants_an_organization_owned_registration_anywhere(
    open_pipeline: Callable[[], Any], grant: Grant
) -> None:
    async with open_pipeline() as p:
        sp1 = str(uuid.uuid4())
        p.env.directory.add_principal("service_principal", sp1)
        assert (await p.register(sp1, owner=p.org_a)).status_code == 201
        inside = await grant(
            p.client, p.admin, p.team_a, _sp_id(sp1), "consumer", "service_principal"
        )
        outside = await grant(
            p.client, p.admin, p.team_b, _sp_id(sp1), "consumer", "service_principal"
        )
    assert inside.status_code == 201, inside.text
    # A Global admin is not confined by the owner (quick 261119).
    assert outside.status_code == 201, outside.text


async def test_a_service_principal_grant_naming_an_id_with_no_active_row_is_404(
    open_pipeline: Callable[[], Any], grant: Grant
) -> None:
    async with open_pipeline() as p:
        stranger = str(uuid.uuid4())
        p.env.directory.add_principal("service_principal", stranger)
        resp = await grant(
            p.client, p.admin, p.team_a, _sp_id(stranger), "consumer", "service_principal"
        )
        stored = p.sql(
            p.env.db_url,
            "SELECT id FROM role_assignments WHERE principal_id = %s",
            _sp_id(stranger),
        )
    assert resp.status_code == 404, resp.text
    assert stored == []


async def test_disabling_the_service_principal_in_entra_is_401_on_the_next_request(
    open_pipeline: Callable[[], Any], grant: Grant
) -> None:
    async with open_pipeline() as p:
        sp1 = str(uuid.uuid4())
        p.env.directory.add_principal("service_principal", sp1)
        assert (await p.register(sp1)).status_code == 201
        await grant(p.client, p.admin, p.team_a, _sp_id(sp1), "consumer", "service_principal")
        before = await p.get_folder(p.team_a, sp1)
        p.env.directory.set_enabled(sp1, False)
        after = await p.get_folder(p.team_a, sp1)
    assert before.status_code == 200, before.text
    assert after.status_code == 401, after.text
