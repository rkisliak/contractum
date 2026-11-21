"""Identity as token issuer and policy decision point for other services (D-05, D-08, D-35)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dc_auth.authorizer import AuthorizerUnavailable, Outcome
from dc_auth.issuers import IssuerConfig, IssuerRegistry
from dc_auth.jwks import JwksCache
from dc_auth.principal import Principal
from dc_auth.remote import RemoteAuthorizer
from dc_auth.testing import FakeIssuer
from dc_auth.verify import TokenVerifier
from pydantic import SecretStr

pytestmark = pytest.mark.integration

LOCAL_ISSUER = "http://identity:8000"
CHECK = "/internal/v1/authz/check"
FOLDERS = "/internal/v1/authz/accessible-folders"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]

_ANYONE = Principal(
    id="x:anyone", type="user", display="anyone", issuer="x", subject="anyone", via="jwt"
)


@asynccontextmanager
async def _remote(app: Any, token: str) -> AsyncIterator[RemoteAuthorizer]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        yield RemoteAuthorizer(
            http, base_url="http://identity", bearer=SecretStr(token), timeout_s=2.0
        )


def _root_id(sql: Sql, env: Any) -> uuid.UUID:
    row = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]
    root_id: uuid.UUID = row["id"]
    return root_id


async def test_jwks_endpoint_serves_the_public_key_and_a_verifier_accepts_identity_tokens(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        resp = await client.get("/.well-known/jwks.json")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "public, max-age=300"
        keys = resp.json()["keys"]
        assert [k["kid"] for k in keys] == [env.app.state.signing_keys.kid]
        assert keys[0]["use"] == "sig"
        assert keys[0]["alg"] == "RS256"
        assert "d" not in keys[0]  # never the private exponent

        # A verifier with no static JWKS fetches the key over HTTP from identity itself.
        issuer = IssuerConfig(
            issuer=LOCAL_ISSUER,
            jwks_uri=f"{LOCAL_ISSUER}/.well-known/jwks.json",
            audiences=("contractum",),
            namespace="local",
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app)) as http:
            verifier = TokenVerifier(IssuerRegistry([issuer]), JwksCache(http))
            principal = await verifier.verify(token)
    assert principal.id.startswith("local:")
    assert principal.restricted is False


async def test_previous_public_key_stays_in_the_jwks_for_rotation(
    make_identity: Callable[..., Any], asgi_client: Any, tmp_path: Path
) -> None:
    previous = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    previous_file = tmp_path / "previous_public.pem"
    previous_file.write_bytes(
        previous.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    env = make_identity(jwt_previous_public_key_file=previous_file)
    async with asgi_client(env.app) as client:
        keys = (await client.get("/.well-known/jwks.json")).json()["keys"]
    assert len(keys) == 2
    assert keys[0]["kid"] == env.app.state.signing_keys.kid
    assert keys[1]["kid"] != keys[0]["kid"]
    assert all("d" not in k for k in keys)


async def test_admin_is_allowed_on_root_with_folder_facts(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = _root_id(sql, env)
        async with _remote(env.app, token) as authz:
            decision = await authz.check(_ANYONE, "folder:read", root)
            scope = await authz.accessible_folders(_ANYONE, "folder:read")
    assert decision.outcome is Outcome.ALLOW
    assert decision.folder is not None
    assert decision.folder.kind == "root"
    assert decision.folder.slug == "root"
    assert decision.folder.path == "root"
    assert decision.folder.ancestor_ids == (root,)
    assert decision.folder.organization_id is None
    assert decision.folder.archived is False
    assert scope.all is True


async def test_non_admin_gets_not_found_on_a_target_and_forbidden_without_one(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    fake_issuer: FakeIssuer,
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app):
        root = _root_id(sql, env)
        async with _remote(env.app, fake_issuer.mint("nobody")) as authz:
            on_root = await authz.check(_ANYONE, "folder:read", root)
            on_unknown = await authz.check(_ANYONE, "folder:read", uuid.uuid4())
            audit = await authz.check(_ANYONE, "audit:read", None)
            scope = await authz.accessible_folders(_ANYONE, "folder:read")
    assert on_root.outcome is Outcome.NOT_FOUND and on_root.folder is None
    assert on_unknown.outcome is Outcome.NOT_FOUND
    assert audit.outcome is Outcome.FORBIDDEN
    assert scope.all is False
    assert scope.folder_ids == frozenset()


async def test_list_permissions_are_allowed_for_everyone_and_trimmed_to_an_empty_scope(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
) -> None:
    # D-35: lists have no target object; every non-restricted caller is allowed and the handler
    # trims the rows, so a caller without access gets an empty page (never a 403 or a count).
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        async with _remote(env.app, fake_issuer.mint("nobody")) as authz:
            stranger = [
                (await authz.check(_ANYONE, perm, None)).outcome
                for perm in ("contract:list", "folder:list", "template:list", "group:list")
            ]
            no_access = await authz.accessible_folders(_ANYONE, "folder:read")
            no_contracts = await authz.accessible_folders(_ANYONE, "contract:read")
            audit = (await authz.check(_ANYONE, "audit:read", None)).outcome
        async with _remote(env.app, admin) as authz:
            allowed = [
                (await authz.check(_ANYONE, perm, None)).outcome
                for perm in ("contract:list", "folder:list", "template:list", "group:list")
            ]
    assert stranger == [Outcome.ALLOW] * 4
    assert no_access.is_empty and no_contracts.is_empty
    assert audit is Outcome.FORBIDDEN  # the audit read is not a list: it stays admin-only
    assert allowed == [Outcome.ALLOW] * 4
    # "contract:search" is not a registered permission: fail closed, even for the global admin.
    async with _remote(env.app, admin) as authz:
        unknown = (await authz.check(_ANYONE, "contract:search", None)).outcome
    assert unknown is Outcome.FORBIDDEN


async def test_extra_claims_grant_nothing_only_role_assignments_do(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    fake_issuer: FakeIssuer,
    sql: Sql,
) -> None:
    # Prohibition: no Phase 1 permission is ever derived from token claims.
    env = make_identity()
    plain = fake_issuer.mint("mallory")
    loaded = fake_issuer.mint(
        "mallory",
        extra_claims={"roles": ["global_admin"], "groups": ["admins"], "admin": True},
    )
    async with asgi_client(env.app):
        root = _root_id(sql, env)
        answers = []
        for token in (plain, loaded):
            async with _remote(env.app, token) as authz:
                answers.append(
                    [
                        (await authz.check(_ANYONE, "folder:read", root)).outcome,
                        (await authz.check(_ANYONE, "folder:list", None)).outcome,
                        (await authz.check(_ANYONE, "audit:read", None)).outcome,
                        (await authz.accessible_folders(_ANYONE, "folder:read")).all,
                    ]
                )
    assert answers[0] == answers[1]
    assert answers[1] == [Outcome.NOT_FOUND, Outcome.ALLOW, Outcome.FORBIDDEN, False]


async def test_restricted_bootstrap_token_and_missing_token_are_refused_on_internal_routes(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    bootstrap_token: Callable[[Any, Any], Awaitable[str]],
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        restricted = await bootstrap_token(client, env)
        headers = {"Authorization": f"Bearer {restricted}"}
        body = {"permission": "folder:read"}
        for path, payload in ((CHECK, body), (FOLDERS, body)):
            resp = await client.post(path, json=payload, headers=headers)
            assert resp.status_code == 403
            assert resp.json()["type"] == "urn:dc:problem:password-rotation-required"
            anonymous = await client.post(path, json=payload)
            assert anonymous.status_code == 401
            assert anonymous.json()["type"] == "urn:dc:problem:unauthenticated"
        async with _remote(env.app, restricted) as authz:
            with pytest.raises(AuthorizerUnavailable):  # a 403 is not a decision: fail closed
                await authz.check(_ANYONE, "folder:read", None)


async def test_the_principal_comes_from_the_token_never_from_the_request_body(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        await admin_token(client, env)
        admin_row = sql(env.db_url, "SELECT id FROM users")[0]
        admin_id = f"local:{admin_row['id']}"
        resp = await client.post(
            CHECK,
            json={
                "permission": "folder:list",
                "principal_id": admin_id,
                "principal": {"id": admin_id},
            },
            headers={"Authorization": f"Bearer {fake_issuer.mint('mallory')}"},
        )
        scope = await client.post(
            FOLDERS,
            json={"permission": "folder:read", "principal_id": admin_id},
            headers={"Authorization": f"Bearer {fake_issuer.mint('mallory')}"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"outcome": "allow", "folder": None, "permissions": []}
    # The scope is mallory's own (nothing), not the admin's named in the body.
    assert scope.status_code == 200
    assert scope.json() == {"all": False, "folder_ids": [], "excluded_subtrees": []}


async def test_authz_check_is_open_to_every_unrestricted_principal_and_rotate_to_local_ones(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    fake_issuer: FakeIssuer,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        async with _remote(env.app, fake_issuer.mint("nobody")) as authz:
            fake_check = (await authz.check(_ANYONE, "authz:check", None)).outcome
            fake_rotate = (await authz.check(_ANYONE, "password:rotate", None)).outcome
        async with _remote(env.app, admin) as authz:
            local_rotate = (await authz.check(_ANYONE, "password:rotate", None)).outcome
    assert fake_check is Outcome.ALLOW
    assert fake_rotate is Outcome.FORBIDDEN
    assert local_rotate is Outcome.ALLOW


async def test_folder_facts_walk_the_ancestors_and_inherit_archival(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = _root_id(sql, env)
        ids: dict[str, uuid.UUID] = {}
        parent = root
        for kind, slug, path in (
            ("organization", "acme", "root.acme"),
            ("project", "payments", "root.acme.payments"),
            ("team", "ingest", "root.acme.payments.ingest"),
        ):
            row = sql(
                env.db_url,
                "INSERT INTO folder (kind, slug, display_name, path, parent_id, created_by, "
                "updated_by) VALUES (%s, %s, %s, %s, %s, 'test', 'test') RETURNING id",
                kind,
                slug,
                slug.title(),
                path,
                parent,
            )[0]
            ids[kind] = row["id"]
            parent = row["id"]
        async with _remote(env.app, token) as authz:
            team = (await authz.check(_ANYONE, "folder:read", ids["team"])).folder
            project = (await authz.check(_ANYONE, "folder:read", ids["project"])).folder
            assert team is not None and project is not None
            assert team.archived is False
            sql(
                env.db_url,
                "UPDATE folder SET archived_at = now() WHERE id = %s",
                ids["organization"],
            )
            archived_team = (await authz.check(_ANYONE, "folder:read", ids["team"])).folder
    assert team.kind == "team"
    assert team.slug == "ingest"
    assert team.path == "root.acme.payments.ingest"
    assert team.ancestor_ids == (root, ids["organization"], ids["project"], ids["team"])
    assert team.organization_id == ids["organization"]
    assert project.ancestor_ids == (root, ids["organization"], ids["project"])
    assert project.organization_id == ids["organization"]
    assert archived_team is not None and archived_team.archived is True


async def test_an_unknown_folder_is_not_found_even_for_the_admin(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        async with _remote(env.app, token) as authz:
            decision = await authz.check(_ANYONE, "folder:read", uuid.uuid4())
    assert decision.outcome is Outcome.NOT_FOUND
    assert decision.folder is None


# --- Plan 04: Entra principals through the shared RemoteAuthorizer (AUTH-03) ----------------------


async def test_other_services_see_the_entra_answers_through_the_remote_authorizer(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    grant_sql: Callable[..., None],
    fake_issuer: FakeIssuer,
    sql: Sql,
) -> None:
    from dc_auth.testing import ENTRA_TENANT_ID
    from dc_core.problems import ProblemError

    env = make_identity()
    alice, bob, carol = (str(uuid.uuid4()) for _ in range(3))
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = _root_id(sql, env)
        _org, _project, team = await folder_chain(client, admin, root, "ra")
        group = str(uuid.uuid4())
        env.directory.add_group(group, "readers")
        sql(
            env.db_url,
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
            "display_name, created_by, updated_by) "
            "VALUES ('group', %s, %s, %s, 'readers', 'test', 'test')",
            ENTRA_TENANT_ID,
            group,
            root,
        )
        grant_sql(env.db_url, group, "team_reader", team, "entra_group")
        env.directory.add_principal("user", alice, groups=(group,))
        env.directory.add_principal("user", bob)  # unregistered: in the directory, in no group
        env.directory.add_principal("user", carol, groups=(group,), enabled=False)
        async with _remote(env.app, fake_issuer.mint_entra(alice)) as authz:
            allowed = await authz.check(_ANYONE, "folder:read", uuid.UUID(team))
            scope = await authz.accessible_folders(_ANYONE, "folder:read")
        async with _remote(env.app, fake_issuer.mint_entra(bob)) as authz:
            with pytest.raises(ProblemError) as unregistered:
                await authz.check(_ANYONE, "folder:read", uuid.UUID(team))
        async with _remote(env.app, fake_issuer.mint_entra(carol)) as authz:
            with pytest.raises(ProblemError) as disabled:
                await authz.check(_ANYONE, "folder:read", uuid.UUID(team))
    assert allowed.outcome is Outcome.ALLOW
    assert uuid.UUID(team) in scope.folder_ids
    # An answer, never an outage: AuthorizerUnavailable would surface as 503 in the caller.
    assert unregistered.value.status == 403
    assert unregistered.value.slug == "principal-not-registered"
    assert disabled.value.status == 401


# --- Plan 09: a PAT is accepted directly by every service (AUTH-06, D-71, D-72) -------------------

PRINCIPAL = "/internal/v1/authz/principal"


@asynccontextmanager
async def _remote_opaque(app: Any) -> AsyncIterator[tuple[TokenVerifier, httpx.AsyncClient]]:
    """A verifier as another service builds it: PATs go to identity, nothing is cached."""
    from dc_auth.opaque import RemoteOpaqueResolver

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        resolver = RemoteOpaqueResolver(http, base_url="http://identity", timeout_s=2.0)
        issuer = IssuerConfig(
            issuer=LOCAL_ISSUER,
            jwks_uri=f"{LOCAL_ISSUER}/.well-known/jwks.json",
            audiences=("contractum",),
            namespace="local",
        )
        yield TokenVerifier(IssuerRegistry([issuer]), JwksCache(http), opaque=resolver), http


async def test_principal_route_answers_the_owner_of_a_live_pat_and_401_for_a_revoked_one(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        body = await create_pat(client, admin, name="ci")
        pat = body["token"]
        live = await client.post(PRINCIPAL, content=b"{}", headers=_json_bearer(pat))
        revoked = await client.delete(
            f"/api/v1/identity/pats/{body['id']}", headers={"Authorization": f"Bearer {admin}"}
        )
        dead = await client.post(PRINCIPAL, content=b"{}", headers=_json_bearer(pat))
        owner = sql(env.db_url, "SELECT id FROM users")[0]["id"]
    assert live.status_code == 200, live.text
    out = live.json()
    assert out["type"] == "pat" and out["via"] == "pat"
    assert out["credential_id"] == pat.split("_")[1]
    assert out["id"] == f"local:{owner}"
    assert pat not in live.text  # the token is never echoed
    assert revoked.status_code == 204, revoked.text
    assert dead.status_code == 401


def _json_bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def test_a_pat_verifies_and_authorizes_through_the_remote_resolver(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        _org, _project, team = await folder_chain(client, admin, _root_id(sql, env), "rp")
        pat = (await create_pat(client, admin))["token"]
        async with _remote_opaque(env.app) as (verifier, _http):
            principal = await verifier.verify(pat)
        async with _remote(env.app, pat) as authz:
            decision = await authz.check(_ANYONE, "folder:read", uuid.UUID(team))
    assert principal.via == "pat" and principal.type == "pat"
    assert principal.id.startswith("local:")
    assert decision.outcome is Outcome.ALLOW


async def test_revoking_a_pat_through_identity_makes_the_next_remote_verification_401(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    from dc_core.problems import ProblemError

    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        body = await create_pat(client, admin)
        pat = body["token"]
        async with _remote_opaque(env.app) as (verifier, _http):
            before = await verifier.verify(pat)
            revoked = await client.delete(
                f"/api/v1/identity/pats/{body['id']}",
                headers={"Authorization": f"Bearer {admin}"},
            )
            with pytest.raises(ProblemError) as after:
                await verifier.verify(pat)  # no cache anywhere: the very next call
    assert before.via == "pat"
    assert revoked.status_code == 204, revoked.text
    assert after.value.status == 401
    assert after.value.slug == "unauthenticated"


async def test_the_pat_of_an_entra_owner_removed_from_every_group_is_403_not_503_remotely(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
    fake_issuer: FakeIssuer,
    sql: Sql,
) -> None:
    from dc_auth.testing import ENTRA_TENANT_ID
    from dc_core.problems import ProblemError

    env = make_identity()
    alice, group = str(uuid.uuid4()), str(uuid.uuid4())
    async with asgi_client(env.app) as client:
        await admin_token(client, env)
        root = _root_id(sql, env)
        env.directory.add_group(group, "engineers")
        sql(
            env.db_url,
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
            "display_name, created_by, updated_by) "
            "VALUES ('group', %s, %s, %s, 'engineers', 'test', 'test')",
            ENTRA_TENANT_ID,
            group,
            root,
        )
        env.directory.add_principal("user", alice, groups=(group,))
        pat = (await create_pat(client, fake_issuer.mint_entra(alice)))["token"]
        async with _remote_opaque(env.app) as (verifier, _http):
            working = await verifier.verify(pat)
            env.directory.set_groups(alice, set())
            with pytest.raises(ProblemError) as unregistered:
                await verifier.verify(pat)
        direct = await client.post(PRINCIPAL, content=b"{}", headers=_json_bearer(pat))
    assert working.via == "pat"
    assert unregistered.value.status == 403
    assert unregistered.value.slug == "principal-not-registered"
    assert direct.status_code == 403
    assert direct.json()["type"] == "urn:dc:problem:principal-not-registered"


async def test_password_rotation_is_decided_only_for_a_local_principal_signed_in_with_a_jwt(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_pat: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    # D-79, Pitfall 6: a PAT carries its local owner's id but is not an interactive sign-in.
    env = make_identity()
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        pat = (await create_pat(client, admin))["token"]
        async with _remote(env.app, admin) as authz:
            by_jwt = (await authz.check(_ANYONE, "password:rotate", None)).outcome
        async with _remote(env.app, pat) as authz:
            by_pat = (await authz.check(_ANYONE, "password:rotate", None)).outcome
    assert by_jwt is Outcome.ALLOW
    assert by_pat is Outcome.FORBIDDEN
