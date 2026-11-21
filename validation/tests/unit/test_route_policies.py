"""Every validation route is declared with require() or public() (D-07, PLAT-04)."""

from __future__ import annotations

import pytest
from dc_auth.roles import PERMISSIONS
from dc_core.policy import assert_all_routes_have_policy, find_policy
from dc_validation.main import create_validation_app
from dc_validation.settings import ValidationSettings
from fastapi.routing import APIRoute, iter_route_contexts

pytestmark = pytest.mark.unit

ALLOWED_PUBLIC = frozenset(
    {("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/api/v1/validation/version")}
)
# Every route with its exact permission (method, path) -> permission; None marks a public route.
EXPECTED: dict[tuple[str, str], str | None] = {
    ("GET", "/healthz"): None,
    ("GET", "/readyz"): None,
    ("GET", "/api/v1/validation/version"): None,
    ("POST", "/api/v1/validation/folders/{folder_id}/rule-sets"): "ruleset:create",
    ("GET", "/api/v1/validation/rule-sets/{rule_set_id}"): "ruleset:read",
    ("PUT", "/api/v1/validation/rule-sets/{rule_set_id}/draft"): "ruleset:update",
    ("PUT", "/api/v1/validation/rule-sets/{rule_set_id}/draft/rules/{rule_key}"): "ruleset:update",
    (
        "DELETE",
        "/api/v1/validation/rule-sets/{rule_set_id}/draft/rules/{rule_key}",
    ): "ruleset:update",
    ("POST", "/api/v1/validation/rule-sets/{rule_set_id}/draft/check"): "ruleset:read",
    ("POST", "/api/v1/validation/rule-sets/{rule_set_id}/publish"): "ruleset:publish",
    ("GET", "/api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}"): "ruleset:read",
    ("GET", "/api/v1/validation/folders/{folder_id}/rule-sets"): "ruleset:read",
    ("GET", "/api/v1/validation/rule-sets/{rule_set_id}/versions"): "ruleset:read",
    (
        "POST",
        "/api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}/deprecate",
    ): "ruleset:deprecate",
    ("DELETE", "/api/v1/validation/rule-sets/{rule_set_id}"): "ruleset:update",
    ("PUT", "/api/v1/validation/rule-sets/{rule_set_id}/mandatory"): "ruleset:mandate",
    (
        "POST",
        "/api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}/enforce-now",
    ): "ruleset:mandate",
    (
        "POST",
        "/api/v1/validation/folders/{folder_id}/platform-rule-sets/{rule_set_id}"
        "/versions/{version_no}/enforce-now",
    ): "ruleset:mandate",
    (
        "PUT",
        "/api/v1/validation/folders/{folder_id}/platform-rule-sets/{rule_set_id}/paused",
    ): "ruleset:mandate",
    (
        "PUT",
        "/api/v1/validation/folders/{folder_id}/platform-rule-sets/{rule_set_id}"
        "/versions/{version_no}/enforce-at",
    ): "ruleset:mandate",
    ("PUT", "/api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}/enforce-at"): (
        "ruleset:mandate"
    ),
    ("POST", "/internal/v1/validation/folders/{folder_id}/runs"): "contract:validate",
    ("GET", "/internal/v1/validation/runs/{run_id}"): "contract:read-published",
    ("POST", "/internal/v1/validation/rule-sets/{rule_set_id}/dry-run"): "ruleset:dry-run",
    ("POST", "/internal/v1/validation/folders/{folder_id}/drift"): "governance:drift-read",
    ("GET", "/internal/v1/validation/folders/{folder_id}/usage"): "folder:delete",
    ("POST", "/internal/v1/validation/folders/{folder_id}/purge"): "folder:delete",
    ("GET", "/internal/v1/about"): "settings:read",
    ("POST", "/api/v1/validation/folders/{folder_id}/validate"): "contract:validate",
    ("GET", "/api/v1/validation/folders/{folder_id}/available-rule-sets"): "ruleset:read-published",
    (
        "GET",
        "/api/v1/validation/folders/{folder_id}/available-rule-sets/{rule_set_id}",
    ): "ruleset:read-published",
    (
        "GET",
        "/api/v1/validation/folders/{folder_id}/available-rule-sets/{rule_set_id}/versions/{version_no}",
    ): "ruleset:read-published",
}

ALLOWED_NON_API = frozenset({"/api/v1/validation/openapi.json", "/healthz", "/readyz"})


def _app():  # type: ignore[no-untyped-def]
    # The database URL is never connected: the walker only needs the route table.
    return create_validation_app(
        ValidationSettings(database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never")  # type: ignore[arg-type]
    )


def test_every_validation_route_declares_a_policy_and_only_the_health_routes_are_public() -> None:
    assert_all_routes_have_policy(
        _app(),
        module="validation",
        allowed_non_api=ALLOWED_NON_API,
        allowed_public=ALLOWED_PUBLIC,
        min_routes=len(EXPECTED),
        known_permissions=frozenset(PERMISSIONS),
    )


def test_the_route_table_and_permissions_are_exactly_the_documented_ones() -> None:
    declared: dict[tuple[str, str], str | None] = {}
    for rc in iter_route_contexts(_app().routes):
        if not isinstance(rc.original_route, APIRoute):
            continue
        meta = find_policy(rc.dependant)
        assert meta is not None, rc.path
        for method in rc.methods or ():
            declared[(method, rc.path or "")] = None if meta.public else meta.permission
    assert declared == EXPECTED


def test_openapi_is_served_under_the_module_prefix_and_lists_no_internal_path() -> None:
    app = _app()
    assert app.openapi_url == "/api/v1/validation/openapi.json"
    assert not [path for path in app.openapi()["paths"] if "/internal" in path]
