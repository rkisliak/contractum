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
# as a non-root user above uid and gid 10000, the bundled Postgres and NATS included. The gateway
# runs non-root on the unprivileged port 8443 behind the Service's port 443.

NAMESPACE = "contractum"


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


def test_every_pod_runs_as_non_root_above_uid_and_gid_10000() -> None:
    workloads = _workloads(_render(namespace=NAMESPACE))
    assert {doc["metadata"]["name"] for doc in workloads} >= {"postgres", "nats"}
    for doc in workloads:
        context = _pod(doc).get("securityContext", {})
        name = doc["metadata"]["name"]
        assert context.get("runAsNonRoot") is True, name
        assert context.get("runAsUser", 0) > 10000, name
        assert context.get("runAsGroup", 0) > 10000, name
        assert context.get("seccompProfile") == {"type": "RuntimeDefault"}, name


@pytest.mark.parametrize("name", ["postgres", "nats"])
def test_the_bundled_backing_services_own_their_data_volume_as_10001(name: str) -> None:
    (backing,) = [
        doc
        for doc in _render(namespace=NAMESPACE)
        if doc["metadata"]["name"] == name and doc["kind"] == "StatefulSet"
    ]
    context = _pod(backing)["securityContext"]
    # fsGroup makes the data volume writable and the 0440 Secret files readable for the uid.
    assert (context["runAsUser"], context["runAsGroup"], context["fsGroup"]) == (
        10001,
        10001,
        10001,
    )
    assert backing["spec"]["volumeClaimTemplates"][0]["metadata"]["name"] == "data"


def test_postgres_has_writable_scratch_directories() -> None:
    (postgres,) = [
        doc
        for doc in _render(namespace=NAMESPACE)
        if doc["metadata"]["name"] == "postgres" and doc["kind"] == "StatefulSet"
    ]
    mounts = {m["name"] for m in _pod(postgres)["containers"][0]["volumeMounts"]}
    assert {"data", "run", "tmp"} <= mounts
    scratch = {v["name"] for v in _pod(postgres)["volumes"] if "emptyDir" in v}
    # /tmp also holds the nss_wrapper passwd file that gives uid 10001 a name for initdb.
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


# --- image references (code scanning, Trivy KSV-0125) -----------------------------------------
# The platform images come from the release registry ghcr.io/rkisliak (release.yml) with the
# chart's appVersion as the default tag. A local cluster points the chart at its imported
# contractum/<component>:dev images with an empty registry and the dev tag.

PLATFORM_IMAGES = {"identity", "contracts", "validation", "audit", "mail", "api-docs", "ui"}
THIRD_PARTY = {"postgres", "nats", "traefik"}


def _app_version() -> str:
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
    version: str = chart["appVersion"]
    return version


def _images(docs: list[dict[str, Any]]) -> set[str]:
    return {body["image"] for _, _, body in _containers(docs)}


def _platform(image: str) -> bool:
    return image.rsplit(":", 1)[0].rsplit("/", 1)[-1] in PLATFORM_IMAGES


def test_platform_images_default_to_the_release_registry_and_the_app_version() -> None:
    images = _images(_render(namespace=NAMESPACE))
    platform = {image for image in images if _platform(image)}
    assert platform == {
        f"ghcr.io/rkisliak/contractum/{name}:{_app_version()}" for name in PLATFORM_IMAGES
    }
    # The third-party images carry no registry host (Docker Hub official images).
    for image in images - platform:
        assert "/" not in image, image
        assert image.split(":")[0] in THIRD_PARTY, image


def test_local_images_render_with_an_empty_registry_and_the_dev_tag() -> None:
    images = _images(_render("global.imageRegistry=", "global.imageTag=dev", namespace=NAMESPACE))
    assert {image for image in images if _platform(image)} == {
        f"contractum/{name}:dev" for name in PLATFORM_IMAGES
    }


def test_a_mirror_registry_and_tag_reach_every_platform_image() -> None:
    images = _images(
        _render(
            "global.imageRegistry=myregistry.azurecr.io",
            "global.imageTag=1.2.3",
            namespace=NAMESPACE,
        )
    )
    assert {image for image in images if _platform(image)} == {
        f"myregistry.azurecr.io/contractum/{name}:1.2.3" for name in PLATFORM_IMAGES
    }


def test_every_platform_resource_carries_the_app_version_label() -> None:
    # Chart.yaml holds placeholders; the Release workflow packages the chart with the released
    # version as version and appVersion, so the label names the platform version of an install.
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
    assert (chart["version"], chart["appVersion"]) == ("0.0.0", "dev")
    labelled = [
        doc
        for doc in _render(namespace=NAMESPACE)
        if doc.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/part-of")
        == "contractum"
    ]
    assert labelled
    for doc in labelled:
        labels = doc["metadata"]["labels"]
        assert labels["app.kubernetes.io/version"] == _app_version(), doc["metadata"]["name"]


# --- digests and the runtime version -----------------------------------------------------------
# A published chart pins every platform image to the digest that was tested (global.imageDigests,
# written by scripts/release/release_images.py pin-chart). The images hold no version of their own,
# so the chart hands the platform version to the containers as DC_VERSION.

DIGESTS = {
    name: "sha256:" + "0123456789abcdef"[i] * 64 for i, name in enumerate(sorted(PLATFORM_IMAGES))
}


def _digest_sets() -> list[str]:
    return [f"global.imageDigests.{name}={digest}" for name, digest in DIGESTS.items()]


def _platform_images(docs: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Every platform image reference of the render, by component."""
    found: dict[str, set[str]] = {name: set() for name in PLATFORM_IMAGES}
    for _, _, body in _containers(docs):
        repository = body["image"].split("@", 1)[0].rsplit(":", 1)[0]
        component = repository.rsplit("/", 1)[-1]
        if component in PLATFORM_IMAGES:
            found[component].add(body["image"])
    return found


def test_digests_render_as_tag_and_digest_for_every_platform_image() -> None:
    images = _platform_images(_render(*_digest_sets(), namespace=NAMESPACE))
    for name, references in images.items():
        assert references == {
            f"ghcr.io/rkisliak/contractum/{name}:{_app_version()}@{DIGESTS[name]}"
        }, name


def test_an_explicit_image_tag_renders_tag_only_even_with_digests() -> None:
    images = _platform_images(
        _render(*_digest_sets(), "global.imageTag=1.2.3", namespace=NAMESPACE)
    )
    for name, references in images.items():
        assert references == {f"ghcr.io/rkisliak/contractum/{name}:1.2.3"}, name


def test_a_component_without_a_digest_renders_tag_only() -> None:
    sets = [s for s in _digest_sets() if "imageDigests.ui=" not in s]
    images = _platform_images(_render(*sets, namespace=NAMESPACE))
    assert images["ui"] == {f"ghcr.io/rkisliak/contractum/ui:{_app_version()}"}
    assert images["identity"] == {
        f"ghcr.io/rkisliak/contractum/identity:{_app_version()}@{DIGESTS['identity']}"
    }


def test_a_local_render_without_digests_is_unchanged() -> None:
    docs = _render("global.imageRegistry=", "global.imageTag=dev", namespace=NAMESPACE)
    images = _platform_images(docs)
    for name, references in images.items():
        assert references == {f"contractum/{name}:dev"}, name
    assert (
        yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))["global"][
            "imageDigests"
        ]
        == {}
    )


def _env(container: dict[str, Any]) -> dict[str, str]:
    return {item["name"]: item.get("value", "") for item in container.get("env", [])}


def test_api_docs_receives_the_platform_version() -> None:
    api_docs = _by_name(_render(namespace=NAMESPACE))[("api-docs", "api-docs")]
    assert _env(api_docs)["DC_VERSION"] == _app_version()
    explicit = _by_name(_render("global.imageTag=1.2.3", namespace=NAMESPACE))
    assert _env(explicit[("api-docs", "api-docs")])["DC_VERSION"] == "1.2.3"
