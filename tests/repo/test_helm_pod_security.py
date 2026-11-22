"""The chart's pod hardening, rendered with ``helm template`` (code scanning, Trivy KSV rules).

Every resource names its release namespace, every container runs on a read-only root filesystem
without privilege escalation and with all capabilities dropped, and every pod runs as a non-root
user, except the two whose change needs an operator decision (the gateway binds port 443, and the
bundled NATS data volume of an existing release is owned by root). The exceptions are listed here
exactly, so a new one fails the test.
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
NAMESPACE = "contractum"
# Pods that still start as root: the open gateway and NATS decisions of the security audit.
ROOT_PODS = {"gateway", "nats"}
# Pods on the image's own fixed uid at or below 10000: postgres (999) owns its data directory.
LOW_UID_PODS = {"postgres"}


def _render(*args: str) -> list[dict[str, Any]]:
    command = ["helm", "template", "dc", str(CHART), *args]
    out = subprocess.run(command, check=True, capture_output=True, text=True).stdout  # noqa: S603
    return [doc for doc in yaml.safe_load_all(out) if isinstance(doc, dict)]


def _workloads(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc.get("kind") in WORKLOADS]


def _pod(doc: dict[str, Any]) -> dict[str, Any]:
    pod: dict[str, Any] = doc["spec"]["template"]["spec"]
    return pod


def _containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    pod = _pod(doc)
    return [*pod.get("containers", []), *pod.get("initContainers", [])]


@pytest.mark.parametrize("namespace", [NAMESPACE, "contractum-eval"])
def test_every_resource_names_the_release_namespace(namespace: str) -> None:
    docs = _render("--namespace", namespace)
    assert docs
    for doc in docs:
        assert doc["metadata"].get("namespace") == namespace, doc["metadata"]["name"]


def test_every_container_has_a_read_only_root_and_no_privileges() -> None:
    workloads = _workloads(_render("--namespace", NAMESPACE))
    assert {doc["metadata"]["name"] for doc in workloads} >= {"postgres", "nats", "ui", "gateway"}
    for doc in workloads:
        for container in _containers(doc):
            where = f"{doc['metadata']['name']}/{container['name']}"
            context = container.get("securityContext", {})
            assert context.get("readOnlyRootFilesystem") is True, where
            assert context.get("allowPrivilegeEscalation") is False, where
            assert context.get("capabilities", {}).get("drop") == ["ALL"], where


def test_every_pod_but_the_listed_exceptions_runs_as_non_root() -> None:
    starts_as_root = set()
    for doc in _workloads(_render("--namespace", NAMESPACE)):
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
        for doc in _render("--namespace", NAMESPACE)
        if doc["metadata"]["name"] == "postgres" and doc["kind"] == "StatefulSet"
    ]
    context = _pod(postgres)["securityContext"]
    assert (context["runAsUser"], context["runAsGroup"], context["fsGroup"]) == (999, 999, 999)
    mounts = {m["name"] for m in _pod(postgres)["containers"][0]["volumeMounts"]}
    assert {"data", "run", "tmp"} <= mounts
    scratch = {v["name"] for v in _pod(postgres)["volumes"] if "emptyDir" in v}
    assert {"run", "tmp"} <= scratch
