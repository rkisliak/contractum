"""Compose topology rules, asserted on the resolved ``docker compose config`` (no containers)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = "infra/docker/service.Dockerfile"
# The one image built from its own Dockerfile: stdlib only, no dc_* package (pinned below).
API_DOCS_DOCKERFILE = "infra/docker/api-docs.Dockerfile"
# The web UI image: a Node build stage and an unprivileged nginx runtime, context ./ui.
UI_DOCKERFILE = "infra/docker/ui.Dockerfile"
APP_SERVICES = ("identity", "contracts", "validation", "audit")
LONG_RUNNING_WITH_HEALTHCHECK = ("postgres", "nats", "traefik", "ui", *APP_SERVICES)


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["docker", "compose", "config", "--format", "json"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"docker compose config failed: {result.stderr}")
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def _services(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = config["services"]
    return services


def test_only_traefik_publishes_a_port_and_it_is_8443_to_443(config: dict[str, Any]) -> None:
    with_ports = {name: svc["ports"] for name, svc in _services(config).items() if svc.get("ports")}
    assert list(with_ports) == ["traefik"]
    assert len(with_ports["traefik"]) == 1
    mapping = with_ports["traefik"][0]
    assert str(mapping["published"]) == "8443"
    assert mapping["target"] == 443


def test_app_images_are_built_from_the_service_dockerfile_with_their_package(
    config: dict[str, Any],
) -> None:
    services = _services(config)
    built_images: set[str] = set()
    for name, svc in services.items():
        build = svc.get("build")
        if not build:
            continue
        # Every platform image takes the release build identity ("dev" unless the caller sets it).
        assert {"VERSION", "GIT_SHA", "BUILD_TIME"} <= set(build.get("args") or {}), name
        if name == "api-docs":
            assert build["dockerfile"] == API_DOCS_DOCKERFILE
            assert set(build["args"]) == {"VERSION", "GIT_SHA", "BUILD_TIME"}
            continue
        if name == "ui":
            assert Path(build["context"]).name == "ui"
            assert (Path(build["context"]) / build["dockerfile"]).resolve() == (
                ROOT / UI_DOCKERFILE
            )
            assert build["target"] == "runtime"
            continue
        assert build["dockerfile"] == DOCKERFILE, name
        assert build["args"]["PACKAGE"] == f"dc-{name}", name
        built_images.add(svc["image"])
    assert {n for n in APP_SERVICES} <= {n for n, s in services.items() if s.get("build")}
    # Every service that runs a dc_* package must use an image built that way.
    for name, svc in services.items():
        command = svc.get("command") or []
        text = " ".join(command) if isinstance(command, list) else str(command)
        if "dc_" in text:
            assert svc["image"] in built_images, name


@pytest.mark.parametrize("service", APP_SERVICES)
def test_app_services_wait_for_their_migrate_job_and_dev_init(
    config: dict[str, Any], service: str
) -> None:
    depends = _services(config)[service]["depends_on"]
    assert depends[f"{service}-migrate"]["condition"] == "service_completed_successfully"
    assert depends["dev-init"]["condition"] == "service_completed_successfully"


@pytest.mark.parametrize("service", APP_SERVICES)
def test_migrate_jobs_wait_for_postgres_and_dev_init(config: dict[str, Any], service: str) -> None:
    migrate = _services(config)[f"{service}-migrate"]
    assert migrate["restart"] == "no"
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert migrate["depends_on"]["dev-init"]["condition"] == "service_completed_successfully"


def test_postgres_and_nats_dependencies_use_service_healthy(config: dict[str, Any]) -> None:
    for service, svc in _services(config).items():
        for dep, spec in (svc.get("depends_on") or {}).items():
            if dep in {"postgres", "nats"}:
                assert spec["condition"] == "service_healthy", f"{service} -> {dep}"
            if dep == "dev-init" or dep.endswith("-migrate"):
                assert spec["condition"] == "service_completed_successfully", f"{service} -> {dep}"


def test_no_service_waits_on_another_with_a_fixed_delay(config: dict[str, Any]) -> None:
    for name, svc in _services(config).items():
        command = svc.get("command") or []
        text = " ".join(command) if isinstance(command, list) else str(command)
        assert "sleep" not in text, name
        for part in (svc.get("healthcheck") or {}).get("test", []):
            assert "sleep" not in str(part), name


@pytest.mark.parametrize("service", LONG_RUNNING_WITH_HEALTHCHECK)
def test_long_running_services_define_healthchecks(config: dict[str, Any], service: str) -> None:
    healthcheck = _services(config)[service].get("healthcheck")
    assert healthcheck and healthcheck.get("test"), service


@pytest.mark.parametrize("service", ["postgres", "nats"])
def test_postgres_and_nats_are_only_on_the_backend_network(
    config: dict[str, Any], service: str
) -> None:
    assert set(_services(config)[service]["networks"]) == {"backend"}


def test_backend_is_internal_and_edge_has_the_fixed_subnet(config: dict[str, Any]) -> None:
    assert config["networks"]["backend"]["internal"] is True
    assert not config["networks"]["edge"].get("internal")
    subnets = [c["subnet"] for c in config["networks"]["edge"]["ipam"]["config"]]
    assert subnets == ["172.28.0.0/24"]
    assert _services(config)["traefik"]["networks"]["edge"]["ipv4_address"] == "172.28.0.10"


def test_app_services_trust_forwarded_headers_only_from_traefik(config: dict[str, Any]) -> None:
    for service in APP_SERVICES:
        command = _services(config)[service]["command"]
        index = command.index("--forwarded-allow-ips")
        assert command[index + 1] == "172.28.0.10", service
        assert "--proxy-headers" in command, service


def test_every_secret_mount_is_a_read_only_subpath_of_dev_secrets(config: dict[str, Any]) -> None:
    for name, svc in _services(config).items():
        for volume in svc.get("volumes") or []:
            if volume.get("target") == "/run/secrets":
                assert volume["source"] == "dev-secrets", name
                assert volume.get("read_only") is True, name
                assert volume["volume"]["subpath"], name


def test_no_secret_value_or_default_is_written_in_compose() -> None:
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD:" not in text
    assert "POSTGRES_PASSWORD=" not in text
    assert "POSTGRES_PASSWORD_FILE" in text
    assert "docker.sock" not in text


def test_problem_responder_is_edge_only_unpublished_and_healthchecked(
    config: dict[str, Any],
) -> None:
    responder = _services(config)["problem-responder"]
    assert set(responder["networks"]) == {"edge"}
    assert not responder.get("ports")
    assert responder["healthcheck"]["test"]
    assert not responder.get("build")  # reuses the identity image, adds no image of its own
    assert responder["image"] == _services(config)["identity"]["image"]


def test_traefik_waits_for_the_problem_responder_to_be_healthy(config: dict[str, Any]) -> None:
    depends = _services(config)["traefik"]["depends_on"]
    assert depends["problem-responder"]["condition"] == "service_healthy"


def test_fake_graph_is_backend_only_unpublished_and_healthchecked(
    config: dict[str, Any],
) -> None:
    fake = _services(config)["fake-graph"]
    assert set(fake["networks"]) == {"backend"}
    assert not fake.get("ports")
    assert fake["healthcheck"]["test"]
    assert not fake.get("build")  # reuses the identity image, adds no image of its own
    assert fake["image"] == _services(config)["identity"]["image"]
    assert fake["depends_on"]["dev-init"]["condition"] == "service_completed_successfully"


def test_fake_graph_mounts_only_its_own_read_only_secret_subpath(config: dict[str, Any]) -> None:
    fake = _services(config)["fake-graph"]
    secret_mounts = [v for v in fake["volumes"] if v.get("target") == "/run/secrets"]
    assert len(secret_mounts) == 1
    assert secret_mounts[0]["source"] == "dev-secrets"
    assert secret_mounts[0]["read_only"] is True
    assert secret_mounts[0]["volume"]["subpath"] == "fake-graph"
    # Its application directory is read-only too, and nothing else is mounted.
    assert {v["target"] for v in fake["volumes"]} == {"/run/secrets", "/opt/fake-graph"}
    assert all(v.get("read_only") is True for v in fake["volumes"])


def test_identity_waits_for_the_fake_graph_and_points_its_real_client_at_it(
    config: dict[str, Any],
) -> None:
    identity = _services(config)["identity"]
    assert identity["depends_on"]["fake-graph"]["condition"] == "service_healthy"
    env = identity["environment"]
    assert env["DC_GRAPH_BASE_URL"] == "http://fake-graph:8080/v1.0"
    assert (
        env["DC_GRAPH_TOKEN_URL_TEMPLATE"] == "http://fake-graph:8080/{tenant_id}/oauth2/v2.0/token"
    )
    assert env["DC_GRAPH_CLIENT_ID"] == "identity-graph-dev"
    # The client secret is a file under /run/secrets (the setting's default), never an env value.
    assert not any("GRAPH_CLIENT_SECRET" in key for key in env)


def test_identity_reaches_contracts_and_validation_without_depending_on_them(
    config: dict[str, Any],
) -> None:
    """Quick 261119-0v1: a folder delete asks both at request time; a depends_on is a cycle."""
    identity = _services(config)["identity"]
    env = identity["environment"]
    assert env["DC_CONTRACTS_URL"] == "http://contracts:8000"
    assert env["DC_VALIDATION_URL"] == "http://validation:8000"
    assert "contracts" not in identity["depends_on"]
    assert "validation" not in identity["depends_on"]
    helm = (ROOT / "deploy/helm/contractum/templates/services.yaml").read_text(encoding="utf-8")
    assert 'value: "http://contracts:8000"' in helm
    assert 'value: "http://validation:8000"' in helm


def test_no_graph_secret_value_is_written_in_compose() -> None:
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "FAKE_GRAPH_CLIENT_SECRET" not in text
    assert "DC_GRAPH_CLIENT_SECRET" not in text


DEV_TENANT = "11111111-2222-4333-8444-555555555555"
ENTRA_PERSONAS = ("alice", "bob", "carol", "pipeline", "alice-idtoken", "alice-wrong-aud")
# SPA personas: selected by client_id (the browser flow cannot send a persona scope), same claims.
SPA_PERSONAS = {"ui-alice": "alice", "ui-bob": "bob"}


def _entra_mappings() -> list[dict[str, Any]]:
    document = json.loads((ROOT / "infra/mock-oidc/config.json").read_text(encoding="utf-8"))
    callbacks = [c for c in document["tokenCallbacks"] if c["issuerId"] == "entra"]
    assert len(callbacks) == 1
    mappings: list[dict[str, Any]] = callbacks[0]["requestMappings"]
    return mappings


def test_every_entra_persona_mapping_sets_the_claims_the_mock_takes_from_it() -> None:
    mappings = _entra_mappings()
    assert {m["match"] for m in mappings} == {*ENTRA_PERSONAS, *SPA_PERSONAS}
    for mapping in mappings:
        claims = mapping["claims"]
        for name in ("sub", "aud", "ver", "tid", "oid"):
            assert claims.get(name), (mapping["match"], name)
        assert claims["tid"] == DEV_TENANT, mapping["match"]
        assert claims["ver"] == "2.0", mapping["match"]


def test_the_mock_keeps_interactive_login_off_and_the_spa_personas_mirror_alice_and_bob() -> None:
    document = json.loads((ROOT / "infra/mock-oidc/config.json").read_text(encoding="utf-8"))
    # With interactiveLogin on, typed claims are ignored and `tid` is overwritten (A-3).
    assert document["interactiveLogin"] is False
    by_scope = {m["match"]: m for m in _entra_mappings() if m["requestParam"] == "scope"}
    by_client = {m["match"]: m for m in _entra_mappings() if m["requestParam"] == "client_id"}
    assert set(by_client) == set(SPA_PERSONAS)
    for client_id, persona in SPA_PERSONAS.items():
        assert by_client[client_id]["claims"] == by_scope[persona]["claims"], client_id


def test_the_default_mock_issuer_still_serves_the_generic_dev_token() -> None:
    document = json.loads((ROOT / "infra/mock-oidc/config.json").read_text(encoding="utf-8"))
    default = next(c for c in document["tokenCallbacks"] if c["issuerId"] == "default")
    assert default["requestMappings"][0]["claims"]["sub"] == "e2e-non-admin"


ENTRA_BROWSER_ISSUER = "https://localhost:8443/entra"
DEV_MOCK_ROUTER_FILE = "gateway/dynamic/dev-mock-oidc.yml"


def test_every_service_trusts_the_entra_mock_through_one_issuers_entry(
    config: dict[str, Any],
) -> None:
    for service in APP_SERVICES:
        issuers = json.loads(_services(config)[service]["environment"]["DC_AUTH_ISSUERS"])
        entra = [i for i in issuers if i.get("kind") == "entra"]
        assert len(entra) == 1, service
        # The browser-visible issuer (the SPA runs PKCE against it through the gateway), with the
        # JWKS fetched in-network. A second entry for the tenant is refused by IssuerRegistry.
        assert entra[0]["issuer"] == ENTRA_BROWSER_ISSUER
        assert entra[0]["jwks_uri"] == "http://mock-oidc:8080/entra/jwks"
        assert entra[0]["tenant_id"] == DEV_TENANT
        assert entra[0]["namespace"] == "entra"


def test_the_ui_service_publishes_the_entra_stand_in_with_public_identifiers_only(
    config: dict[str, Any],
) -> None:
    env = _services(config)["ui"]["environment"]
    assert env["DC_UI_ENTRA_ENABLED"] == "true"
    assert env["DC_UI_ENTRA_CLIENT_ID"] in SPA_PERSONAS
    assert env["DC_UI_ENTRA_AUTHORITY"] == ENTRA_BROWSER_ISSUER
    assert env["DC_UI_ENTRA_SCOPES"] == "openid profile offline_access"
    assert env["DC_UI_ENTRA_PROTOCOL"] == "oidc"
    assert not [key for key in env if key.lower().endswith(("password", "secret"))]


def test_the_mock_issuer_is_on_the_backend_and_edge_networks(config: dict[str, Any]) -> None:
    mock = _services(config)["mock-oidc"]
    assert set(mock["networks"]) == {"backend", "edge"}
    assert not mock.get("ports")


def _gateway_files() -> list[Path]:
    return [
        path
        for path in (ROOT / "gateway").rglob("*")
        if path.is_file() and path.suffix in {".yml", ".yaml", ".py"}
    ]


def test_the_gateway_never_routes_to_the_fake_graph() -> None:
    for path in _gateway_files():
        text = path.read_text(encoding="utf-8")
        for forbidden in ("fake-graph", "/_fake"):
            assert forbidden not in text, (path.name, forbidden)


def test_the_mock_issuer_appears_in_the_gateway_only_in_the_dev_only_file() -> None:
    holders = [
        path.relative_to(ROOT).as_posix()
        for path in _gateway_files()
        if "mock-oidc" in path.read_text(encoding="utf-8")
    ]
    assert holders == [DEV_MOCK_ROUTER_FILE]


def test_the_dev_mock_router_is_one_unstripped_entra_router_marked_dev_only() -> None:
    text = (ROOT / DEV_MOCK_ROUTER_FILE).read_text(encoding="utf-8")
    assert text.startswith("# DEV ONLY"), "first line must warn that the file is compose-only"
    document = yaml.safe_load(text)
    routers = document["http"]["routers"]
    assert list(routers) == ["mock-oidc-dev"]
    router = routers["mock-oidc-dev"]
    assert router["rule"] == "PathPrefix(`/entra/`)"
    assert router["middlewares"] == ["security-headers"]
    assert router["service"] == "mock-oidc"
    assert "stripPrefix" not in text
    assert "replacePath" not in text
    assert "middlewares" not in document["http"]
    servers = document["http"]["services"]["mock-oidc"]["loadBalancer"]["servers"]
    assert servers == [{"url": "http://mock-oidc:8080"}]


# --- Phase 04.1: the mail service, the mail catcher and the webhook sink -----------------------

DEV_ONLY_BACKEND = ("mail", "mailpit", "webhook-sink")


@pytest.mark.parametrize("service", DEV_ONLY_BACKEND)
def test_mail_mailpit_and_webhook_sink_are_backend_only_unpublished_and_healthchecked(
    config: dict[str, Any], service: str
) -> None:
    svc = _services(config)[service]
    assert set(svc["networks"]) == {"backend"}
    assert not svc.get("ports")
    assert svc["healthcheck"]["test"]


def test_mail_is_built_from_the_service_dockerfile_with_its_package_and_mounts_no_secret(
    config: dict[str, Any],
) -> None:
    mail = _services(config)["mail"]
    assert mail["build"]["dockerfile"] == DOCKERFILE
    assert mail["build"]["args"]["PACKAGE"] == "dc-mail"
    assert mail["image"] == "contractum/mail:dev"
    assert "dc_mail.main:create_mail_app" in mail["command"]
    assert not [v for v in mail.get("volumes") or [] if v.get("target") == "/run/secrets"]
    assert mail["depends_on"]["identity"]["condition"] == "service_healthy"
    assert mail["depends_on"]["dev-init"]["condition"] == "service_completed_successfully"
    env = mail["environment"]
    assert env["DC_SMTP_HOST"] == "mailpit"
    assert str(env["DC_SMTP_PORT"]) == "1025"
    assert env["DC_SMTP_TLS"] == "none"
    assert not any("PASSWORD" in key for key in env)


def test_mailpit_is_a_pinned_image_and_the_sink_reuses_the_identity_image(
    config: dict[str, Any],
) -> None:
    services = _services(config)
    assert services["mailpit"]["image"] == "axllent/mailpit:v1.34.0"
    sink = services["webhook-sink"]
    assert not sink.get("build")
    assert sink["image"] == services["identity"]["image"]
    assert not [v for v in sink["volumes"] if v.get("target") == "/run/secrets"]
    assert all(v.get("read_only") is True for v in sink["volumes"])


def test_contracts_reaches_mail_and_the_sink_without_depending_on_them(
    config: dict[str, Any],
) -> None:
    contracts = _services(config)["contracts"]
    for service in DEV_ONLY_BACKEND:
        # Delivery is asynchronous and must never gate startup.
        assert service not in contracts["depends_on"], service
    env = contracts["environment"]
    assert env["DC_MAIL_URL"] == "http://mail:8000"
    assert json.loads(env["DC_WEBHOOK_ALLOWED_PRIVATE_HOSTS"]) == ["webhook-sink"]
    assert env["DC_WEBHOOK_REQUIRE_HTTPS"] == "false"
    assert not any(key.endswith(("MAIL_API_KEY", "WEBHOOK_SECRET_KEY")) for key in env)


def test_contracts_reaches_validation_without_depending_on_it(
    config: dict[str, Any],
) -> None:
    contracts = _services(config)["contracts"]
    # A validation outage must never block contracts from starting: publish fails closed (503)
    # and saves degrade instead (D-131, D-134).
    assert "validation" not in contracts["depends_on"]
    assert "validation-migrate" not in contracts["depends_on"]
    assert contracts["environment"]["DC_VALIDATION_URL"] == "http://validation:8000"


def test_only_contracts_holds_the_webhook_key_and_the_notifier_key_is_never_in_compose() -> None:
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "dcp_" not in text
    assert "whsec_" not in text
    assert "DC_MAIL_API_KEY" not in text


def test_the_gateway_never_routes_to_the_mail_service_mail_catcher_or_webhook_sink() -> None:
    for path in (ROOT / "gateway").rglob("*"):
        if path.is_file() and path.suffix in {".yml", ".yaml", ".py"}:
            text = path.read_text(encoding="utf-8")
            for forbidden in ("mailpit", "webhook-sink", "/_sink", "mail:8000", "//mail"):
                assert forbidden not in text, (path.name, forbidden)


# --- Phase 7: the api-docs container (PLAT-06, D-176) -----------------------------------------


def test_api_docs_is_on_edge_and_backend_unpublished_and_healthchecked(
    config: dict[str, Any],
) -> None:
    api_docs = _services(config)["api-docs"]
    assert set(api_docs["networks"]) == {"edge", "backend"}
    assert not api_docs.get("ports")
    assert api_docs["healthcheck"]["test"]
    assert api_docs["image"] == "contractum/api-docs:dev"
    assert api_docs["build"]["dockerfile"] == API_DOCS_DOCKERFILE
    # It holds no secret: nothing from dev-secrets is mounted.
    assert not [v for v in api_docs.get("volumes") or [] if v.get("source") == "dev-secrets"]


def test_api_docs_waits_for_the_four_services_and_restarts_with_them(
    config: dict[str, Any],
) -> None:
    depends = _services(config)["api-docs"]["depends_on"]
    assert set(depends) == set(APP_SERVICES)
    for service in APP_SERVICES:
        assert depends[service]["condition"] == "service_healthy", service
        # A service Compose restarts or re-creates restarts api-docs, which merges again (IN-02:
        # a container's own restart policy does not, so the comment in compose.yaml says so).
        assert depends[service]["restart"] is True, service


def test_the_compose_project_name_is_pinned_and_the_e2e_runner_follows_it() -> None:
    # Read as raw YAML, not through `docker compose config`: a COMPOSE_PROJECT_NAME in the
    # developer's shell must not change the answer.
    base = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    overlay = yaml.safe_load((ROOT / "compose.test.yaml").read_text(encoding="utf-8"))
    assert base["name"] == "contractum"
    env = overlay["services"]["e2e"]["environment"]
    assert env["DC_E2E_COMPOSE_PROJECT"] == "${COMPOSE_PROJECT_NAME:-contractum}"


def test_the_e2e_runner_waits_for_api_docs() -> None:
    overlay = yaml.safe_load((ROOT / "compose.test.yaml").read_text(encoding="utf-8"))
    depends = overlay["services"]["e2e"]["depends_on"]
    assert depends["api-docs"]["condition"] == "service_healthy"


def test_the_e2e_denial_concurrency_default_matches_the_module_default() -> None:
    # G-07-11-2: 25 concurrent denials is identity's capacity limit; the overlay default must not
    # override the test module's own default with a larger value.
    overlay = yaml.safe_load((ROOT / "compose.test.yaml").read_text(encoding="utf-8"))
    env = overlay["services"]["e2e"]["environment"]
    assert env["DC_E2E_DENIAL_CONCURRENCY"] == "${DC_E2E_DENIAL_CONCURRENCY:-25}"
    module = (ROOT / "tests/e2e/test_phase7_denial_guarantee.py").read_text(encoding="utf-8")
    assert '_env_int("DC_E2E_DENIAL_CONCURRENCY", 25)' in module


# --- Phase 8: the ui container (UI-04, D-UI-08-04, D-UI-08-10; T-8-15) -------------------------


def test_ui_is_edge_only_unprivileged_read_only_and_holds_no_secret(
    config: dict[str, Any],
) -> None:
    ui = _services(config)["ui"]
    assert set(ui["networks"]) == {"edge"}
    assert not ui.get("ports")
    assert not ui.get("volumes")  # no secret mount, no bind mount, nothing writable but /tmp
    assert not ui.get("secrets")
    assert ui["read_only"] is True
    assert ui["tmpfs"] == ["/tmp"]  # noqa: S108 - a container path, not a host temp file
    assert ui["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in ui["security_opt"]
    assert ui["healthcheck"]["test"]
    assert ui["image"] == "contractum/ui:dev"
    assert ui["build"]["target"] == "runtime"
    # Public identifiers only: nothing in its environment looks like a credential.
    assert all(key.startswith("DC_UI_") for key in ui["environment"])
    assert not [
        key
        for key in ui["environment"]
        if any(word in key for word in ("SECRET", "PASSWORD", "TOKEN", "KEY"))
    ]


def test_traefik_waits_for_the_ui_to_be_healthy(config: dict[str, Any]) -> None:
    depends = _services(config)["traefik"]["depends_on"]
    assert depends["ui"]["condition"] == "service_healthy"


# --- Phase 8: the Playwright UI e2e runner (D-UI-08-32, A-11; T-8-16, T-8-17) ------------------

PLAYWRIGHT_DOCKERFILE = "infra/docker/playwright.Dockerfile"


def _ui_e2e_service() -> dict[str, Any]:
    overlay = yaml.safe_load((ROOT / "compose.test.yaml").read_text(encoding="utf-8"))
    service: dict[str, Any] = overlay["services"]["ui-e2e"]
    return service


def test_the_ui_e2e_image_tag_equals_the_playwright_pin() -> None:
    # T-8-17: the bundled browsers must be the ones the library version expects.
    package = json.loads((ROOT / "ui/package.json").read_text(encoding="utf-8"))
    version = package["devDependencies"]["@playwright/test"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), "the pin must be exact"
    dockerfile = (ROOT / PLAYWRIGHT_DOCKERFILE).read_text(encoding="utf-8")
    assert f"FROM mcr.microsoft.com/playwright:v{version}-noble" in dockerfile.splitlines()


def test_the_ui_e2e_runner_is_built_from_its_dockerfile_in_the_test_profile() -> None:
    service = _ui_e2e_service()
    assert service["profiles"] == ["test"]
    assert service["build"]["context"] == "./ui"
    assert service["build"]["dockerfile"] == f"../{PLAYWRIGHT_DOCKERFILE}"
    assert service["environment"]["DC_UI_E2E_BASE_URL"] == "https://localhost:8443"


def test_the_ui_e2e_runner_is_on_edge_only() -> None:
    # Every UI journey goes through the gateway like a browser (T-8-16).
    assert _ui_e2e_service()["networks"] == ["edge"]


def test_the_ui_e2e_runner_mounts_only_the_e2e_secrets_read_only_and_the_dev_bind() -> None:
    mounts = {mount["target"]: mount for mount in _ui_e2e_service()["volumes"]}
    secrets = mounts.pop("/run/secrets")
    assert secrets["source"] == "dev-secrets"
    assert secrets["read_only"] is True
    assert secrets["volume"]["subpath"] == "e2e"
    assert mounts.pop("/dev-out")["source"] == "./.dev"
    # The only other mount is the gitignored artifacts directory; never the Docker socket.
    assert set(mounts) == {"/app/test-results"}
    assert mounts["/app/test-results"]["source"] == "./ui/test-results"


def test_the_ui_e2e_runner_waits_for_the_gateway_identity_and_the_ui() -> None:
    depends = _ui_e2e_service()["depends_on"]
    assert set(depends) == {"traefik", "identity", "ui"}
    for name, condition in depends.items():
        assert condition["condition"] == "service_healthy", name
