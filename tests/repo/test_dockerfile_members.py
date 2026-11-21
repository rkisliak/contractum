"""Pitfall 12 (workspace member drift): both Dockerfiles copy every member's pyproject.toml.

`uv sync --locked --no-install-workspace` resolves the whole workspace, so a member whose
manifest is missing from the build context fails the image build (or, worse, builds against a
stale lock view). Adding a workspace member without touching the Dockerfiles must fail here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = ("infra/docker/service.Dockerfile", "infra/docker/e2e.Dockerfile")


def workspace_member_dirs(root: Path) -> list[str]:
    """Member directories of ``[tool.uv.workspace]`` (globs expanded), as POSIX paths.

    Like uv, a glob match counts only when it is a directory holding a pyproject.toml and does
    not start with a dot (``shared/.claude`` holds skills, not a package).
    """
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns: list[str] = config["tool"]["uv"]["workspace"]["members"]
    found: set[str] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.name.startswith("."):
                continue
            if path.is_dir() and (path / "pyproject.toml").is_file():
                found.add(path.relative_to(root).as_posix())
    return sorted(found)


def copied_manifests(dockerfile_text: str) -> set[str]:
    """Directories whose ``pyproject.toml`` a Dockerfile copies (``COPY <dir>/pyproject.toml``)."""
    return {
        match.group(1)
        for match in re.finditer(
            r"^COPY\s+(?:--\S+\s+)*(\S+)/pyproject\.toml\s", dockerfile_text, re.MULTILINE
        )
    }


def test_the_workspace_has_the_documented_members() -> None:
    assert workspace_member_dirs(ROOT) == [
        "audit",
        "contracts",
        "identity",
        "mail",
        "shared/dc-auth",
        "shared/dc-core",
        "shared/dc-odcs",
        "shared/dc-rules",
        "validation",
    ]


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_every_workspace_member_manifest_is_copied_by_both_dockerfiles(dockerfile: str) -> None:
    copied = copied_manifests((ROOT / dockerfile).read_text(encoding="utf-8"))
    missing = [member for member in workspace_member_dirs(ROOT) if member not in copied]
    assert not missing, f"{dockerfile} does not copy pyproject.toml of: {missing}"


def test_the_manifest_scanner_sees_a_missing_member() -> None:
    """The check can fail: a Dockerfile that forgets a member is reported."""
    text = "COPY shared/dc-core/pyproject.toml    shared/dc-core/pyproject.toml\n"
    assert copied_manifests(text) == {"shared/dc-core"}
    assert "audit" not in copied_manifests(text)


def test_a_new_member_directory_is_discovered_from_the_workspace_globs(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["shared/*", "svc"]\n', encoding="utf-8"
    )
    for name in ("shared/lib-a", "svc", "shared/.claude", "shared/no-manifest"):
        (tmp_path / name).mkdir(parents=True)
    for name in ("shared/lib-a", "svc"):
        (tmp_path / name / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert workspace_member_dirs(tmp_path) == ["shared/lib-a", "svc"]
