"""The parts of ``scripts/ci.sh`` (``CI_PART``) together run the whole gate, and E2E Nightly runs
every part, every Playwright project and every shard.

``.github/workflows/e2e-nightly.yml`` runs the gate as parallel jobs, one per part. These tests
run ``scripts/ci.sh`` in a scratch tree where ``uv``, ``docker``, ``pnpm``, ``node`` and the two e2e
runners only log their command line, once for the whole gate (``CI_PART=all``, ``CI_E2E=1``) and
once per part, in the default and in the full mode: the parts together must run exactly the
commands of the whole gate. The workflow tests then pin the jobs to the parts and the ``ui-e2e``
matrix to the projects of ``ui/playwright.config.ts``.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "e2e-nightly.yml"
PLAYWRIGHT_CONFIG = ROOT / "ui" / "playwright.config.ts"
PARTS = ("checks", "ui", "ui-e2e", "py-e2e", "operation-coverage")

_LOG = 'printf "%s %s\\n" "$(basename "$0")" "$*" >>"$CI_LOG"\n'


def _stub(path: Path, body: str = "") -> None:
    path.write_text(f"#!/usr/bin/env bash\n{_LOG}{body}exit 0\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scratch copy of scripts/ci.sh with logging stand-ins for every command it runs."""
    root = tmp_path_factory.mktemp("ci-parts")
    (root / "scripts").mkdir()
    (root / "ui").mkdir()
    (root / "bin").mkdir()
    (root / "scripts" / "ci.sh").write_text(
        (ROOT / "scripts" / "ci.sh").read_text(encoding="utf-8"), encoding="utf-8"
    )
    nvmrc = (ROOT / "ui" / ".nvmrc").read_text(encoding="utf-8").strip()
    (root / "ui" / ".nvmrc").write_text(nvmrc + "\n", encoding="utf-8")
    _stub(root / "scripts" / "e2e-ui.sh")
    _stub(root / "scripts" / "e2e.sh")
    (root / "scripts" / "release").mkdir()
    _stub(root / "scripts" / "release" / "tested_images.sh")
    _stub(root / "bin" / "uv")
    _stub(root / "bin" / "docker")
    _stub(root / "bin" / "pnpm", 'if [ "$1" = "--version" ]; then echo 12.17.0; fi\n')
    _stub(root / "bin" / "node", f'if [ "$1" = "--version" ]; then echo v{nvmrc}; fi\n')
    return root


def _run(
    tree: Path, part: str, *, full: bool, ui_e2e_args: str = "", release: bool = False
) -> list[str]:
    log = tree / f"log-{part}-{full}-{len(ui_e2e_args)}-{release}.txt"
    log.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CI", "DC_RELEASE"))}
    env.update(
        PATH=f"{tree / 'bin'}{os.pathsep}{os.environ['PATH']}",
        CI_LOG=str(log),
        CI_PART=part,
        CI_UI_E2E_ARGS=ui_e2e_args,
    )
    if release:
        env["DC_RELEASE_IMAGES"] = ".release-images/release-images.json"
    if part == "all":
        env["CI_E2E"] = "1"
    if full:
        env["CI_FULL"] = "1"
    subprocess.run(["/bin/bash", "scripts/ci.sh"], env=env, check=True, cwd=tree)
    lines = log.read_text(encoding="utf-8").splitlines()
    # The availability probes are not steps.
    return [line for line in lines if line not in {"docker info", "node --version"}]


@pytest.mark.parametrize("full", [False, True], ids=["default", "full"])
def test_the_parts_together_run_exactly_the_whole_gate(tree: Path, full: bool) -> None:
    whole = _run(tree, "all", full=full)
    union: set[str] = set()
    for part in PARTS:
        commands = _run(tree, part, full=full)
        assert commands or (part == "operation-coverage" and not full), part
        union |= set(commands)
    assert union == set(whole)
    # The whole gate still runs every stage, so an empty stand-in run cannot pass.
    assert any(line.startswith("uv run pytest -m unit") for line in whole)
    assert any(line.startswith("uv run pytest -m integration") for line in whole)
    assert any(line.startswith("pnpm --dir ui ci:") for line in whole)
    assert "e2e-ui.sh " in whole
    assert "e2e.sh tests/e2e" in whole
    assert ("uv run python scripts/operation_coverage.py --gate" in whole) is full
    assert ("uv run python scripts/coverage_gate.py" in whole) is full


def test_the_ui_e2e_part_passes_its_playwright_arguments(tree: Path) -> None:
    commands = _run(tree, "ui-e2e", full=False, ui_e2e_args="--project=volume --shard=1/4")
    assert "e2e-ui.sh --project=volume --shard=1/4" in commands
    assert "e2e.sh tests/e2e" not in commands


def test_an_unknown_part_is_refused(tree: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        _run(tree, "lint", full=False)
    assert refused.value.returncode == 2


def _jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    return jobs


def _part_of(job: dict[str, Any]) -> str | None:
    for step in job.get("steps", []):
        part = step.get("env", {}).get("CI_PART")
        if part is not None:
            assert "bash scripts/ci.sh" in step["run"]
            return str(part)
    return None


def test_every_part_has_its_own_nightly_job() -> None:
    parts = {name: _part_of(job) for name, job in _jobs().items()}
    assert sorted(part for part in parts.values() if part) == sorted(PARTS)
    coverage = _jobs()["operation-coverage"]
    assert coverage["if"] == "inputs.full"
    assert set(coverage["needs"]) == {"checks", "ui-e2e", "py-e2e"}


def _projects() -> dict[str, list[str]]:
    """Each Playwright project of ui/playwright.config.ts with its dependencies."""
    text = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
    blocks = re.split(r'\n\s*name: "', text.split("projects: [", 1)[1])[1:]
    projects: dict[str, list[str]] = {}
    for block in blocks:
        name = block.split('"', 1)[0]
        deps = re.search(r"dependencies: \[([^\]]*)\]", block)
        projects[name] = re.findall(r'"([\w-]+)"', deps.group(1)) if deps else []
    return projects


def test_the_ui_e2e_matrix_runs_every_playwright_project_and_shard() -> None:
    projects = _projects()
    assert {"chromium", "chromium-dark", "volume-setup", "volume", "volume-dark"} <= set(projects)
    job = _jobs()["ui-e2e"]
    entries = job["strategy"]["matrix"]["include"]
    assert job["strategy"]["fail-fast"] is False
    shards: dict[tuple[str, ...], set[tuple[int, int]]] = {}
    for entry in entries:
        args = entry["args"].split()
        selected = tuple(sorted(a.removeprefix("--project=") for a in args if "--project=" in a))
        assert selected, entry
        assert set(selected) <= set(projects), entry
        shard = [a.removeprefix("--shard=") for a in args if a.startswith("--shard=")]
        index, total = (int(x) for x in shard[0].split("/")) if shard else (1, 1)
        shards.setdefault(selected, set()).add((index, total))
    for selected, seen in shards.items():
        totals = {total for _, total in seen}
        assert len(totals) == 1, selected
        total = totals.pop()
        assert {index for index, _ in seen} == set(range(1, total + 1)), selected
    covered = {project for selected in shards for project in selected}
    covered |= {dep for project in covered for dep in projects[project]}
    assert covered == set(projects)
    names = {entry["part"] for entry in entries}
    assert len(names) == len(entries)
    uploads = [step for step in job["steps"] if "upload-artifact" in step.get("uses", "")]
    assert uploads
    assert all("${{ matrix.part }}" in step["with"]["name"] for step in uploads)


# --- the release images: the same gate, started without a build ------------------------------


@pytest.mark.parametrize("part", ["ui-e2e", "py-e2e"])
def test_the_cold_start_builds_from_source_unless_release_images_are_given(
    tree: Path, part: str
) -> None:
    commands = _run(tree, part, full=False)
    assert "docker compose down -v --remove-orphans" in commands
    assert "docker compose up -d --build --wait --wait-timeout 300" in commands
    assert not [line for line in commands if "tested_images.sh" in line]
    assert not [line for line in commands if "--no-build" in line]


@pytest.mark.parametrize("part", ["ui-e2e", "py-e2e"])
def test_release_images_start_the_stack_without_a_build_and_prove_it_runs_them(
    tree: Path, part: str
) -> None:
    commands = _run(tree, part, full=False, release=True)
    start = commands.index("docker compose down -v --remove-orphans")
    assert commands[start + 1] == "docker compose up -d --no-build --wait --wait-timeout 300"
    assert commands[start + 2] == "tested_images.sh verify-stack"
    assert not [line for line in commands if "--build" in line.replace("--no-build", "")]
    # The tests start only after the stack was proven to run the release images.
    runner = "e2e-ui.sh " if part == "ui-e2e" else "e2e.sh tests/e2e"
    assert commands.index(runner) > start + 2


def test_the_whole_gate_in_release_mode_runs_the_same_steps_apart_from_the_start(
    tree: Path,
) -> None:
    plain = _run(tree, "all", full=False)
    release = _run(tree, "all", full=False, release=True)
    swapped = [line for line in release if line not in {"tested_images.sh verify-stack"}]
    assert [
        "docker compose up -d --build --wait --wait-timeout 300"
        if line == "docker compose up -d --no-build --wait --wait-timeout 300"
        else line
        for line in swapped
    ] == plain


@pytest.fixture(scope="module")
def runners(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scratch copy of the two warm-stack runner scripts with logging stand-ins."""
    root = tmp_path_factory.mktemp("runners")
    (root / "scripts").mkdir()
    (root / "bin").mkdir()
    for name in ("e2e.sh", "e2e-ui.sh"):
        (root / "scripts" / name).write_text(
            (ROOT / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (root / "scripts" / "e2e-runner-env.sh").write_text("", encoding="utf-8")
    _stub(root / "bin" / "docker")
    _stub(root / "bin" / "git", "echo abc123def456\n")
    return root


def _runner(
    runners: Path, script: str, args: str, *, release: bool, performance: bool = False
) -> list[str]:
    log = runners / f"log-{script}-{release}-{performance}.txt"
    log.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CI", "DC_"))}
    env.update(PATH=f"{runners / 'bin'}{os.pathsep}{os.environ['PATH']}", CI_LOG=str(log))
    env["RUNNER_ARGS"] = args
    if release:
        env["DC_RELEASE_IMAGES"] = ".release-images/release-images.json"
    if performance:
        env["DC_PERFORMANCE"] = "1"
    subprocess.run(
        ["/bin/bash", "-c", "exec bash scripts/$RUNNER_SCRIPT $RUNNER_ARGS"],
        env={**env, "RUNNER_SCRIPT": script},
        check=True,
        cwd=runners,
    )
    return [
        line for line in log.read_text(encoding="utf-8").splitlines() if not line.startswith("git ")
    ]


COMPOSE = "docker compose"
TEST = "docker compose -f compose.yaml -f compose.test.yaml --profile test"
PYTEST = "pytest -m e2e and not performance -q -o asyncio_default_test_loop_scope=session"


def test_the_py_runner_builds_everything_unless_release_images_are_given(runners: Path) -> None:
    commands = _runner(runners, "e2e.sh", "tests/e2e", release=False)
    assert commands == [
        f"{COMPOSE} up -d --build --wait --wait-timeout 300",
        f"{TEST} run --rm --build e2e {PYTEST} tests/e2e",
    ]


def test_the_py_runner_builds_only_its_runner_with_release_images(runners: Path) -> None:
    commands = _runner(runners, "e2e.sh", "tests/e2e", release=True)
    assert commands == [
        f"{COMPOSE} up -d --no-build --wait --wait-timeout 300",
        f"{TEST} build e2e",
        f"{TEST} run --rm e2e {PYTEST} tests/e2e",
    ]
    # `run --build` may rebuild the services the runner depends on and replace the tested images.
    assert not [line for line in commands if "--build" in line]


def test_the_py_runner_keeps_the_performance_marker_in_both_modes(runners: Path) -> None:
    for release in (False, True):
        commands = _runner(runners, "e2e.sh", "tests/e2e", release=release, performance=True)
        assert "pytest -m e2e -q" in commands[-1]


def test_the_ui_runner_builds_everything_unless_release_images_are_given(runners: Path) -> None:
    commands = _runner(runners, "e2e-ui.sh", "ui/e2e/smoke.spec.ts -g axe", release=False)
    assert commands == [
        f"{COMPOSE} up -d --build --wait --wait-timeout 300",
        f"{TEST} run --rm --build ui-e2e pnpm exec playwright test e2e/smoke.spec.ts -g axe",
    ]


def test_the_ui_runner_builds_only_its_runner_with_release_images(runners: Path) -> None:
    commands = _runner(runners, "e2e-ui.sh", "--project=volume", release=True)
    assert commands == [
        f"{COMPOSE} up -d --no-build --wait --wait-timeout 300",
        f"{TEST} build ui-e2e",
        f"{TEST} run --rm ui-e2e pnpm exec playwright test --project=volume",
    ]
    assert not [line for line in commands if "--build" in line]


def test_operation_coverage_downloads_each_ui_part_to_its_own_folder() -> None:
    """The UI parts share file names, so a merged download can corrupt the API call records."""
    downloads = [
        step["with"]
        for step in _jobs()["operation-coverage"]["steps"]
        if "download-artifact" in step.get("uses", "")
        and "ui-api-calls" in step.get("with", {}).get("pattern", "")
    ]
    assert len(downloads) == 1
    assert "merge-multiple" not in downloads[0]
    assert downloads[0]["path"] == "ui/test-results"
