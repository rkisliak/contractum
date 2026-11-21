"""Runtime images carry no fixable OS vulnerability and no pip (quick 261120-pn7).

The Trivy image scan of the :dev images found HIGH advisories that the pinned base images still
ship: the Debian zlib1g and Alpine musl/zlib packages (fixed in the distribution, not yet in the
base tag), and the copies of urllib3, msgpack and setuptools vendored inside pip. The services
never run pip, so the Python images remove it, and every runtime stage upgrades the OS packages as
root before it drops to its unprivileged user. These tests read the Dockerfiles only; the real
proof is `trivy image --ignore-unfixed` on a fresh build.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def final_stage(path: str) -> str:
    """The text of the last build stage (from its last FROM line on)."""
    text = (ROOT / path).read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"(?m)^FROM ", text)]
    return text[starts[-1] :]


def position(stage: str, needle: str) -> int:
    index = stage.find(needle)
    assert index >= 0, f"missing {needle!r} in the runtime stage"
    return index


@pytest.mark.parametrize(
    "dockerfile", ["infra/docker/service.Dockerfile", "infra/docker/api-docs.Dockerfile"]
)
def test_python_runtime_upgrades_os_packages_and_drops_pip(dockerfile: str) -> None:
    stage = final_stage(dockerfile)
    user = re.search(r"(?m)^USER ", stage)
    assert user is not None
    upgrade = position(stage, "apt-get upgrade")
    removal = position(stage, "pip uninstall")
    assert upgrade < user.start()
    assert removal < user.start()
    # The pip that is removed is the base image's, called by absolute path (the app venv has none).
    assert "/usr/local/bin/python -m pip uninstall" in stage


def test_ui_runtime_upgrades_alpine_packages_as_root() -> None:
    stage = final_stage("infra/docker/ui.Dockerfile")
    root = position(stage, "USER root")
    upgrade = position(stage, "apk upgrade")
    drop = position(stage, "USER 101")
    assert root < upgrade < drop
