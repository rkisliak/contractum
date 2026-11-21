"""PDP rules through ``/internal/v1/authz/check`` for every role class (RBAC-03, RBAC-04, RBAC-06).

Each case builds a small tree and its role rows by SQL, then asks identity the way a service would:
with the caller's own token. The 404-versus-403 split follows ``visible_via`` (D-35).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from dc_auth.testing import FakeIssuer

pytestmark = pytest.mark.integration

CHECK = "/internal/v1/authz/check"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]


@dataclass
class Tree:
    root: str
    org: str  # O
    project: str  # O/P
    team: str  # O/P/T
    team2: str  # O/P/T2 (sibling team)
    org2: str  # O2
    team_other: str  # O2/P2/T3


class Db:
    def __init__(self, url: str, sql: Sql) -> None:
        self.url = url
        self._sql = sql

    def folder(self, kind: str, slug: str, parent: str | None, path: str) -> str:
        row = self._sql(
            self.url,
            "INSERT INTO folder (kind, slug, display_name, path, parent_id, created_by, "
            "updated_by) VALUES (%s, %s, %s, %s, %s, 'test', 'test') RETURNING id",
            kind,
            slug,
            slug,
            path,
            parent,
        )[0]
        return str(row["id"])

    def tree(self) -> Tree:
        root = str(self._sql(self.url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        org = self.folder("organization", "o1", root, "root.o1")
        project = self.folder("project", "p1", org, "root.o1.p1")
        team = self.folder("team", "t1", project, "root.o1.p1.t1")
        team2 = self.folder("team", "t2", project, "root.o1.p1.t2")
        org2 = self.folder("organization", "o2", root, "root.o2")
        project2 = self.folder("project", "p2", org2, "root.o2.p2")
        team_other = self.folder("team", "t3", project2, "root.o2.p2.t3")
        return Tree(root, org, project, team, team2, org2, team_other)

    def grant(self, principal_id: str, role: str, folder_id: str, ptype: str = "user") -> None:
        self._sql(
            self.url,
            "INSERT INTO role_assignments (folder_id, principal_type, principal_id, role, "
            "created_by, updated_by) VALUES (%s, %s, %s, %s, 'test', 'test')",
            folder_id,
            ptype,
            principal_id,
            role,
        )

    def block(self, folder_id: str) -> None:
        self._sql(self.url, "UPDATE folder SET block_inheritance = true WHERE id = %s", folder_id)

    def group(self, owner: str, name: str, members: list[str]) -> str:
        group_id = str(
            self._sql(
                self.url,
                "INSERT INTO local_groups (owner_folder_id, name, created_by, updated_by) "
                "VALUES (%s, %s, 'test', 'test') RETURNING id",
                owner,
                name,
            )[0]["id"]
        )
        for member in members:
            self._sql(
                self.url,
                "INSERT INTO group_members (group_id, member_type, member_id, created_by, "
                "updated_by) VALUES (%s, 'user', %s, 'test', 'test')",
                group_id,
                member,
            )
        return group_id


async def _check(
    client: Any, token: str, permission: str, folder_id: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"permission": permission}
    if folder_id is not None:
        body["folder_id"] = folder_id
    response = await client.post(CHECK, json=body, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    decision: dict[str, Any] = response.json()
    return decision


async def _outcome(client: Any, token: str, permission: str, folder_id: str | None = None) -> str:
    return str((await _check(client, token, permission, folder_id))["outcome"])


@pytest.fixture
def stage(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
) -> Callable[[], Any]:
    """``async with stage() as (client, db, tree, admin, token_of)``."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _stage() -> Any:
        env = make_identity()
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            db = Db(env.db_url, sql)
            yield client, db, db.tree(), admin, fake_issuer.mint

    return _stage


async def test_global_admin_is_exempt_from_a_block_at_the_organization(stage: Any) -> None:
    async with stage() as (client, db, tree, admin, _mint):
        db.block(tree.org)
        for permission in ("folder:read", "contract:create", "contract:read"):
            assert await _outcome(client, admin, permission, tree.team) == "allow"


async def test_a_global_reader_is_cut_at_a_blocked_organization_but_not_elsewhere(
    stage: Any,
) -> None:
    async with stage() as (client, db, tree, _admin, mint):
        db.grant("fake:gr", "global_reader", tree.root)
        db.block(tree.org)
        token = mint("gr")
        assert await _outcome(client, token, "folder:read", tree.root) == "allow"
        assert await _outcome(client, token, "folder:read", tree.org) == "not_found"
        assert await _outcome(client, token, "contract:read", tree.team) == "not_found"
        assert await _outcome(client, token, "folder:read", tree.team_other) == "allow"
        assert await _outcome(client, token, "contract:read", tree.team_other) == "allow"


async def test_a_consumer_at_root_is_cut_at_a_blocked_team(stage: Any) -> None:
    async with stage() as (client, db, tree, _admin, mint):
        db.grant("fake:cs", "consumer", tree.root)
        db.block(tree.team)
        token = mint("cs")
        assert await _outcome(client, token, "contract:read-published", tree.team) == "not_found"
        assert await _outcome(client, token, "contract:read-published", tree.team2) == "allow"
        # a consumer never holds content or write permissions anywhere
        assert await _outcome(client, token, "contract:read", tree.team2) == "not_found"
        assert await _outcome(client, token, "contract:create", tree.team2) == "forbidden"


async def test_set_block_is_evaluated_at_the_parent_folder(stage: Any) -> None:
    async with stage() as (client, db, tree, _admin, mint):
        db.grant("fake:oa", "org_admin", tree.org)
        db.grant("fake:ta", "team_admin", tree.team)
        org_admin, team_admin = mint("oa"), mint("ta")
        # an Org admin may flip the flag on a Team below
        decision = await _check(client, org_admin, "folder:set-block", tree.team)
        assert decision["outcome"] == "allow"
        # a Team admin can read their team, so they get 403 (not 404) on their own flag
        assert await _outcome(client, team_admin, "folder:set-block", tree.team) == "forbidden"
        # an Org admin cannot flip the flag on their own org: that is decided at ROOT
        assert await _outcome(client, org_admin, "folder:set-block", tree.org) == "forbidden"
        # nobody in another organization even sees the folder
        assert await _outcome(client, org_admin, "folder:set-block", tree.team_other) == "not_found"


async def test_an_unknown_permission_is_forbidden_even_for_the_global_admin(stage: Any) -> None:
    async with stage() as (client, _db, tree, admin, mint):
        for token in (admin, mint("stranger")):
            assert await _outcome(client, token, "contract:search") == "forbidden"
            assert await _outcome(client, token, "contract:search", tree.team) == "forbidden"


async def test_a_root_target_permission_needs_a_role_at_root(stage: Any) -> None:
    async with stage() as (client, db, tree, admin, mint):
        db.grant("fake:ta", "team_admin", tree.team)
        assert await _outcome(client, mint("ta"), "audit:read") == "forbidden"
        assert await _outcome(client, mint("stranger"), "audit:read") == "forbidden"
        decision = await _check(client, admin, "audit:read")
        assert decision["outcome"] == "allow"
        assert "audit:read" in decision["permissions"]


async def test_a_restricted_bootstrap_session_holds_no_authority(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    bootstrap_token: Callable[[Any, Any], Awaitable[str]],
    fresh_db_url: str,
    sql: Sql,
) -> None:
    from dc_auth.authorizer import Outcome
    from dc_auth.principal import Principal
    from dc_core.db import make_async_engine, make_session_factory
    from dc_identity.authz import LocalAuthorizer

    env = make_identity()
    async with asgi_client(env.app) as client:
        restricted = await bootstrap_token(client, env)
        tree = Db(fresh_db_url, sql).tree()
        response = await client.post(
            CHECK,
            json={"permission": "folder:read", "folder_id": tree.team},
            headers={"Authorization": f"Bearer {restricted}"},
        )
        assert response.status_code == 403  # the wire refuses it before any decision
        admin_row = sql(fresh_db_url, "SELECT id FROM users")[0]
        engine = make_async_engine(fresh_db_url)
        try:
            principal = Principal(
                id=f"local:{admin_row['id']}",
                type="user",
                display="admin",
                issuer="x",
                subject="admin",
                via="jwt",
                scopes=frozenset({"password:rotate"}),
            )
            decision = await LocalAuthorizer(make_session_factory(engine)).check(
                principal, "folder:read", uuid.UUID(tree.team)
            )
        finally:
            await engine.dispose()
    assert decision.outcome is Outcome.FORBIDDEN


async def test_a_grant_held_through_a_local_group_applies_to_its_members(stage: Any) -> None:
    async with stage() as (client, db, tree, _admin, mint):
        group = db.group(tree.root, "platform-editors", ["fake:member"])
        db.grant(group, "team_editor", tree.team, ptype="local_group")
        member, outsider = mint("member"), mint("outsider")
        assert await _outcome(client, member, "contract:create", tree.team) == "allow"
        assert await _outcome(client, member, "contract:create", tree.team2) == "not_found"
        assert await _outcome(client, outsider, "contract:create", tree.team) == "not_found"


async def test_roles_from_different_sources_union_their_permissions(stage: Any) -> None:
    async with stage() as (client, db, tree, _admin, mint):
        db.grant("fake:both", "policy_manager", tree.team)
        db.grant("fake:both", "team_reader", tree.team)
        db.grant("fake:pm", "policy_manager", tree.team)
        token, pm_only = mint("both"), mint("pm")
        decision = await _check(client, token, "contract:read", tree.team)
        assert decision["outcome"] == "allow"
        assert {"contract:read", "contract:read-metadata", "template:publish"} <= set(
            decision["permissions"]
        )
        # a Policy Manager alone never reads content, but does read its metadata permission
        assert await _outcome(client, pm_only, "contract:read", tree.team) == "not_found"
        assert await _outcome(client, pm_only, "contract:read-metadata", tree.team) == "allow"
