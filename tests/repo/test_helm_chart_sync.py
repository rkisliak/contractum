"""The Helm chart's copies of compose configuration stay byte-identical to their sources.

Helm can read only files inside the chart directory, so deploy/helm/contractum/files holds
copies of the gateway configuration, the problem responder and the Postgres init script. A change
to a source must be copied into the chart in the same commit, or Kubernetes and compose drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "contractum"

COPIES = {
    "files/gateway/traefik.yml": "gateway/traefik.yml",
    "files/gateway/dynamic/routers.yml": "gateway/dynamic/routers.yml",
    "files/gateway/dynamic/middlewares.yml": "gateway/dynamic/middlewares.yml",
    "files/gateway/dynamic/tls.yml": "gateway/dynamic/tls.yml",
    "files/problem-responder/responder.py": "gateway/problem-responder/responder.py",
    "files/postgres/10-service-databases.sh": "infra/postgres/init/10-service-databases.sh",
}


@pytest.mark.parametrize(("copy", "source"), sorted(COPIES.items()))
def test_chart_copy_matches_its_source(copy: str, source: str) -> None:
    assert (CHART / copy).read_bytes() == (ROOT / source).read_bytes(), (
        f"{copy} differs from {source}: copy the source into the chart"
    )


def test_every_gateway_dynamic_file_except_the_dev_mock_is_in_the_chart() -> None:
    sources = {p.name for p in (ROOT / "gateway" / "dynamic").glob("*.yml")}
    copied = {p.name for p in (CHART / "files" / "gateway" / "dynamic").glob("*.yml")}
    # dev-mock-oidc.yml routes the compose-only mock Entra issuer; it never ships in the chart.
    assert copied == sources - {"dev-mock-oidc.yml"}


def test_gateway_pod_has_a_writable_tmp_for_response_buffering() -> None:
    """Traefik's buffering middleware spills responses over 1 MiB to /tmp (the Swagger UI bundle is
    1.7 MB); the gateway's root filesystem is read-only, so /tmp must be a mounted emptyDir."""
    template = (CHART / "templates" / "gateway.yaml").read_text(encoding="utf-8")
    assert "readOnlyRootFilesystem: true" in template
    assert "{name: tmp, mountPath: /tmp}" in template
    assert "- name: tmp\n          emptyDir:" in template
