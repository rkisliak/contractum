"""The generated HTTP API reference page matches the committed OpenAPI and authorization matrix.

Loads ``scripts/gen_http_reference.py`` by path. A route, summary or permission change without
``uv run python scripts/gen_http_reference.py`` fails here, in the unit step.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "gen_http_reference.py"
METHODS = ("get", "put", "post", "delete", "patch")
# Internal ids that must never reach user docs: decision ids (D-12, D-UI-08-30), requirement ids
# (VAL-03, SITE-01) and quick task ids (261121-psn).
INTERNAL_ID = re.compile(r"\bD-(?:[A-Z]+-)*\d+\b|\b[A-Z]{2,}-\d{2,}\b|\b\d{6}-[a-z0-9]{3}\b")


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_http_reference_under_test", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def spec_doc(gen: ModuleType) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(gen.SPEC.read_text(encoding="utf-8"))
    return doc


@pytest.fixture(scope="module")
def matrix(gen: ModuleType) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(gen.MATRIX.read_text(encoding="utf-8"))
    return doc


def _operations(doc: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method.upper(), path, op)
        for path, item in doc["paths"].items()
        for method, op in item.items()
        if method in METHODS
    ]


def test_committed_page_is_current(gen: ModuleType) -> None:
    committed = gen.TARGET.read_text(encoding="utf-8")
    assert gen.render(gen.load_spec(), gen.load_matrix()) == committed, gen.STALE_HINT


def test_check_mode_passes_on_the_committed_page(gen: ModuleType) -> None:
    assert gen.main(["--check"]) == 0


def test_summary_change_changes_the_output(
    gen: ModuleType, spec_doc: dict[str, Any], matrix: dict[str, Any]
) -> None:
    changed = copy.deepcopy(spec_doc)
    _, _, op = _operations(changed)[0]
    op["summary"] = "A summary nobody wrote"
    assert gen.render(changed, matrix) != gen.render(spec_doc, matrix)
    assert "A summary nobody wrote" in gen.render(changed, matrix)


def test_check_mode_reports_a_stale_page_without_writing(
    gen: ModuleType, spec_doc: dict[str, Any], matrix: dict[str, Any], tmp_path: Path
) -> None:
    target = tmp_path / "http-api.md"
    target.write_text("stale\n", encoding="utf-8")
    assert gen.write_or_check(gen.render(spec_doc, matrix), target, check=True) == 1
    assert target.read_text(encoding="utf-8") == "stale\n"
    missing = tmp_path / "missing.md"
    assert gen.write_or_check("x\n", missing, check=True) == 1
    assert not missing.exists()


def test_write_mode_writes_and_then_checks_clean(gen: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "out" / "http-api.md"
    assert gen.write_or_check("page\n", target, check=False) == 0
    assert target.read_text(encoding="utf-8") == "page\n"
    assert gen.write_or_check("page\n", target, check=True) == 0


def test_every_operation_appears_exactly_once(
    gen: ModuleType, spec_doc: dict[str, Any], matrix: dict[str, Any]
) -> None:
    page = gen.render(spec_doc, matrix)
    rows = [line for line in page.splitlines() if line.startswith("| `")]
    keys = [tuple(cell.strip().strip("`") for cell in row.split("|")[1:3]) for row in rows]
    expected = sorted((method, path) for method, path, _ in _operations(spec_doc))
    assert sorted(keys) == expected
    assert len(set(keys)) == len(keys)


def test_no_description_text_and_no_internal_ids(
    gen: ModuleType, spec_doc: dict[str, Any], matrix: dict[str, Any]
) -> None:
    page = gen.render(spec_doc, matrix)
    assert INTERNAL_ID.findall(page) == []
    for _, _, op in _operations(spec_doc):
        description = (op.get("description") or "").strip()
        first_line = description.splitlines()[0].strip() if description else ""
        if len(first_line) > 20:
            assert first_line not in page


def test_permission_sign_in_and_route_suffix(gen: ModuleType) -> None:
    doc = {
        "paths": {
            "/api/v1/x/open": {
                "get": {
                    "operationId": "open_get",
                    "summary": "Open Route",
                    "tags": ["x"],
                    "security": [],
                }
            },
            "/api/v1/x/guarded": {
                "post": {
                    "operationId": "guarded_post",
                    "summary": "Guard | pipe",
                    "tags": ["x"],
                    "security": [{"bearerAuth": []}],
                },
                "get": {"operationId": "unmatched_get", "summary": "Other", "tags": ["x"]},
            },
        }
    }
    matrix = {
        "open_get": {"permission": None, "target": "public"},
        "guarded_post": {"permission": "thing:write", "target": "folder"},
    }
    page = gen.render(doc, matrix)
    assert "| `GET` | `/api/v1/x/open` | Open | public | no |" in page
    assert "| `POST` | `/api/v1/x/guarded` | Guard \\| pipe | `thing:write` | yes |" in page
    assert "| `GET` | `/api/v1/x/guarded` | Other | - | yes |" in page
    # Rows sorted by path, then method.
    assert page.index("/api/v1/x/guarded` | Other") < page.index("Guard \\| pipe")
    assert page.index("Guard \\| pipe") < page.index("/api/v1/x/open")
    assert page.endswith("|\n")
