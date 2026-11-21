"""Every contracts route is declared with require() or public() (D-07, PLAT-04)."""

from __future__ import annotations

import pytest
from dc_auth.roles import PERMISSIONS
from dc_contracts.main import create_contracts_app
from dc_contracts.settings import ContractsSettings
from dc_core.policy import assert_all_routes_have_policy, find_policy
from fastapi.routing import APIRoute, iter_route_contexts

pytestmark = pytest.mark.unit

ALLOWED_PUBLIC = frozenset(
    {("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/api/v1/contracts/version")}
)
ALLOWED_NON_API = frozenset({"/api/v1/contracts/openapi.json", "/healthz", "/readyz"})
EXPECTED = {
    ("POST", "/api/v1/contracts/settings/test-email"): "settings:write",
    ("GET", "/internal/v1/about"): "settings:read",
    ("GET", "/internal/v1/contracts/folders/{folder_id}/usage"): "folder:delete",
    ("POST", "/internal/v1/contracts/folders/{folder_id}/purge"): "folder:delete",
    ("POST", "/api/v1/contracts/folders/{folder_id}/contracts"): "contract:create",
    ("POST", "/api/v1/contracts/folders/{folder_id}/contracts/from-template"): "contract:create",
    ("POST", "/api/v1/contracts/folders/{folder_id}/contracts/starter"): "contract:create",
    ("POST", "/api/v1/contracts/folders/{folder_id}/contracts/from-version"): "contract:create",
    ("GET", "/api/v1/contracts/folders/{folder_id}/available-templates"): "contract:create",
    (
        "GET",
        "/api/v1/contracts/folders/{folder_id}/available-templates/{template_id}"
        "/versions/{version_no}/document",
    ): "contract:create",
    ("GET", "/api/v1/contracts/contracts"): "contract:list",
    ("GET", "/api/v1/contracts/contracts/{contract_id}"): "contract:view",
    ("DELETE", "/api/v1/contracts/contracts/{contract_id}"): "contract:delete",
    ("POST", "/api/v1/contracts/contracts/{contract_id}/restore"): "contract:restore",
    ("GET", "/api/v1/contracts/contracts/{contract_id}/document"): "contract:read",
    ("GET", "/api/v1/contracts/contracts/{contract_id}/drafts"): "contract:read",
    ("GET", "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}"): "contract:read",
    (
        "GET",
        "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/document",
    ): "contract:read",
    ("POST", "/api/v1/contracts/contracts/{contract_id}/drafts"): "contract:update",
    ("PUT", "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}"): "contract:update",
    ("DELETE", "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}"): "contract:update",
    (
        "POST",
        "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/validate",
    ): "contract:read",
    (
        "POST",
        "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/publish",
    ): "contract:publish",
    ("GET", "/api/v1/contracts/contracts/{contract_id}/versions"): "contract:read-published",
    (
        "GET",
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/metadata",
    ): "contract:read-published",
    (
        "GET",
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}",
    ): "contract:read-published",
    (
        "GET",
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/validation-report",
    ): "contract:read-published",
    ("GET", "/api/v1/contracts/contracts/{contract_id}/diff"): "contract:read-published",
    (
        "POST",
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/promote",
    ): "contract:publish",
    (
        "POST",
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/unpublish",
    ): "contract:publish",
    (
        "POST",
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/republish",
    ): "contract:publish",
    (
        "POST",
        "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/deprecate",
    ): "contract:publish",
    ("POST", "/api/v1/contracts/folders/{folder_id}/templates"): "template:create",
    ("GET", "/api/v1/contracts/templates"): "template:list",
    ("GET", "/api/v1/contracts/templates/{template_id}"): "template:read-published",
    ("GET", "/api/v1/contracts/templates/{template_id}/document"): "template:read",
    ("PUT", "/api/v1/contracts/templates/{template_id}/draft"): "template:update",
    ("POST", "/api/v1/contracts/templates/{template_id}/publish"): "template:publish",
    ("GET", "/api/v1/contracts/templates/{template_id}/versions"): "template:read-published",
    (
        "POST",
        "/api/v1/contracts/templates/{template_id}/versions/{version_no}/deprecate",
    ): "template:deprecate",
    (
        "GET",
        "/api/v1/contracts/templates/{template_id}/versions/{version_no}/document",
    ): "template:read-published",
    ("POST", "/api/v1/contracts/folders/{folder_id}/cards"): "card:create",
    ("GET", "/api/v1/contracts/folders/{folder_id}/cards"): "folder:read",
    ("GET", "/api/v1/contracts/cards"): "card:read",
    ("GET", "/api/v1/contracts/cards/{card_id}"): "card:read",
    ("PATCH", "/api/v1/contracts/cards/{card_id}"): "card:write",
    ("POST", "/api/v1/contracts/cards/{card_id}/webhook-secret"): "card:manage-secret",
    ("POST", "/api/v1/contracts/cards/{card_id}/archive"): "card:write",
    ("POST", "/api/v1/contracts/cards/{card_id}/members"): "card:write",
    (
        "DELETE",
        "/api/v1/contracts/cards/{card_id}/members/{member_type}/{member_id}",
    ): "card:write",
    ("POST", "/api/v1/contracts/cards/{card_id}/subscriptions"): "card:write",
    ("GET", "/api/v1/contracts/cards/{card_id}/subscriptions"): "card:read",
    (
        "POST",
        "/api/v1/contracts/cards/{card_id}/subscriptions/{subscription_id}/move",
    ): "card:write",
    (
        "POST",
        "/api/v1/contracts/cards/{card_id}/subscriptions/{subscription_id}/unsubscribe",
    ): "card:write",
    ("GET", "/api/v1/contracts/cards/{card_id}/notices"): "card:read",
    ("GET", "/api/v1/contracts/contracts/{contract_id}/subscribers"): "contract:read",
    (
        "POST",
        "/api/v1/contracts/contracts/{contract_id}/subscriptions/{subscription_id}/remove",
    ): "contract:publish",
    ("GET", "/api/v1/contracts/contracts/{contract_id}/rule-sets"): "contract:read-metadata",
    ("PUT", "/api/v1/contracts/contracts/{contract_id}/rule-sets/{rule_set_id}"): "contract:update",
    (
        "DELETE",
        "/api/v1/contracts/contracts/{contract_id}/rule-sets/{rule_set_id}",
    ): "contract:update",
    (
        "POST",
        "/api/v1/contracts/governance/rule-set-dry-runs",
    ): "ruleset:dry-run",
    ("GET", "/api/v1/contracts/governance/drift"): "governance:drift-read",
    ("GET", "/healthz"): None,
    ("GET", "/readyz"): None,
    ("GET", "/api/v1/contracts/version"): None,
}


def _app():  # type: ignore[no-untyped-def]
    # The database URL is never connected: the walker only needs the route table.
    return create_contracts_app(
        ContractsSettings(database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never")  # type: ignore[arg-type]
    )


def test_every_contracts_route_declares_a_policy_and_only_the_health_routes_are_public() -> None:
    assert_all_routes_have_policy(
        _app(),
        module="contracts",
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
            declared[(method, rc.path or "")] = meta.permission
    assert declared == EXPECTED


def test_no_card_scoped_fetch_route_exists() -> None:
    """D-88: members fetch versions only through the version routes, with their own access."""
    declared = {rc.path or "" for rc in iter_route_contexts(_app().routes)}
    card_paths = [p for p in declared if p.startswith("/api/v1/contracts/cards/")]
    assert card_paths
    for path in card_paths:
        assert not path.endswith("/versions"), path
        assert not path.endswith("/document"), path
        assert "/versions/" not in path, path
    documented = {path for (_method, path) in EXPECTED if "/cards/" in path}
    assert documented <= set(card_paths)


def test_openapi_is_served_under_the_module_prefix_and_lists_no_internal_path() -> None:
    app = _app()
    assert app.openapi_url == "/api/v1/contracts/openapi.json"
    paths = app.openapi()["paths"]
    assert not [path for path in paths if "/internal" in path]
    create = paths["/api/v1/contracts/folders/{folder_id}/contracts"]["post"]
    assert set(create["requestBody"]["content"]) == {
        "application/yaml",
        "application/x-yaml",
        "text/yaml",
        "application/json",
    }


def test_supported_versions_setting_rejects_values_without_a_vendored_schema() -> None:
    with pytest.raises(ValueError, match="supported_odcs_versions"):
        ContractsSettings(supported_odcs_versions=frozenset({"v3.0.2"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="supported_odcs_versions"):
        ContractsSettings(supported_odcs_versions=frozenset())  # type: ignore[arg-type]


def test_impact_page_settings_stay_within_validations_batch_caps() -> None:
    settings = ContractsSettings()
    assert (settings.impact_page_size, settings.impact_page_max_folders) == (25, 20)
    assert settings.impact_page_max_bytes == 4 * 1024 * 1024
    for field, value in (
        ("impact_page_size", 26),
        ("impact_page_size", 0),
        ("impact_page_max_folders", 21),
        ("impact_page_max_bytes", 4 * 1024 * 1024 + 1),
        ("impact_page_max_bytes", 0),
    ):
        with pytest.raises(ValueError, match=field):
            ContractsSettings(**{field: value})  # type: ignore[arg-type]
