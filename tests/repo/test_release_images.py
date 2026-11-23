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


PUBLISH = {
    "contents": "read",
    "packages": "write",
    "id-token": "write",
    "attestations": "write",
}
PRE_RELEASE = "needs.plan.outputs.prerelease == 'true'"
PROMOTE = "needs.plan.outputs.prerelease == 'false'"


def test_least_privilege_per_job() -> None:
    jobs = _workflow()["jobs"]
    assert jobs["plan"]["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["image"]["permissions"] == PUBLISH
    assert jobs["chart"]["permissions"] == PUBLISH
    assert jobs["cli-image"]["permissions"] == PUBLISH
    # The wheel and the sdist are attested, not pushed: no packages: write.
    attest = {"contents": "read", "id-token": "write", "attestations": "write"}
    assert jobs["sdk"]["permissions"] == attest
    # The tested record is attested, never pushed.
    assert jobs["tested"]["permissions"] == attest
    # Jobs that only read the registry get no write.
    assert jobs["images-manifest"]["permissions"] == {"contents": "read", "packages": "read"}
    assert jobs["verify-candidate"]["permissions"] == {
        "contents": "read",
        "packages": "read",
        "attestations": "read",
    }
    # The nightlies called with the release images pull them, and so need packages: read.
    for caller in ("e2e", "dast"):
        assert jobs[caller]["permissions"] == {
            "contents": "read",
            "actions": "read",
            "packages": "read",
        }
    # Retagging needs packages: write and nothing else.
    for tagger in ("promote-images", "latest"):
        assert jobs[tagger]["permissions"] == {"contents": "read", "packages": "write"}
    assert jobs["images-ready"]["permissions"] == {"contents": "read"}
    assert jobs["release"]["permissions"] == {"contents": "write"}
    holders = [name for name, job in jobs.items() if job["permissions"].get("contents") == "write"]
    assert holders == ["release"]
    writers = {name for name, job in jobs.items() if "packages" in job["permissions"]}
    assert {n for n in writers if jobs[n]["permissions"]["packages"] == "write"} == {
        "image",
        "chart",
        "cli-image",
        "promote-images",
        "latest",
    }


def test_the_job_graph_builds_once_tests_the_digests_and_promotes_them() -> None:
    jobs = _workflow()["jobs"]
    assert set(jobs) == {
        "plan",
        "image",
        "images-manifest",
        "e2e",
        "dast",
        "tested",
        "verify-candidate",
        "promote-images",
        "images-ready",
        "sdk",
        "cli-image",
        "chart",
        "release",
        "latest",
    }
    needs = {name: job.get("needs") for name, job in jobs.items()}
    assert needs == {
        "plan": None,
        "image": "plan",
        "images-manifest": ["plan", "image"],
        "e2e": ["plan", "images-manifest"],
        "dast": ["plan", "images-manifest"],
        "tested": ["plan", "images-manifest", "e2e", "dast"],
        "verify-candidate": "plan",
        "promote-images": ["plan", "verify-candidate"],
        "images-ready": ["plan", "tested", "promote-images"],
        "sdk": "plan",
        "cli-image": ["plan", "images-ready"],
        "chart": ["plan", "images-ready"],
        "release": ["plan", "images-ready", "chart", "sdk", "cli-image"],
        "latest": ["plan", "release"],
    }
    for name in ("image", "images-manifest", "e2e", "dast"):
        assert jobs[name]["if"] == PRE_RELEASE, name
    assert jobs["tested"]["if"] == (
        f"{PRE_RELEASE} && needs.e2e.result == 'success' && needs.dast.result == 'success'"
    )
    for name in ("verify-candidate", "promote-images"):
        assert jobs[name]["if"] == PROMOTE, name
    assert "if" not in jobs["sdk"]  # the SDK rebuilds in both modes


def test_a_job_behind_a_skipped_branch_is_not_skipped_with_it() -> None:
    # A job whose ancestor was skipped is skipped too, unless its own `if` says otherwise; the
    # chart, the CLI image, the release and latest each name the exact results they need.
    jobs = _workflow()["jobs"]
    ready = jobs["images-ready"]["if"]
    assert "!cancelled()" in ready
    assert "needs.plan.result == 'success'" in ready
    assert "needs.plan.outputs.prerelease == 'true' && needs.tested.result == 'success'" in ready
    assert (
        "needs.plan.outputs.prerelease == 'false' && needs.promote-images.result == 'success'"
        in ready
    )
    for name in ("cli-image", "chart"):
        assert jobs[name]["if"] == "${{ !cancelled() && needs.images-ready.result == 'success' }}"
    release = jobs["release"]["if"]
    assert "!cancelled()" in release
    for direct in ("plan", "images-ready", "chart", "sdk", "cli-image"):
        assert f"needs.{direct}.result == 'success'" in release, direct
    latest = jobs["latest"]["if"]
    assert "!cancelled()" in latest
    assert "needs.plan.outputs.prerelease == 'false'" in latest
    assert "needs.release.result == 'success'" in latest


def test_no_expression_is_expanded_inside_a_shell_script() -> None:
    # Inputs and step outputs reach `run:` through env variables only (no script injection).
    for name, job in _workflow()["jobs"].items():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", ""), (name, step.get("name"))


def _calls(command: str) -> list[str]:
    """The names of the steps (in every job) whose script runs ``command``."""
    return [
        f"{name}/{step.get('name', step.get('uses', ''))}"
        for name, job in _workflow()["jobs"].items()
        for step in job.get("steps", [])
        if command in step.get("run", "")
    ]


def _builds(job: str) -> list[str]:
    steps = _workflow()["jobs"][job].get("steps", [])
    return [
        step.get("name", step.get("uses", ""))
        for step in steps
        if "docker/build-push-action" in step.get("uses", "")
        or "docker build" in step.get("run", "")
        or "buildx build" in step.get("run", "")
    ]


def test_only_the_pre_release_builds_server_images_and_promote_builds_none() -> None:
    assert _builds("image") == ["Build and push the image"]
    for job in ("verify-candidate", "promote-images", "latest", "images-manifest", "tested"):
        assert _builds(job) == [], job
    # The server images are built nowhere else; the CLI image is the other build in the file.
    built = {name for name in _workflow()["jobs"] if _builds(name)}
    assert built == {"image", "cli-image"}


def test_the_release_workflow_calls_the_two_nightlies_on_the_built_images() -> None:
    jobs = _workflow()["jobs"]
    assert jobs["e2e"]["uses"] == "./.github/workflows/e2e-nightly.yml"
    assert jobs["e2e"]["with"] == {
        "full": True,
        "release-images": "release-images",
        "release-version": "${{ needs.plan.outputs.version }}",
    }
    assert jobs["dast"]["uses"] == "./.github/workflows/dast-nightly.yml"
    assert jobs["dast"]["with"] == {
        "release-images": "release-images",
        "release-version": "${{ needs.plan.outputs.version }}",
    }
    for name in ("e2e", "dast"):
        assert "steps" not in jobs[name]
        assert "secrets" not in jobs[name]  # no secret is passed to the test runs


def test_the_manifest_is_assembled_from_the_built_digests_and_the_tags_are_read_back() -> None:
    steps = _steps("images-manifest")
    download = steps["actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333"]["with"]
    assert (download["pattern"], download["merge-multiple"]) == ("digest-*", True)
    assemble = steps["Assemble the manifest of the built images"]
    assert "release_images.py assemble --digests digests" in assemble["run"]
    assert assemble["env"]["VERSION"] == "${{ needs.plan.outputs.version }}"
    assert assemble["env"]["REGISTRY"] == "${{ needs.plan.outputs.registry }}"
    verify = steps["Every candidate tag resolves to its recorded digest"]
    assert verify["env"]["DC_RELEASE_IMAGES"] == "release-images.json"
    assert verify["run"] == 'bash scripts/release/tested_images.sh verify-tags "$VERSION"'
    upload = next(s for n, s in steps.items() if n.startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "release-images"
    image = _steps("image")
    assert "digest-${{ matrix.name }}" in str(image)


def test_tested_needs_both_suites_and_attests_the_record_it_writes() -> None:
    steps = _steps("tested")
    record = steps["Record that E2E and DAST passed on these digests"]
    assert record["env"]["E2E_RESULT"] == "${{ needs.e2e.result }}"
    assert record["env"]["DAST_RESULT"] == "${{ needs.dast.result }}"
    assert '--e2e "$E2E_RESULT" --dast "$DAST_RESULT"' in record["run"]
    attest = steps["Attest the tested record"]
    assert attest["uses"].startswith("actions/attest@")
    assert attest["with"] == {"subject-path": "tested-images.json"}
    upload = next(s for n, s in steps.items() if n.startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "tested-images"
    # Only this job writes the record and only it attests it.
    assert _calls("release_images.py record") == [
        "tested/Record that E2E and DAST passed on these digests"
    ]


def test_verify_candidate_trusts_nothing_before_it_checks_everything() -> None:
    steps = _steps("verify-candidate")
    names = list(steps)
    published = steps["The candidate is a published pre-release"]
    assert published["env"]["CANDIDATE"] == "${{ needs.plan.outputs.candidate }}"
    assert "prerelease=true draft=false" in published["run"]
    assert "gh release view" in published["run"]
    download = steps["Download the tested record of the candidate"]
    assert "--pattern tested-images.json" in download["run"]
    assert "test -s candidate/tested-images.json" in download["run"]
    check = steps["The record is for this version, commit and registry, and both suites passed"]
    assert check["env"] == {
        "IMAGE_VERSION": "${{ needs.plan.outputs.image-version }}",
        "SHA": "${{ needs.plan.outputs.sha }}",
        "REGISTRY": "${{ needs.plan.outputs.registry }}",
    }
    assert (
        '--version "$IMAGE_VERSION" --commit "$SHA" --registry "$REGISTRY" --tested' in check["run"]
    )
    attestations = steps["The record and every image attestation verify"]
    assert attestations["run"] == "bash scripts/release/tested_images.sh verify-attestations"
    assert attestations["env"]["DC_RELEASE_IMAGES"] == "candidate/tested-images.json"
    tags = steps["Every candidate tag still points at its tested digest"]
    assert tags["run"] == 'bash scripts/release/tested_images.sh verify-tags "$IMAGE_VERSION"'
    # Nothing is read from the record before it was checked, and the registry login is removed.
    order = [
        "The candidate is a published pre-release",
        "Download the tested record of the candidate",
        "The record is for this version, commit and registry, and both suites passed",
        "Log in to ghcr.io",
        "The record and every image attestation verify",
        "Every candidate tag still points at its tested digest",
        "Log out of ghcr.io",
    ]
    positions = [names.index(n) for n in order]
    assert positions == sorted(positions)
    assert steps["Log out of ghcr.io"]["if"] == "always()"
    upload = next(s for n, s in steps.items() if n.startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "tested-images"


def test_promote_images_only_adds_the_stable_tag_to_the_verified_digests() -> None:
    steps = _steps("promote-images")
    tag = steps["Add the stable tag to the tested digests"]
    assert tag["run"] == 'bash scripts/release/tested_images.sh tag "$VERSION"'
    assert tag["env"] == {
        "DC_RELEASE_IMAGES": "tested/tested-images.json",
        "VERSION": "${{ needs.plan.outputs.version }}",
    }
    download = steps["actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333"]["with"]
    assert download["name"] == "tested-images"
    assert steps["Log out of ghcr.io"]["if"] == "always()"


def test_latest_follows_the_release_and_uses_the_released_digests() -> None:
    steps = _steps("latest")
    tag = steps["Point latest at the released digests"]
    assert tag["run"] == "bash scripts/release/tested_images.sh tag latest"
    assert tag["env"] == {"DC_RELEASE_IMAGES": "tested/tested-images.json"}
    assert _calls("tested_images.sh tag latest") == ["latest/Point latest at the released digests"]
    assert steps["Log out of ghcr.io"]["if"] == "always()"


def test_the_chart_pins_the_tested_digests_and_checks_the_package_before_the_push() -> None:
    steps = _steps("chart")
    names = list(steps)
    pin = steps["Pin the chart to the tested digests"]
    assert "release_images.py check --manifest tested/tested-images.json" in pin["run"]
    assert "--tested" in pin["run"]
    assert pin["env"]["IMAGE_VERSION"] == "${{ needs.plan.outputs.image-version }}"
    assert "pin-chart" in pin["run"]
    assert "--values deploy/helm/contractum/values.yaml" in pin["run"]
    package = steps["Package the chart"]["run"]
    assert "helm lint deploy/helm/contractum" in package
    assert '--version "$VERSION" --app-version "$VERSION"' in package
    assert 'helm template dc "dist/contractum-${VERSION}.tgz"' in package
    assert "check-rendered" in package
    assert '--version "$VERSION" --rendered rendered.yaml' in package
    order = [
        "Pin the chart to the tested digests",
        "Package the chart",
        "Push the chart to ghcr.io",
    ]
    assert [names.index(n) for n in order] == sorted(names.index(n) for n in order)


def test_the_release_attaches_the_tested_record_before_the_checksums_are_written() -> None:
    steps = _steps("release")
    download = [
        s["with"]["name"]
        for n, s in steps.items()
        if n.startswith("actions/download-artifact@") and s["with"]["name"] == "tested-images"
    ]
    assert download == ["tested-images"]
    sums = steps["Check the SDK files and write SHA256SUMS of every attached file"]["run"]
    assert "tested/tested-images.json assets/" in sums
    assert sums.index("tested/tested-images.json") < sums.index("sha256sum -- * > SHA256SUMS")
    create = steps["Create the tag and the GitHub Release"]["run"]
    assert create.index("assets/tested-images.json") < create.index("assets/SHA256SUMS")


def test_the_plan_outputs_the_candidate_the_image_version_and_the_registry() -> None:
    outputs = _workflow()["jobs"]["plan"]["outputs"]
    assert outputs["candidate"] == "${{ steps.version.outputs.candidate }}"
    assert outputs["image-version"] == "${{ steps.version.outputs.image-version }}"
    assert outputs["registry"] == "${{ steps.registry.outputs.registry }}"
    registry = _steps("plan")["Registry of the platform images"]
    assert registry["run"] == 'echo "registry=ghcr.io/${OWNER,,}" >> "$GITHUB_OUTPUT"'


# --- the two nightlies the release calls ----------------------------------------------------------


@pytest.mark.parametrize("name", ["e2e-nightly.yml", "dast-nightly.yml"])
def test_a_nightly_keeps_its_triggers_and_declares_the_release_inputs(name: str) -> None:
    workflow = _yaml(ROOT / ".github" / "workflows" / name)
    triggers = workflow[True]
    assert {"schedule", "workflow_dispatch", "workflow_call"} <= set(triggers)
    assert len(triggers["schedule"]) == 1
    inputs = triggers["workflow_call"]["inputs"]
    assert inputs["release-images"] == {
        "description": inputs["release-images"]["description"],
        "type": "string",
        "default": "",
    }
    assert inputs["release-version"]["type"] == "string"
    assert inputs["release-version"]["default"] == ""
    if name == "e2e-nightly.yml":
        assert inputs["full"]["type"] == "boolean"
        assert inputs["full"]["default"] is False
        assert triggers["workflow_dispatch"]["inputs"]["full"]["default"] is False
    else:
        assert "full" not in inputs


@pytest.mark.parametrize(
    ("name", "release", "nightly"),
    [
        ("e2e-nightly.yml", "e2e-release-{0}", "e2e-nightly"),
        ("dast-nightly.yml", "dast-release-{0}", "dast-nightly"),
    ],
)
def test_a_release_call_has_its_own_concurrency_group(
    name: str, release: str, nightly: str
) -> None:
    workflow = _yaml(ROOT / ".github" / "workflows" / name)
    group = workflow["concurrency"]["group"]
    assert group == (
        f"${{{{ inputs.release-images && format('{release}', github.run_id) || '{nightly}' }}}}"
    )
    # In a called workflow github.workflow names the caller: it would collide with Release's group.
    assert "github.workflow" not in group
    assert workflow["concurrency"]["cancel-in-progress"] is False


@pytest.mark.parametrize(
    ("name", "jobs"),
    [("e2e-nightly.yml", ("ui-e2e", "py-e2e")), ("dast-nightly.yml", ("dast",))],
)
def test_the_test_jobs_pull_the_release_images_without_a_credential_while_the_stack_runs(
    name: str, jobs: tuple[str, ...]
) -> None:
    workflow = _yaml(ROOT / ".github" / "workflows" / name)
    for job_name in jobs:
        job = workflow["jobs"][job_name]
        assert job["permissions"] == {"contents": "read", "packages": "read"}, job_name
        assert job["env"]["DC_RELEASE_IMAGES"] == (
            "${{ inputs.release-images && '.release-images/release-images.json' || '' }}"
        )
        assert job["env"]["DC_VERSION"] == "${{ inputs.release-version }}"
        steps = {step.get("name", step.get("uses", "")): step for step in job["steps"]}
        names = list(steps)
        load = steps["Load the release images by digest"]
        assert load["if"] == "inputs.release-images != ''"
        assert load["env"] == {"REGISTRY_TOKEN": "${{ github.token }}"}
        assert "--password-stdin" in load["run"]
        # Logout in the same step, on any exit, before the stack or a runner with the Docker
        # socket starts.
        assert "trap 'docker logout ghcr.io' EXIT" in load["run"]
        assert load["run"].index("trap") < load["run"].index("tested_images.sh load")
        download = next(s for n, s in steps.items() if n.startswith("actions/download-artifact@"))
        assert download["with"] == {
            "name": "${{ inputs.release-images }}",
            "path": ".release-images",
        }
        first_run = next(n for n in names if n.startswith(("Run the", "Start the compose")))
        assert names.index("Load the release images by digest") < names.index(first_run)
        verify = steps["The stack still runs the release images"]
        assert verify["run"] == "bash scripts/release/tested_images.sh verify-stack"
        assert "inputs.release-images != ''" in verify["if"]
    # The `checks` job keeps the dev default: it takes no release environment.
    if name == "e2e-nightly.yml":
        for plain in ("checks", "ui-static", "operation-coverage"):
            assert "env" not in workflow["jobs"][plain]
            assert "packages" not in workflow["jobs"][plain].get("permissions", {})


def test_dast_starts_the_release_stack_without_a_build_and_keeps_its_scans() -> None:
    workflow = _yaml(ROOT / ".github" / "workflows" / "dast-nightly.yml")
    steps = {s.get("name", s.get("uses", "")): s for s in workflow["jobs"]["dast"]["steps"]}
    up = steps["Start the compose stack"]
    assert up["id"] == "up"
    assert "docker compose up -d --no-build --wait --wait-timeout 300" in up["run"]
    assert "docker compose up -d --build --wait --wait-timeout 300" in up["run"]
    assert "tested_images.sh verify-stack" in up["run"]
    names = list(steps)
    assert names.index("Schemathesis (merged OpenAPI, authenticated)") < names.index(
        "OWASP ZAP baseline and API scan (gate on High)"
    )
    assert (
        "tests/e2e/test_phase7_schemathesis.py"
        in steps["Schemathesis (merged OpenAPI, authenticated)"]["run"]
    )
    assert names.index("OWASP ZAP baseline and API scan (gate on High)") < names.index(
        "The stack still runs the release images"
    )


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
    for arg in ("GIT_SHA=", "BUILD_TIME="):
        assert arg in build["build-args"]
    # The images hold no version: it is given at startup, so one build is tested and promoted.
    assert "VERSION" not in build["build-args"]
    assert build["tags"].endswith(":${{ needs.plan.outputs.version }}")
    assert build["push"] is True
    assert build["sbom"] is True
    assert build["provenance"] == "mode=max"
    assert build["no-cache"] is True
    attest = steps["Attest build provenance"]
    assert attest["uses"].startswith("actions/attest@")
    assert attest["with"]["push-to-registry"] is True
    digest = steps["Record the digest"]
    assert digest["env"]["DIGEST"] == "${{ steps.build.outputs.digest }}"
    assert digest["run"].rstrip().endswith('printf \'%s\\n\' "$DIGEST" > "digest/${NAME}"')
    upload = next(s for n, s in steps.items() if n.startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "digest-${{ matrix.name }}"
    assert upload["with"]["path"] == "digest/${{ matrix.name }}"


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


@pytest.mark.parametrize("dockerfile", PLATFORM_DOCKERFILES)
def test_a_platform_image_holds_no_release_version(dockerfile: str) -> None:
    # One build is tested and then released unchanged, so the version cannot be part of it: it
    # arrives at startup (DC_VERSION, DC_UI_VERSION).
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    code = re.sub(r"(?m)^\s*#.*$", "", text)
    assert not re.search(r"(?m)^\s*ARG VERSION\b", code), dockerfile
    assert not re.search(r"\bDC(_UI)?_VERSION\b", code), dockerfile
    assert not re.search(r"\$\{?VERSION\b", code), dockerfile
    assert 'org.opencontainers.image.version="${VERSION}"' not in text, dockerfile


def test_the_ui_image_clears_the_version_label_its_base_image_sets() -> None:
    # nginx-unprivileged labels itself with its own nginx version, and a Dockerfile cannot delete an
    # inherited label: an empty one keeps it from passing for the platform version.
    text = (ROOT / "infra/docker/ui.Dockerfile").read_text(encoding="utf-8")
    runtime = text.split("AS runtime", 1)[1]
    assert 'org.opencontainers.image.version=""' in runtime
    for other in ("infra/docker/service.Dockerfile", "infra/docker/api-docs.Dockerfile"):
        other_text = (ROOT / other).read_text(encoding="utf-8")
        assert "org.opencontainers.image.version" not in other_text, other


def test_every_action_in_the_release_workflow_is_pinned_by_sha() -> None:
    uses = re.findall(r"uses:\s*(\S+)", RELEASE.read_text(encoding="utf-8"))
    assert uses
    allowed = (
        "actions/",
        "astral-sh/setup-uv@",
        "docker/setup-buildx-action@",
        "docker/build-push-action@",
    )
    # The two reusable workflows of this repository are the only references that are not pinned.
    local = ("./.github/workflows/e2e-nightly.yml", "./.github/workflows/dast-nightly.yml")
    assert [ref for ref in uses if ref.startswith("./")] == list(local)
    assert "azure/setup-helm" not in RELEASE.read_text(encoding="utf-8")
    for ref in uses:
        if ref in local:
            assert (ROOT / ref).is_file(), ref
            continue
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
