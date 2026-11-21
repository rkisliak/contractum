"""Every identity route is declared with require() or public() (D-07, PLAT-04)."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dc_auth.roles import PERMISSIONS
from dc_core.policy import assert_all_routes_have_policy, find_policy
from dc_identity.main import create_identity_app
from dc_identity.settings import IdentitySettings
from fastapi.routing import APIRoute, iter_route_contexts

pytestmark = pytest.mark.unit

ALLOWED_PUBLIC = frozenset(
    {
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("GET", "/.well-known/jwks.json"),
        ("POST", "/api/v1/identity/auth/login"),
        ("POST", "/api/v1/identity/auth/refresh"),
        ("POST", "/api/v1/identity/auth/logout"),
        ("GET", "/api/v1/identity/auth/policy"),
        ("GET", "/api/v1/identity/version"),
    }
)
ALLOWED_NON_API = frozenset({"/healthz", "/readyz", "/.well-known/jwks.json"})


def _app(tmp_path: Path):  # type: ignore[no-untyped-def]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file = tmp_path / "jwt_signing_key.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    # The database URL is never connected: the walker only needs the route table.
    settings = IdentitySettings(
        database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never",  # type: ignore[arg-type]
        jwt_signing_key_file=key_file,
        bootstrap_secret_file=tmp_path / "absent",
    )
    return create_identity_app(settings)


def test_every_identity_route_declares_a_policy_and_only_documented_routes_are_public(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    assert_all_routes_have_policy(
        app,
        module="identity",
        allowed_non_api=ALLOWED_NON_API,
        allowed_public=ALLOWED_PUBLIC,
        min_routes=94,
        known_permissions=frozenset(PERMISSIONS),
    )


def test_the_public_route_set_is_exactly_the_seven_documented_routes(tmp_path: Path) -> None:
    app = _app(tmp_path)
    public: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    for rc in iter_route_contexts(app.routes):
        if not isinstance(rc.original_route, APIRoute):
            continue
        meta = find_policy(rc.dependant)
        assert meta is not None, rc.path
        for method in rc.methods or ():
            seen.add((method, rc.path or ""))
            if meta.public:
                public.add((method, rc.path or ""))
    assert public == ALLOWED_PUBLIC
    assert ("POST", "/internal/v1/authz/check") in seen
    assert ("POST", "/internal/v1/authz/accessible-folders") in seen
    assert ("POST", "/internal/v1/authz/principal") in seen
    assert ("POST", "/internal/v1/authz/grantees") in seen
    assert ("POST", "/internal/v1/authz/members/check") in seen
    assert ("POST", "/api/v1/identity/auth/password") in seen
    assert ("GET", "/api/v1/identity/me") in seen
    assert ("GET", "/api/v1/identity/me/capabilities") in seen
    for route in (
        ("POST", "/api/v1/identity/folders"),
        ("GET", "/api/v1/identity/folders"),
        ("GET", "/api/v1/identity/folders/{folder_id}"),
        ("PATCH", "/api/v1/identity/folders/{folder_id}"),
        ("POST", "/api/v1/identity/folders/{folder_id}/archive"),
        ("POST", "/api/v1/identity/folders/{folder_id}/unarchive"),
        ("PUT", "/api/v1/identity/folders/{folder_id}/block-inheritance"),
        ("GET", "/api/v1/identity/folders/{folder_id}/effective-access"),
        ("POST", "/api/v1/identity/users"),
        ("GET", "/api/v1/identity/users"),
        ("GET", "/api/v1/identity/users/{user_id}"),
        ("POST", "/api/v1/identity/folders/{folder_id}/grants"),
        ("GET", "/api/v1/identity/folders/{folder_id}/grants"),
        ("GET", "/api/v1/identity/grants/{grant_id}"),
        ("DELETE", "/api/v1/identity/grants/{grant_id}"),
        ("POST", "/api/v1/identity/groups"),
        ("POST", "/api/v1/identity/groups/{group_id}/members"),
        ("GET", "/api/v1/identity/groups"),
        ("GET", "/api/v1/identity/groups/{group_id}"),
        ("PATCH", "/api/v1/identity/groups/{group_id}"),
        ("DELETE", "/api/v1/identity/groups/{group_id}"),
        ("GET", "/api/v1/identity/groups/{group_id}/members"),
        ("DELETE", "/api/v1/identity/groups/{group_id}/members/{member_id}"),
        ("GET", "/api/v1/identity/folders/{folder_id}/grantable-groups"),
        ("POST", "/api/v1/identity/entra/groups"),
        ("GET", "/api/v1/identity/entra/groups"),
        ("GET", "/api/v1/identity/entra/groups/{registration_id}"),
        ("GET", "/api/v1/identity/entra/groups/{registration_id}/members"),
        ("DELETE", "/api/v1/identity/entra/groups/{registration_id}"),
        ("POST", "/api/v1/identity/entra/users"),
        ("GET", "/api/v1/identity/entra/users"),
        ("GET", "/api/v1/identity/entra/users/{entra_user_id}"),
        ("POST", "/api/v1/identity/entra/service-principals"),
        ("GET", "/api/v1/identity/entra/service-principals"),
        ("GET", "/api/v1/identity/entra/service-principals/{registration_id}"),
        ("POST", "/api/v1/identity/pats"),
        ("GET", "/api/v1/identity/pats"),
        ("DELETE", "/api/v1/identity/pats/{credential_id}"),
        ("GET", "/api/v1/identity/admin/pats"),
        ("DELETE", "/api/v1/identity/admin/pats/{credential_id}"),
        ("POST", "/api/v1/identity/service-accounts"),
        ("GET", "/api/v1/identity/service-accounts"),
        ("GET", "/api/v1/identity/service-accounts/{service_account_id}"),
        ("PATCH", "/api/v1/identity/service-accounts/{service_account_id}"),
        ("POST", "/api/v1/identity/service-accounts/{service_account_id}/keys"),
        ("GET", "/api/v1/identity/service-accounts/{service_account_id}/keys"),
        ("DELETE", "/api/v1/identity/service-accounts/{service_account_id}/keys/{key_id}"),
    ):
        assert route in seen
    # Quick 261119-0v1: exactly one folder DELETE exists (a folder:delete holder, empty folders
    # only) and its deletion check; folders are never moved through the API.
    assert ("DELETE", "/api/v1/identity/folders/{folder_id}") in seen
    assert ("GET", "/api/v1/identity/folders/{folder_id}/deletion-check") in seen
    assert [(m, p) for m, p in seen if m == "DELETE" and "/folders" in p] == [
        ("DELETE", "/api/v1/identity/folders/{folder_id}")
    ]
    assert not any(m in ("PUT", "PATCH") and p.endswith("/parent") for m, p in seen)
