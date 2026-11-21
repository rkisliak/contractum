"""Every container the chart renders has CPU and memory requests and limits (quick 261120-s0p).

The audit asked for limits on every workload. The requests stay what they were (Docker Desktop kind
schedules the defaults); this test renders the chart with ``helm template`` and walks every
Deployment, StatefulSet and Job, init containers included.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed"),
]

CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "contractum"
WORKLOADS = {"Deployment", "StatefulSet", "Job"}

# The requests of the chart before limits were added: they must render unchanged.
REQUESTS = {
    "identity": ("250m", "384Mi"),
    "contracts": ("250m", "384Mi"),
    "validation": ("500m", "1Gi"),
    "audit": ("100m", "256Mi"),
    "mail": ("50m", "128Mi"),
    "api-docs": ("50m", "96Mi"),
    "ui": ("25m", "32Mi"),
    "traefik": ("100m", "64Mi"),
    "postgres": ("250m", "512Mi"),
    "nats": ("100m", "128Mi"),
}


def _render(*sets: str) -> list[dict[str, Any]]:
    args = ["helm", "template", "dc", str(CHART)]
    for value in sets:
        args += ["--set", value]
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout  # noqa: S603
    return [doc for doc in yaml.safe_load_all(out) if isinstance(doc, dict)]


def _containers(
    docs: list[dict[str, Any]], kinds: set[str] = WORKLOADS
) -> list[tuple[str, str, dict[str, Any]]]:
    """(workload name, container name, container) for every container and initContainer."""
    found = []
    for doc in docs:
        if doc.get("kind") not in kinds:
            continue
        pod = doc["spec"]["template"]["spec"]
        for key in ("containers", "initContainers"):
            for container in pod.get(key, []):
                found.append((doc["metadata"]["name"], container["name"], container))
    return found


def _by_name(docs: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(w, c): body for w, c, body in _containers(docs)}


@pytest.mark.parametrize("mailpit", [False, True])
def test_every_container_has_requests_and_limits(mailpit: bool) -> None:
    docs = _render(f"mailpit.enabled={str(mailpit).lower()}")
    containers = _containers(docs)
    assert containers
    names = {c for _, c, _ in containers}
    assert {"wait-for-migrations", "done", "problem-responder"} <= names
    assert ("mailpit" in names) is mailpit
    for workload, name, container in containers:
        resources = container.get("resources", {})
        for section in ("requests", "limits"):
            for field in ("cpu", "memory"):
                value = resources.get(section, {}).get(field)
                assert value, f"{workload}/{name} has no {section}.{field}"


def test_the_existing_requests_render_unchanged() -> None:
    seen: set[str] = set()
    for _, name, container in _containers(_render(), {"Deployment", "StatefulSet"}):
        if name in REQUESTS:
            requests = container["resources"]["requests"]
            assert (requests["cpu"], requests["memory"]) == REQUESTS[name], name
            seen.add(name)
    assert seen == set(REQUESTS)


def test_a_service_limit_override_reaches_the_container() -> None:
    docs = _render("identity.resources.limits.cpu=3")
    identity = _by_name(docs)[("identity", "identity")]
    assert str(identity["resources"]["limits"]["cpu"]) == "3"


def test_the_migrations_override_reaches_every_migration_container() -> None:
    docs = _render("migrations.resources.limits.memory=1Gi")
    job = [(c, body) for w, c, body in _containers(docs) if w.startswith("migrate")]
    assert {c for c, _ in job} == {"identity", "contracts", "validation", "audit", "done"}
    for name, container in job:
        assert container["resources"]["limits"]["memory"] == "1Gi", name


def test_the_other_new_blocks_are_overridable() -> None:
    docs = _render(
        "mailpit.enabled=true",
        "waitForMigrations.resources.limits.cpu=700m",
        "mailpit.resources.limits.memory=200Mi",
        "problemResponder.resources.limits.cpu=300m",
    )
    by_name = _by_name(docs)
    waits = [b for (w, c), b in by_name.items() if c == "wait-for-migrations"]
    assert waits
    assert all(b["resources"]["limits"]["cpu"] == "700m" for b in waits)
    assert by_name[("mailpit", "mailpit")]["resources"]["limits"]["memory"] == "200Mi"
    assert by_name[("problem-responder", "problem-responder")]["resources"]["limits"]["cpu"] == (
        "300m"
    )
