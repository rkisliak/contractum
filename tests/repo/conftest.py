"""Shared repo-test fixtures.

``service_apps`` builds every service app in-process with never-connected settings through
``scripts/export_openapi.py::build_service_apps``, the one construction the UI snapshot export
shares (research Pattern 6). Helpers live here, not in a sibling module, because the suite runs
with ``--import-mode=importlib``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI

EXPORT_OPENAPI = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"


def load_export_openapi() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi", EXPORT_OPENAPI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def service_apps(tmp_path_factory: pytest.TempPathFactory) -> dict[str, FastAPI]:
    apps: dict[str, FastAPI] = load_export_openapi().build_service_apps(
        tmp_path_factory.mktemp("service-apps")
    )
    return apps
