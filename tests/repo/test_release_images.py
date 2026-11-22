"""Release images, their trusted registry and the non-root test runners (code scanning roots).

- KSV-0125: the chart's platform images come from ghcr.io/rkisliak, where
  .github/workflows/release.yml publishes them, and infra/trivy tells Trivy's KSV-0125 to trust
  that registry host. The data is configuration for the check, never an ignore.
- DS-0002: the e2e and Playwright runner images end on a non-root USER, and compose.test.yaml
  runs them as a non-root user with only the groups they need (scripts/e2e-runner-env.sh).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
CHART = ROOT / "deploy" / "helm" / "contractum"
PLATFORM_IMAGES = {"identity", "contracts", "validation", "audit", "mail", "api-docs", "ui"}


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _workflow() -> dict[str, Any]:
    workflow: dict[str, Any] = _yaml(RELEASE)
    return workflow


# --- release workflow ---------------------------------------------------------------------------


def test_release_runs_only_on_a_version_tag_or_by_hand() -> None:
    # PyYAML reads the `on` key as True.
    triggers = _workflow()[True]
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v[0-9]*"]}
    assert _workflow()["permissions"] == {"contents": "read"}


def test_the_version_job_checks_the_tag_against_the_chart_app_version() -> None:
    job = _workflow()["jobs"]["version"]
    assert job["permissions"] == {"contents": "read"}
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert "refs/tags/v[0-9]*" in script
    assert "deploy/helm/contractum/Chart.yaml" in script
    assert "appVersion" in script


def test_the_image_job_publishes_every_platform_image_to_ghcr_with_attestations() -> None:
    job = _workflow()["jobs"]["image"]
    assert job["needs"] == "version"
    assert job["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    matrix = {entry["name"]: entry for entry in job["strategy"]["matrix"]["include"]}
    assert set(matrix) == PLATFORM_IMAGES
    # The same build inputs as ci.yml's docker-build matrix, which proves them on every PR.
    ci = _yaml(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]["docker-build"]
    for entry in ci["strategy"]["matrix"]["include"]:
        built = matrix[entry["name"]]
        assert (built["context"], built["file"], built["target"]) == (
            entry["context"],
            entry["file"],
            entry["target"],
        ), entry["name"]
    steps = {step.get("name", step.get("uses", "")): step for step in job["steps"]}
    meta = steps["Image name and build metadata"]["run"]
    assert "image=ghcr.io/${OWNER,,}/contractum/${NAME}" in meta
    build = steps["Build and push the image"]["with"]
    assert build["push"] is True
    assert build["sbom"] is True
    assert build["provenance"] == "mode=max"
    assert build["no-cache"] is True
    assert "org.opencontainers.image.source=" in build["labels"]
    attest = steps["Attest build provenance"]
    assert attest["uses"].startswith("actions/attest@")
    assert attest["with"]["push-to-registry"] is True


def test_every_action_in_the_release_workflow_is_pinned_by_sha() -> None:
    uses = re.findall(r"uses:\s*(\S+)", RELEASE.read_text(encoding="utf-8"))
    assert uses
    allowed = ("actions/", "docker/setup-buildx-action@", "docker/build-push-action@")
    for ref in uses:
        assert ref.startswith(allowed), ref
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref


def test_the_chart_default_registry_is_where_the_release_publishes() -> None:
    values = _yaml(CHART / "values.yaml")["global"]
    assert values["imageRegistry"] == "ghcr.io/rkisliak"
    # Empty: the chart's appVersion, the tag the release workflow checks and publishes.
    assert values["imageTag"] == ""


# --- Trivy check data ---------------------------------------------------------------------------


def test_trivy_trusts_exactly_the_release_registry_host() -> None:
    data = _yaml(ROOT / "infra" / "trivy" / "ksv0125.yaml")
    assert data == {"ksv0125": {"trusted_registries": ["ghcr.io"]}}
    registry = _yaml(CHART / "values.yaml")["global"]["imageRegistry"]
    assert registry.split("/")[0] in data["ksv0125"]["trusted_registries"]


def test_the_trivy_misconfiguration_scan_reads_the_check_data_and_no_ignore() -> None:
    security = (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    (scan,) = [line for line in security.splitlines() if "--scanners vuln,secret,misconfig" in line]
    assert "--config-data infra/trivy" in scan
    ignores = [
        line
        for line in (ROOT / ".trivyignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert ignores == []


# --- non-root test runners ----------------------------------------------------------------------


def _final_user(dockerfile: str) -> str:
    users = re.findall(r"(?m)^USER\s+(\S+)", (ROOT / dockerfile).read_text(encoding="utf-8"))
    assert users, dockerfile
    return users[-1]


@pytest.mark.parametrize(
    ("dockerfile", "user"),
    [
        ("infra/docker/e2e.Dockerfile", "10001:10001"),
        ("infra/docker/playwright.Dockerfile", "pwuser"),
    ],
)
def test_the_runner_images_end_on_a_non_root_user(dockerfile: str, user: str) -> None:
    assert _final_user(dockerfile) == user


def test_the_playwright_workdir_is_writable_by_the_runner_group() -> None:
    # Playwright removes its bind-mounted outputDir (/app/test-results) before a run: a non-root
    # runner needs write access to /app or the run stops with EACCES.
    text = (ROOT / "infra/docker/playwright.Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"(?m)^RUN chgrp 10001 /app && chmod 0775 /app$", text)
    assert text.index("chmod 0775 /app") < text.index("USER pwuser")


def _runner(name: str) -> dict[str, Any]:
    service: dict[str, Any] = _yaml(ROOT / "compose.test.yaml")["services"][name]
    return service


@pytest.mark.parametrize(("name", "default"), [("e2e", "10001"), ("ui-e2e", "pwuser")])
def test_the_runners_run_as_the_host_user_or_their_image_user(name: str, default: str) -> None:
    service = _runner(name)
    assert service["user"] == f"${{DC_E2E_UID:-{default}}}:${{DC_E2E_GID:-{default}}}"
    # The group 10001 may read the e2e secrets (infra/dev-init/generate.py GROUP_READABLE).
    assert "10001" in service["group_add"]


def test_only_the_e2e_runner_joins_the_docker_socket_group() -> None:
    assert _runner("e2e")["group_add"] == ["10001", "${DC_DOCKER_GID:-65534}"]
    assert _runner("ui-e2e")["group_add"] == ["10001"]
    sockets = [
        mount
        for mount in _runner("e2e")["volumes"]
        if mount.get("source") == "/var/run/docker.sock"
    ]
    assert len(sockets) == 1
    assert all(m.get("source") != "/var/run/docker.sock" for m in _runner("ui-e2e")["volumes"])


def test_every_runner_script_sets_the_runner_identity_before_it_starts_one() -> None:
    env = (ROOT / "scripts" / "e2e-runner-env.sh").read_text(encoding="utf-8")
    assert 'if [ "$uid" = "0" ]; then' in env
    for script in ("scripts/e2e.sh", "scripts/e2e-ui.sh", "run.sh"):
        text = (ROOT / script).read_text(encoding="utf-8")
        source = text.index("source scripts/e2e-runner-env.sh")
        assert source < text.index("--profile test"), script
    assert "DC_DOCKER_GID" in (ROOT / "run.ps1").read_text(encoding="utf-8")
