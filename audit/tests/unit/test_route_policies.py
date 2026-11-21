"""Every audit route is declared with require() or public() (D-07, PLAT-04)."""

from __future__ import annotations

import pytest
from dc_audit.main import create_audit_app
from dc_audit.settings import AuditSettings
from dc_auth.roles import PERMISSIONS
from dc_core.policy import assert_all_routes_have_policy, find_policy
from fastapi.routing import APIRoute, iter_route_contexts

pytestmark = pytest.mark.unit

ALLOWED_PUBLIC = frozenset(
    {("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/api/v1/audit/version")}
)
ALLOWED_NON_API = frozenset({"/healthz", "/readyz"})


def _app():  # type: ignore[no-untyped-def]
    # The database URL is never connected: the walker only needs the route table.
    return create_audit_app(
        AuditSettings(database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never")  # type: ignore[arg-type]
    )


def test_every_audit_route_declares_a_policy_and_only_the_health_routes_are_public() -> None:
    assert_all_routes_have_policy(
        _app(),
        module="audit",
        allowed_non_api=ALLOWED_NON_API,
        allowed_public=ALLOWED_PUBLIC,
        min_routes=7,
        known_permissions=frozenset(PERMISSIONS),
    )


def test_public_query_routes_require_audit_list_and_the_internal_read_audit_read() -> None:
    non_public: dict[tuple[str, str], str] = {}
    public: set[tuple[str, str]] = set()
    for rc in iter_route_contexts(_app().routes):
        if not isinstance(rc.original_route, APIRoute):
            continue
        meta = find_policy(rc.dependant)
        assert meta is not None, rc.path
        for method in rc.methods or ():
            key = (method, rc.path or "")
            if meta.public:
                public.add(key)
            else:
                non_public[key] = meta.permission or ""
    assert public == ALLOWED_PUBLIC
    assert non_public == {
        ("GET", "/api/v1/audit/events"): "audit:list",
        ("GET", "/api/v1/audit/events/{event_id}"): "audit:list",
        ("GET", "/internal/v1/events"): "audit:read",
        ("GET", "/internal/v1/about"): "settings:read",
    }


def test_openapi_is_served_under_the_module_prefix_and_lists_no_internal_path() -> None:
    app = _app()
    assert app.openapi_url == "/api/v1/audit/openapi.json"
    assert not [path for path in app.openapi()["paths"] if "/internal" in path]
