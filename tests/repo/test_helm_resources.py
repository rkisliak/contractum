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


def _render(*sets: str, namespace: str | None = None) -> list[dict[str, Any]]:
    args = ["helm", "template", "dc", str(CHART)]
    if namespace is not None:
        args += ["--namespace", namespace]
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


# --- pod hardening (code scanning, Trivy KSV rules) -------------------------------------------
# Every resource names its release namespace, every container runs on a read-only root
# filesystem without privilege escalation and with all capabilities dropped, and every pod runs
# as a non-root user, except the bundled NATS, whose data volume in an existing release is owned
# by root. The exception is listed exactly, so a new one fails. The gateway runs non-root on the
# unprivileged port 8443 behind the Service's port 443.

NAMESPACE = "contractum"
# Pods that still start as root: the bundled NATS (its data volume is owned by root).
ROOT_PODS = {"nats"}
# Pods on the image's own fixed uid at or below 10000: postgres (999) owns its data directory.
LOW_UID_PODS = {"postgres"}


def _workloads(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc.get("kind") in WORKLOADS]


def _pod(doc: dict[str, Any]) -> dict[str, Any]:
    pod: dict[str, Any] = doc["spec"]["template"]["spec"]
    return pod


def _pod_containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    pod = _pod(doc)
    return [*pod.get("containers", []), *pod.get("initContainers", [])]


@pytest.mark.parametrize("namespace", [NAMESPACE, "contractum-eval"])
def test_every_resource_names_the_release_namespace(namespace: str) -> None:
    docs = _render(namespace=namespace)
    assert docs
    for doc in docs:
        assert doc["metadata"].get("namespace") == namespace, doc["metadata"]["name"]


def test_every_container_has_a_read_only_root_and_no_privileges() -> None:
    workloads = _workloads(_render(namespace=NAMESPACE))
    assert {doc["metadata"]["name"] for doc in workloads} >= {"postgres", "nats", "ui", "gateway"}
    for doc in workloads:
        for container in _pod_containers(doc):
            where = f"{doc['metadata']['name']}/{container['name']}"
            context = container.get("securityContext", {})
            assert context.get("readOnlyRootFilesystem") is True, where
            assert context.get("allowPrivilegeEscalation") is False, where
            assert context.get("capabilities", {}).get("drop") == ["ALL"], where


def test_every_pod_but_the_listed_exceptions_runs_as_non_root() -> None:
    starts_as_root = set()
    for doc in _workloads(_render(namespace=NAMESPACE)):
        context = _pod(doc).get("securityContext", {})
        if context.get("runAsNonRoot") is not True:
            starts_as_root.add(doc["metadata"]["name"])
            continue
        if doc["metadata"]["name"] not in LOW_UID_PODS:
            assert context.get("runAsUser", 0) > 10000, doc["metadata"]["name"]
            assert context.get("runAsGroup", 0) > 10000, doc["metadata"]["name"]
    assert starts_as_root == ROOT_PODS


def test_postgres_keeps_its_data_owner_and_writable_scratch_directories() -> None:
    (postgres,) = [
        doc
        for doc in _render(namespace=NAMESPACE)
        if doc["metadata"]["name"] == "postgres" and doc["kind"] == "StatefulSet"
    ]
    context = _pod(postgres)["securityContext"]
    assert (context["runAsUser"], context["runAsGroup"], context["fsGroup"]) == (999, 999, 999)
    mounts = {m["name"] for m in _pod(postgres)["containers"][0]["volumeMounts"]}
    assert {"data", "run", "tmp"} <= mounts
    scratch = {v["name"] for v in _pod(postgres)["volumes"] if "emptyDir" in v}
    assert {"run", "tmp"} <= scratch


def test_the_gateway_listens_unprivileged_behind_service_port_443() -> None:
    docs = _render(namespace=NAMESPACE)
    by_kind = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}
    static = by_kind[("ConfigMap", "gateway-static")]["data"]["traefik.yml"]
    assert 'address: ":8443"' in static
    assert 'address: ":443"' not in static
    gateway = by_kind[("Deployment", "gateway")]
    (traefik,) = _pod(gateway)["containers"]
    ports = {p["name"]: p["containerPort"] for p in traefik["ports"]}
    assert ports == {"websecure": 8443, "ping": 8080}
    assert "add" not in traefik["securityContext"]["capabilities"]
    service = by_kind[("Service", "gateway")]
    assert service["spec"]["ports"] == [{"name": "https", "port": 443, "targetPort": "websecure"}]
    policy = by_kind[("NetworkPolicy", "dc-gateway-from-anywhere")]
    assert policy["spec"]["ingress"] == [{"ports": [{"port": 8443}]}]
