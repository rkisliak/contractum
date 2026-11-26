"""The OS package upgrade in the image builds is never served from the CI build cache."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = [
    ROOT / "infra" / "docker" / "service.Dockerfile",
    ROOT / "infra" / "docker" / "api-docs.Dockerfile",
    ROOT / "infra" / "docker" / "ui.Dockerfile",
    ROOT / "clients" / "python" / "Dockerfile",
]
UPGRADE = re.compile(r"\b(apt-get upgrade|apk upgrade)\b")


def _upgrade_runs(text: str) -> list[str]:
    """Each RUN instruction (with its continuation lines) that upgrades OS packages."""
    runs: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("RUN "):
            current = [line]
        elif current is not None:
            current.append(line)
        if current is not None and not line.rstrip().endswith("\\"):
            run = "\n".join(current)
            if UPGRADE.search(run):
                runs.append(run)
            current = None
    return runs


def test_every_os_upgrade_dockerfile_is_listed() -> None:
    found = {
        p
        for p in ROOT.glob("**/*Dockerfile")
        if "node_modules" not in p.parts and UPGRADE.search(p.read_text(encoding="utf-8"))
    }
    assert found == set(DOCKERFILES)


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_os_upgrade_uses_the_refresh_arg(dockerfile: Path) -> None:
    text = dockerfile.read_text(encoding="utf-8")
    runs = _upgrade_runs(text)
    assert len(runs) == 1
    declared = text.index("ARG OS_PACKAGES_REFRESH=")
    assert declared < text.index(runs[0])
    assert "${OS_PACKAGES_REFRESH" in runs[0]


def _build_steps() -> list[tuple[str, dict[str, Any]]]:
    steps: list[tuple[str, dict[str, Any]]] = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
        for name, job in jobs.items():
            for step in job.get("steps", []):
                if "docker/build-push-action" in step.get("uses", ""):
                    steps.append((f"{workflow.name}:{name}", step.get("with", {})))
    return steps


def test_every_cached_image_build_passes_a_new_refresh_value() -> None:
    cached = [(where, w) for where, w in _build_steps() if "cache-from" in w]
    assert cached, "no cached image build found"
    for where, w in cached:
        assert not w.get("no-cache"), where
        assert "OS_PACKAGES_REFRESH=${{ github.run_id }}" in str(w.get("build-args", "")), where


def test_uncached_builds_really_skip_the_cache() -> None:
    for where, w in _build_steps():
        if "cache-from" not in w and w.get("no-cache") is not True:
            # The CLI push in release.yml reuses the layers of the no-cache smoke build before it.
            assert where == "release.yml:cli-image", where
