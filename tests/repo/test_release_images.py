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
SDK_WORKFLOW = ROOT / ".github" / "workflows" / "sdk.yml"
SDK = ROOT / "clients" / "python"
CHART = ROOT / "deploy" / "helm" / "contractum"
PLATFORM_IMAGES = {"identity", "contracts", "validation", "audit", "mail", "api-docs", "ui"}


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _workflow() -> dict[str, Any]:
    workflow: dict[str, Any] = _yaml(RELEASE)
    return workflow


# --- release workflow ---------------------------------------------------------------------------


def _steps(job: str) -> dict[str, dict[str, Any]]:
    steps: list[dict[str, Any]] = _workflow()["jobs"][job]["steps"]
    return {step.get("name", step.get("uses", "")): step for step in steps}


def test_release_runs_only_by_hand_with_a_mode() -> None:
    # PyYAML reads the `on` key as True. No tag push trigger: the workflow creates the tag itself.
    triggers = _workflow()[True]
    assert set(triggers) == {"workflow_dispatch"}
    mode = triggers["workflow_dispatch"]["inputs"]["mode"]
    assert mode["type"] == "choice"
    assert mode["options"] == ["pre-release", "promote"]
    assert _workflow()["permissions"] == {"contents": "read"}
    assert _workflow()["concurrency"]["cancel-in-progress"] is False


def test_least_privilege_per_job() -> None:
    jobs = _workflow()["jobs"]
    assert jobs["plan"]["permissions"] == {"contents": "read", "actions": "read"}
    publish = {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert jobs["image"]["permissions"] == publish
    assert jobs["chart"]["permissions"] == publish
    assert jobs["cli-image"]["permissions"] == publish
    # The wheel and the sdist are attested, not pushed: no packages: write.
    assert jobs["sdk"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    assert jobs["release"]["permissions"] == {"contents": "write"}
    assert jobs["image"]["needs"] == "plan"
    assert jobs["sdk"]["needs"] == "plan"
    assert jobs["cli-image"]["needs"] == "plan"
    assert jobs["chart"]["needs"] == ["plan", "image"]
    assert jobs["release"]["needs"] == ["plan", "image", "chart", "sdk", "cli-image"]


def test_no_expression_is_expanded_inside_a_shell_script() -> None:
    # Inputs and step outputs reach `run:` through env variables only (no script injection).
    for name, job in _workflow()["jobs"].items():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), (name, step.get("name"))


def test_the_plan_computes_the_version_gates_on_the_three_workflows_and_writes_notes() -> None:
    steps = _steps("plan")
    assert "refs/heads/main" in steps["Runs on main only"]["run"]
    checkout = steps["actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"]["with"]
    assert checkout["fetch-depth"] == 0  # every tag and commit since the last stable release
    version = steps["Next version"]
    assert version["env"] == {"MODE": "${{ inputs.mode }}"}
    assert 'scripts/release/next_version.py --mode "$MODE"' in version["run"]
    build = steps["Build identity of the released commit"]["run"]
    assert "git rev-parse --short=12" in build
    assert "git log -1 --format=%cI" in build
    gate = steps["The commit passed CI, E2E Nightly and DAST Nightly"]["run"]
    for workflow in ("ci.yml", "e2e-nightly.yml", "dast-nightly.yml"):
        assert workflow in gate
        assert (ROOT / ".github" / "workflows" / workflow).is_file()
    assert 'head_sha="$SHA"' in gate
    assert "status=success" in gate
    assert "scripts/release/changelog.py" in steps["Release notes"]["run"]
    outputs = _workflow()["jobs"]["plan"]["outputs"]
    assert outputs["python-version"] == "${{ steps.version.outputs.python-version }}"


def test_ci_can_be_run_by_hand_so_a_skip_ci_head_can_pass_the_gate() -> None:
    assert "workflow_dispatch" in _yaml(ROOT / ".github" / "workflows" / "ci.yml")[True]
    for nightly in ("e2e-nightly.yml", "dast-nightly.yml"):
        assert "workflow_dispatch" in _yaml(ROOT / ".github" / "workflows" / nightly)[True]


def test_the_image_job_publishes_every_platform_image_to_ghcr_with_attestations() -> None:
    job = _workflow()["jobs"]["image"]
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
    steps = _steps("image")
    checkout = steps["actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"]["with"]
    assert checkout["ref"] == "${{ needs.plan.outputs.sha }}"
    assert "image=ghcr.io/${OWNER,,}/contractum/${NAME}" in steps["Image name"]["run"]
    build = steps["Build and push the image"]["with"]
    for arg in ("VERSION=", "GIT_SHA=", "BUILD_TIME="):
        assert arg in build["build-args"]
    assert build["tags"].endswith(":${{ needs.plan.outputs.version }}")
    assert build["push"] is True
    assert build["sbom"] is True
    assert build["provenance"] == "mode=max"
    assert build["no-cache"] is True
    attest = steps["Attest build provenance"]
    assert attest["uses"].startswith("actions/attest@")
    assert attest["with"]["push-to-registry"] is True


def test_the_chart_job_packages_the_version_with_a_checked_helm_and_attests_it() -> None:
    steps = _steps("chart")
    helm = steps["Install Helm"]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", helm["env"]["HELM_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", helm["env"]["HELM_SHA256"])
    assert "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" in helm["run"]
    assert "sha256sum -c -" in helm["run"]
    package = steps["Package the chart"]["run"]
    assert '--version "$VERSION" --app-version "$VERSION"' in package
    push = steps["Push the chart to ghcr.io"]["run"]
    assert "oci://ghcr.io/${OWNER,,}/charts" in push
    assert "--password-stdin" in push
    attest = steps["Attest the chart archive"]
    assert attest["uses"].startswith("actions/attest@")
    assert attest["with"]["subject-path"].startswith("dist/contractum-")


def test_the_release_job_creates_the_tag_and_marks_candidates_as_pre_releases() -> None:
    script = _steps("release")["Create the tag and the GitHub Release"]["run"]
    assert 'gh release create "$TAG"' in script
    assert '--target "$SHA"' in script
    assert "--prerelease --latest=false" in script
    assert "flags=(--latest)" in script
    assert "notes/release-notes.md" in script
    for asset in (
        '"assets/contractum-${VERSION}.tgz"',
        '"assets/contractum-${PY_VERSION}-py3-none-any.whl"',
        '"assets/contractum-${PY_VERSION}.tar.gz"',
        "assets/SHA256SUMS",
    ):
        assert asset in script, asset
    sums = _steps("release")["Check the SDK files and write SHA256SUMS of every attached file"]
    assert "sha256sum -c SHA256SUMS" in sums["run"]
    assert "sha256sum -- * > SHA256SUMS" in sums["run"]


def test_the_sdk_job_builds_the_release_version_from_the_released_commit_and_attests_it() -> None:
    steps = _steps("sdk")
    checkout = steps["actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"]["with"]
    assert checkout["ref"] == "${{ needs.plan.outputs.sha }}"
    setup_uv = next(step for name, step in steps.items() if name.startswith("astral-sh/setup-uv@"))
    assert setup_uv["with"]["enable-cache"] is False  # nothing from the Actions cache in a release
    build = steps["Build and check the wheel and the sdist"]
    assert build["env"] == {"PY_VERSION": "${{ needs.plan.outputs.python-version }}"}
    assert build["run"] == 'bash scripts/build_release.sh "$PY_VERSION"'
    attest = steps["Attest the wheel and the sdist"]
    assert attest["uses"].startswith("actions/attest@")
    assert attest["with"]["subject-checksums"] == "clients/python/dist/SHA256SUMS"
    upload = next(s for n, s in steps.items() if n.startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "sdk-dist"


def test_the_cli_image_carries_the_version_is_smoke_tested_and_is_latest_only_when_stable() -> None:
    steps = _steps("cli-image")
    assert steps["Set the release version"]["run"] == (
        'bash scripts/build_release.sh --set-only "$PY_VERSION"'
    )
    assert "image=ghcr.io/${OWNER,,}/contractum-cli" in steps["Image name"]["run"]
    smoke = steps["Build the image for the smoke test"]["with"]
    assert (smoke["load"], smoke["push"], smoke["no-cache"]) == (True, False, True)
    assert smoke["build-args"] == "VERSION=${{ needs.plan.outputs.version }}"
    reports = steps["Image reports the release version"]["run"]
    assert '[ "$out" = "contractum ${PY_VERSION}" ]' in reports
    fixtures = steps["Image smoke, conforming data exits 0, non-conforming data exits 1"]["run"]
    assert '[ "$good" -eq 0 ] && [ "$bad" -eq 1 ]' in fixtures
    push = steps["Build and push the image"]["with"]
    assert push["build-args"] == smoke["build-args"]
    assert (push["context"], push["file"]) == (smoke["context"], smoke["file"])
    assert (push["push"], push["sbom"], push["provenance"]) == (True, True, "mode=max")
    tags = push["tags"].splitlines()
    assert tags[0] == "${{ steps.meta.outputs.image }}:${{ needs.plan.outputs.version }}"
    assert tags[1] == (
        "${{ needs.plan.outputs.prerelease == 'false' && "
        "format('{0}:latest', steps.meta.outputs.image) || '' }}"
    )
    attest = steps["Attest build provenance"]["with"]
    assert attest["push-to-registry"] is True


def test_the_release_workflow_is_the_only_way_to_release_the_sdk() -> None:
    workflows = ROOT / ".github" / "workflows"
    assert not (workflows / "sdk-release.yml").exists()
    for path in workflows.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "contractum-v" not in text, path.name
        if path != RELEASE:
            assert "gh release create" not in text, path.name
            assert "packages: write" not in text, path.name


def test_the_sdk_workflow_dry_runs_the_release_build_without_publishing() -> None:
    workflow = _yaml(SDK_WORKFLOW)
    assert "pull_request" in workflow[True]
    job = workflow["jobs"]["sdk-package"]
    assert "permissions" not in job  # the workflow's contents: read
    assert workflow["permissions"] == {"contents": "read"}
    steps = {step.get("name", step.get("uses", "")): step for step in job["steps"]}
    assert steps["Build and check the release files (version 0.0.0rc1)"]["run"] == (
        "bash scripts/build_release.sh 0.0.0rc1"
    )
    image = steps["Build the CLI image (not pushed)"]["with"]
    assert (image["push"], image["load"]) == (False, True)
    assert "contractum 0.0.0rc1" in steps["Image reports the release version"]["run"]
    assert "sdk-package" in workflow["jobs"]["sdk-ok"]["needs"]


def test_the_committed_sdk_version_is_the_dev_placeholder() -> None:
    project = (SDK / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'(?m)^version = "0\.0\.0\.dev0"$', project)
    script = (SDK / "scripts" / "build_release.sh").read_text(encoding="utf-8")
    assert 'uv version "$version" --no-sync' in script
    assert "git diff --numstat -- uv.lock" in script
    assert "License-Expression: Apache-2.0" in script
    assert "Noncommercial[ ]License" in script


PLATFORM_DOCKERFILES = [
    "infra/docker/service.Dockerfile",
    "infra/docker/ui.Dockerfile",
    "infra/docker/api-docs.Dockerfile",
]


@pytest.mark.parametrize("dockerfile", PLATFORM_DOCKERFILES)
def test_every_platform_dockerfile_takes_the_commit_and_labels_the_image(dockerfile: str) -> None:
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    for arg in ("GIT_SHA", "BUILD_TIME"):
        assert re.search(rf"(?m)^ARG {arg}=dev$", text), (dockerfile, arg)
    for label in ("title", "revision", "created", "source"):
        assert f"org.opencontainers.image.{label}=" in text, (dockerfile, label)
    assert 'org.opencontainers.image.source="https://github.com/rkisliak/contractum"' in text


@pytest.mark.parametrize("dockerfile", ["infra/docker/api-docs.Dockerfile"])
def test_a_platform_image_holds_no_release_version(dockerfile: str) -> None:
    # One build is tested and then released unchanged, so the version cannot be part of it: it
    # arrives at startup (DC_VERSION, DC_UI_VERSION).
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s*ARG VERSION\b", text), dockerfile
    assert "DC_VERSION" not in re.sub(r"(?m)^\s*#.*$", "", text), dockerfile
    assert 'org.opencontainers.image.version="${VERSION}"' not in text, dockerfile
    assert "${VERSION}" not in text, dockerfile


def test_every_action_in_the_release_workflow_is_pinned_by_sha() -> None:
    uses = re.findall(r"uses:\s*(\S+)", RELEASE.read_text(encoding="utf-8"))
    assert uses
    allowed = (
        "actions/",
        "astral-sh/setup-uv@",
        "docker/setup-buildx-action@",
        "docker/build-push-action@",
    )
    assert "azure/setup-helm" not in RELEASE.read_text(encoding="utf-8")
    for ref in uses:
        assert ref.startswith(allowed), ref
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref


def test_the_chart_default_registry_is_where_the_release_publishes() -> None:
    values = _yaml(CHART / "values.yaml")["global"]
    assert values["imageRegistry"] == "ghcr.io/rkisliak"
    # Empty: the chart's appVersion, which the release workflow sets to the released version.
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
