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
import yaml

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


@pytest.mark.parametrize(
    ("dockerfile", "url"),
    [
        ("infra/docker/ui.Dockerfile", "http://127.0.0.1:8080/healthz.txt"),
        ("infra/docker/api-docs.Dockerfile", "http://localhost:8080/healthz"),
    ],
)
def test_single_purpose_runtime_images_declare_the_compose_health_check(
    dockerfile: str, url: str
) -> None:
    stage = final_stage(dockerfile)
    check = re.search(r"(?m)^HEALTHCHECK .*\\\n\s+CMD \[.*\]$", stage)
    assert check is not None
    assert url in check.group()
    assert "HEALTHCHECK NONE" not in stage
    # The URL is the one compose probes, so the image and compose agree.
    assert url in (ROOT / "compose.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "dockerfile", sorted(p.name for p in (ROOT / "infra/docker").glob("*.Dockerfile"))
)
def test_every_image_declares_a_health_check(dockerfile: str) -> None:
    stage = final_stage(f"infra/docker/{dockerfile}")
    assert re.search(r"(?m)^HEALTHCHECK --interval=\S+ .*\\\n\s+CMD \[", stage), dockerfile
    assert "HEALTHCHECK NONE" not in stage


def test_one_shot_compose_jobs_on_the_service_image_disable_its_health_check() -> None:
    services = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]
    one_shots = {
        "dev-init",
        *(f"{s}-migrate" for s in ("identity", "contracts", "validation", "audit")),
    }
    for name in one_shots:
        assert services[name]["healthcheck"] == {"disable": True}, name
    # Every other service on a platform image either is the service itself (port 8000, the image
    # default) or sets its own check.
    for name, service in services.items():
        if name in one_shots or not str(service.get("image", "")).startswith("contractum/"):
            continue
        assert service["healthcheck"]["test"], name
