"""The two data files behind the API-04 proof are well formed (D-181 parts 1 and 3, T-7-34).

* ``tests/e2e/api04_areas.json`` (the gap audit as data): exactly the twelve REQUIREMENTS API-04
  areas, none empty; every listed operation exists in the merged document of the routed services
  with exactly that permission declared by its route; every permission is registered in
  ``dc_auth.roles`` with the access class the file claims; the interactive-only list equals the
  ``interactive_only`` specs of ``dc_auth.roles``. The e2e image has no ``dc_*`` package, which is
  why the matrix reads these from the file and this test pins the file to the code.
* ``tests/e2e/operation_exemptions.json`` (operations the e2e suite does not exercise): every key
  is an operationId of the merged document and every reason is a sentence.

The merged document is built in process: ``gateway/api-docs/merge.py`` (loaded by path, as in
``test_openapi_merge.py``) over the services' own documents from the ``service_apps`` fixture.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from dc_auth.roles import PERMISSIONS
from dc_core.policy import find_policy
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
MERGE = REPO / "gateway" / "api-docs" / "merge.py"
AREAS_FILE = REPO / "tests" / "e2e" / "api04_areas.json"
EXEMPTIONS_FILE = REPO / "tests" / "e2e" / "operation_exemptions.json"
ROUTED = ("identity", "contracts", "validation", "audit")
METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
API04_AREAS = (
    "folders",
    "groups",
    "grants",
    "tokens",
    "contracts",
    "versions",
    "publish/promote/unpublish/deprecate",
    "templates",
    "rules",
    "rule sets",
    "validation",
    "audit queries",
)


def _load_merge() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gateway_api_docs_merge", MERGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def merged(service_apps: dict[str, FastAPI]) -> dict[str, Any]:
    documents = {name: service_apps[name].openapi() for name in ROUTED}
    result: dict[str, Any] = _load_merge().merge_specs(documents)
    return result


@pytest.fixture(scope="module")
def operations(merged: dict[str, Any]) -> dict[tuple[str, str], str]:
    """(METHOD, path template) -> operationId of every public operation."""
    return {
        (method.upper(), path): str(operation["operationId"])
        for path, item in merged["paths"].items()
        for method, operation in item.items()
        if method in METHODS
    }


@pytest.fixture(scope="module")
def route_permissions(service_apps: dict[str, FastAPI]) -> dict[tuple[str, str], str | None]:
    """(METHOD, path) -> the permission the route's require() declares (None when public)."""
    found: dict[tuple[str, str], str | None] = {}
    for name in ROUTED:
        for rc in iter_route_contexts(service_apps[name].routes):
            if not isinstance(rc.original_route, APIRoute):
                continue
            meta = find_policy(rc.dependant)
            for method in rc.methods or ():
                found[(method, rc.path or "")] = None if meta is None else meta.permission
    return found


@pytest.fixture(scope="module")
def areas_file() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(AREAS_FILE.read_text(encoding="utf-8"))
    return data


# --- api04_areas.json -------------------------------------------------------------------------


def test_the_area_map_names_exactly_the_twelve_api04_areas(areas_file: dict[str, Any]) -> None:
    assert list(areas_file["areas"]) == list(API04_AREAS)
    for area, entries in areas_file["areas"].items():
        assert entries, f"area {area} lists no operation"
        assert any(entry["kind"] == "read" for entry in entries), f"area {area} has no read"


def test_every_listed_operation_exists_with_the_permission_its_route_declares(
    areas_file: dict[str, Any],
    operations: dict[tuple[str, str], str],
    route_permissions: dict[tuple[str, str], str | None],
) -> None:
    for area, entries in areas_file["areas"].items():
        for entry in entries:
            assert set(entry) == {"method", "path", "kind", "permission"}, (area, entry)
            key = (entry["method"], entry["path"])
            assert key in operations, f"{area}: {key} is not in the merged document"
            assert route_permissions[key] == entry["permission"], (area, key, entry)


def test_every_listed_permission_is_registered_with_the_claimed_access_class(
    areas_file: dict[str, Any],
) -> None:
    for area, entries in areas_file["areas"].items():
        for entry in entries:
            spec = PERMISSIONS.get(entry["permission"])
            assert spec is not None, (area, entry)
            assert entry["kind"] in ("read", "write"), (area, entry)
            assert spec.access == entry["kind"], (area, entry, spec.access)


def test_the_interactive_only_list_equals_the_registry(areas_file: dict[str, Any]) -> None:
    golden = {name for name, spec in PERMISSIONS.items() if spec.interactive_only}
    listed = areas_file["interactive_only"]
    assert len(listed) == len(set(listed))
    assert set(listed) == golden


# --- operation_exemptions.json ----------------------------------------------------------------


def test_every_exemption_is_a_merged_operation_with_a_reason(
    operations: dict[tuple[str, str], str],
) -> None:
    assert EXEMPTIONS_FILE.is_file(), f"{EXEMPTIONS_FILE.name} is missing"
    exemptions = json.loads(EXEMPTIONS_FILE.read_text(encoding="utf-8"))
    assert isinstance(exemptions, dict)
    known = set(operations.values())
    unknown = sorted(set(exemptions) - known)
    assert not unknown, f"exemptions for operations not in the merged document: {unknown}"
    for operation_id, reason in exemptions.items():
        assert isinstance(reason, str), operation_id
        text = reason.strip()
        assert text, f"{operation_id}: empty reason"
        assert len(text.split()) >= 4 and text.endswith("."), (
            f"{operation_id}: the reason must be a sentence, got {reason!r}"
        )
