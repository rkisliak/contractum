"""The committed UI OpenAPI snapshot matches the platform's routes (UI-06, D-UI-08-30, T-8-20).

Regenerates the merged document in memory through ``scripts/export_openapi.py`` and compares its
rendering with ``ui/openapi/openapi.json``. A backend route or schema change without
``uv run python scripts/export_openapi.py`` (then ``pnpm --dir ui gen:api``) fails here, in the
unit step, with no Node needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.unit

EXPORT = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"
SNAPSHOT = Path(__file__).resolve().parents[2] / "ui" / "openapi" / "openapi.json"


@pytest.fixture(scope="module")
def export() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi_under_test", EXPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def document(export: ModuleType, service_apps: dict[str, FastAPI]) -> dict[str, Any]:
    doc: dict[str, Any] = export.merged_document(service_apps)
    return doc


def test_snapshot_is_current(export: ModuleType, document: dict[str, Any]) -> None:
    committed = SNAPSHOT.read_text(encoding="utf-8")
    assert export.render(document) == committed, export.STALE_HINT


def test_render_is_deterministic(export: ModuleType, document: dict[str, Any]) -> None:
    first = export.render(document)
    second = export.render(export.merged_document())
    assert first == second
    assert first.endswith("}\n")
    assert not first.endswith("\n\n")


def test_document_has_the_self_description_routes(document: dict[str, Any]) -> None:
    paths = document["paths"]
    assert "/api/v1/identity/me" in paths
    assert "/api/v1/identity/me/capabilities" in paths


def test_document_has_no_internal_paths(document: dict[str, Any]) -> None:
    assert [path for path in document["paths"] if "/internal" in path] == []
