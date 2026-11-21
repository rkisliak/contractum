"""Traefik static and dynamic configuration rules, parsed from the YAML (no Docker needed)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

GATEWAY = Path(__file__).resolve().parents[2] / "gateway"
MODULES = ("identity", "contracts", "validation", "audit")
# The one non-module router (merged OpenAPI document and Swagger UI), pinned exactly below.
API_DOCS_RULE = (
    "Path(`/api/v1/openapi.json`) || Path(`/api/v1/docs`) || PathPrefix(`/api/v1/docs/`)"
)
# The web UI router (D-UI-08-06, research Pattern 5): GET and HEAD only, every path except the API,
# internal, well-known and health ones, one priority above the catch-all.
UI_RULE = (
    "(Method(`GET`) || Method(`HEAD`)) && PathPrefix(`/`) && !PathPrefix(`/api/`)"
    " && !Path(`/api`) && !PathPrefix(`/internal/`) && !PathPrefix(`/.well-known/`)"
    " && !Path(`/healthz`) && !Path(`/readyz`)"
)
STANDARD_MIDDLEWARES = [
    "security-headers",
    "cors",
    "gateway-problems",
    "strip-identity-headers",
    "body-limit",
    "rl-default",
]


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture(scope="module")
def routers() -> dict[str, dict[str, Any]]:
    routers: dict[str, dict[str, Any]] = _load(GATEWAY / "dynamic" / "routers.yml")["http"][
        "routers"
    ]
    return routers


@pytest.fixture(scope="module")
def services() -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = _load(GATEWAY / "dynamic" / "routers.yml")["http"][
        "services"
    ]
    return services


@pytest.fixture(scope="module")
def middlewares() -> dict[str, dict[str, Any]]:
    middlewares: dict[str, dict[str, Any]] = _load(GATEWAY / "dynamic" / "middlewares.yml")["http"][
        "middlewares"
    ]
    return middlewares


@pytest.fixture(scope="module")
def static() -> dict[str, Any]:
    return _load(GATEWAY / "traefik.yml")


def test_gateway_problems_is_an_errors_middleware_for_413_429_and_5xx(
    middlewares: dict[str, dict[str, Any]],
) -> None:
    errors = middlewares["gateway-problems"]["errors"]
    assert errors["status"] == ["413", "429", "500-599"]
    assert errors["service"] == "problem-responder"
    assert errors["query"] == "/{status}"
    assert errors["errorRequestHeaders"] == ["X-Request-ID"]


def test_replace_path_404_rewrites_to_the_404_page(
    middlewares: dict[str, dict[str, Any]],
) -> None:
    assert middlewares["replace-path-404"]["replacePath"]["path"] == "/404"


def test_service_rendered_statuses_pass_through_the_gateway_unchanged(
    middlewares: dict[str, dict[str, Any]],
) -> None:
    statuses = middlewares["gateway-problems"]["errors"]["status"]
    for passthrough in ("400", "401", "403", "404", "405", "406", "409", "415", "422"):
        assert passthrough not in statuses


def test_security_headers_is_hsts_for_a_year_with_subdomains(
    middlewares: dict[str, dict[str, Any]],
) -> None:
    headers = middlewares["security-headers"]["headers"]
    assert headers["stsSeconds"] == 31536000
    assert headers["stsIncludeSubdomains"] is True
    # HSTS only: the UI's CSP and the other headers come from nginx, never from the gateway.
    assert set(headers) == {"stsSeconds", "stsIncludeSubdomains"}


def test_every_router_starts_with_security_headers_then_cors_then_problems_then_the_limiters(
    routers: dict[str, dict[str, Any]],
) -> None:
    for name, router in routers.items():
        chain = router["middlewares"]
        # Outermost, so HSTS also lands on the 404, 413, 429 and 5xx that gateway-problems renders.
        assert chain[0] == "security-headers", name
        assert chain.count("security-headers") == 1, name
        if name == "ui":
            # Same-origin static files: no CORS; gateway-problems still wraps the 429 and 5xx.
            assert chain == ["security-headers", "gateway-problems", "rl-ui"]
            continue
        assert chain[1] == "cors", name
        assert chain[2] == "gateway-problems", name
        assert "rl-ui" not in chain, name  # the static-asset bucket never guards an API route
        for limiter in ("body-limit", "rl-default", "rl-auth"):
            if limiter in chain:
                assert chain.index(limiter) > chain.index("gateway-problems"), (name, limiter)


def test_catchall_problem_is_the_only_router_whose_rule_is_pathprefix_slash(
    routers: dict[str, dict[str, Any]],
) -> None:
    root_rules = [name for name, r in routers.items() if r["rule"] == "PathPrefix(`/`)"]
    assert root_rules == ["catchall-problem"]
    catchall = routers["catchall-problem"]
    assert catchall["service"] == "problem-responder"
    assert catchall["priority"] == 1
    assert catchall["entryPoints"] == ["websecure"]
    assert catchall["middlewares"] == [
        "security-headers",
        "cors",
        "gateway-problems",
        "rl-default",
        "replace-path-404",
    ]


def test_only_the_catchall_targets_the_problem_responder(
    routers: dict[str, dict[str, Any]],
) -> None:
    targeting = [name for name, r in routers.items() if r["service"] == "problem-responder"]
    assert targeting == ["catchall-problem"]


def test_every_other_router_is_a_trailing_slash_module_prefix_to_its_module(
    routers: dict[str, dict[str, Any]],
) -> None:
    for name, router in routers.items():
        if name in {"catchall-problem", "api-docs", "ui"}:
            continue
        module = router["service"]
        assert module in MODULES, name
        rule = router["rule"]
        assert rule.startswith(f"PathPrefix(`/api/v1/{module}/"), name
        assert rule.endswith("/`)"), name


def test_the_api_docs_router_is_the_one_pinned_non_module_router(
    routers: dict[str, dict[str, Any]], services: dict[str, dict[str, Any]]
) -> None:
    non_module = {name for name, r in routers.items() if r["service"] not in MODULES}
    assert non_module == {"catchall-problem", "api-docs", "ui"}
    api_docs = routers["api-docs"]
    assert api_docs["rule"] == API_DOCS_RULE
    assert api_docs["entryPoints"] == ["websecure"]
    assert api_docs["tls"] == {}
    assert api_docs["middlewares"] == STANDARD_MIDDLEWARES
    assert api_docs["service"] == "api-docs"
    assert "priority" not in api_docs
    assert services["api-docs"]["loadBalancer"]["servers"] == [{"url": "http://api-docs:8080"}]


def test_the_ui_router_serves_get_and_head_only_one_priority_above_the_catchall(
    routers: dict[str, dict[str, Any]], services: dict[str, dict[str, Any]]
) -> None:
    ui = routers["ui"]
    assert ui["rule"] == UI_RULE
    assert ui["priority"] == 2
    assert ui["priority"] > routers["catchall-problem"]["priority"]
    assert ui["entryPoints"] == ["websecure"]
    assert ui["tls"] == {}
    assert ui["middlewares"] == ["security-headers", "gateway-problems", "rl-ui"]
    assert ui["service"] == "ui"
    assert services["ui"]["loadBalancer"]["servers"] == [{"url": "http://ui:8080"}]


def test_the_ui_rule_excludes_every_api_internal_well_known_and_health_path(
    routers: dict[str, dict[str, Any]],
) -> None:
    rule = routers["ui"]["rule"]
    for excluded in (
        "!PathPrefix(`/api/`)",
        "!Path(`/api`)",
        "!PathPrefix(`/internal/`)",
        "!PathPrefix(`/.well-known/`)",
        "!Path(`/healthz`)",
        "!Path(`/readyz`)",
    ):
        assert excluded in rule, excluded
    assert "Method(`POST`)" not in rule


def test_the_auth_router_is_stricter_than_the_module_router(
    routers: dict[str, dict[str, Any]],
) -> None:
    assert "rl-auth" in routers["identity-auth"]["middlewares"]
    assert "rl-default" in routers["identity"]["middlewares"]


def test_rate_limits_keep_the_api_buckets_and_give_static_assets_their_own(
    middlewares: dict[str, dict[str, Any]],
) -> None:
    def limits(name: str) -> tuple[int, str, int]:
        rate = middlewares[name]["rateLimit"]
        return rate["average"], rate["period"], rate["burst"]

    assert limits("rl-default") == (50, "1s", 100)
    assert limits("rl-auth") == (5, "1s", 10)
    # A cold SPA load is dozens of immutable assets at once (UI-BUILD-LOG, 2026-11-15).
    assert limits("rl-ui") == (300, "1s", 600)


def test_services_point_at_the_compose_hosts(services: dict[str, dict[str, Any]]) -> None:
    for module in MODULES:
        assert services[module]["loadBalancer"]["servers"] == [{"url": f"http://{module}:8000"}]
    assert services["problem-responder"]["loadBalancer"]["servers"] == [
        {"url": "http://problem-responder:8080"}
    ]


def test_traefik_keeps_sanitize_path_and_states_the_encoded_character_options(
    static: dict[str, Any],
) -> None:
    http = static["entryPoints"]["websecure"]["http"]
    assert http["sanitizePath"] is True
    assert http["encodedCharacters"] == {
        "allowEncodedSlash": True,
        "allowEncodedBackSlash": True,
        "allowEncodedNullCharacter": True,
        "allowEncodedSemicolon": True,
    }


def test_access_log_drops_request_headers(static: dict[str, Any]) -> None:
    assert static["accessLog"]["fields"]["headers"]["defaultMode"] == "drop"


def test_cors_answers_preflights_itself_for_the_single_dev_origin(
    middlewares: dict[str, dict[str, Any]],
) -> None:
    cors = middlewares["cors"]["headers"]
    assert cors["accessControlAllowOriginList"] == ["http://localhost:5173"]
    assert set(cors["accessControlAllowMethods"]) == {"GET", "POST", "PATCH", "PUT", "DELETE"}
    assert len(cors["accessControlAllowMethods"]) == 5
    # The version lifecycle sends conditional requests and reads validators and deprecation flags.
    assert set(cors["accessControlAllowHeaders"]) == {
        "Authorization",
        "Content-Type",
        "Accept",
        "If-Match",
        "If-None-Match",
    }
    assert set(cors["accessControlExposeHeaders"]) == {
        "X-Request-ID",
        "Location",
        "ETag",
        "Deprecation",
        "Sunset",
    }


def test_identity_like_request_headers_are_stripped(
    middlewares: dict[str, dict[str, Any]],
) -> None:
    stripped = middlewares["strip-identity-headers"]["headers"]["customRequestHeaders"]
    assert set(stripped) == {"X-User", "X-Forwarded-User", "X-Remote-User", "X-Principal"}
    assert set(stripped.values()) == {""}


def test_tls_enforces_tls_12_or_newer() -> None:
    tls = _load(GATEWAY / "dynamic" / "tls.yml")["tls"]
    assert tls["options"]["default"]["minVersion"] == "VersionTLS12"
