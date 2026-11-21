"""scripts/operation_coverage.py: matching, UI path normalisation and the exemption rules."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "operation_coverage.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("operation_coverage", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oc = _load()


def _ops() -> list[Any]:
    return [
        oc.Op("GET", "/api/v1/x/{a}/{b}", "params"),
        oc.Op("GET", "/api/v1/x/{a}/metadata", "literal"),
        oc.Op("POST", "/api/v1/x", "create"),
    ]


def test_a_literal_segment_beats_a_parameter_and_the_query_is_ignored() -> None:
    ops = _ops()
    assert oc.match(ops, "GET", "/api/v1/x/1/metadata?y=2").id == "literal"
    assert oc.match(ops, "get", "/api/v1/x/1/2").id == "params"
    assert oc.match(ops, "GET", "/api/v1/x/1/2/3") is None
    assert oc.match(ops, "DELETE", "/api/v1/x") is None


@pytest.mark.parametrize(
    ("raw", "concrete"),
    [
        ("/api/v1/x/{a}/metadata", "/api/v1/x/x/metadata"),
        ("*/api/v1/x/:id/metadata", "/api/v1/x/x/metadata"),
        ("/api/v1/x/${card.id}/metadata?tab=1", "/api/v1/x/x/metadata"),
    ],
)
def test_ui_path_literals_become_concrete_paths(raw: str, concrete: str) -> None:
    assert oc._client_path(raw) == concrete


def test_a_template_literal_path_matches_every_fitting_operation() -> None:
    pattern = oc._template_pattern("/api/v1/identity/users/{user_id}/${action}")
    assert pattern.match("/api/v1/identity/users/x/deactivate")
    assert pattern.match("/api/v1/identity/users/x/reactivate")
    assert not pattern.match("/api/v1/identity/users/x")
    assert not pattern.match("/api/v1/identity/users/x/a/b")


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "method": "GET",
        "path": "/p",
        "backend_ok": 1,
        "backend_refused": 1,
        "authz_matrix": True,
        "ui_client": True,
    }
    row.update(overrides)
    return row


def test_a_missing_layer_fails_unless_exempt_and_a_used_exemption_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exemptions = tmp_path / "exemptions.json"
    exemptions.write_text(
        json.dumps(
            {
                "b": {"backend_ok": "reason"},
                "c": {"backend_ok": "reason"},
                "gone": {"backend_ok": "reason"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(oc, "EXEMPTIONS", exemptions)
    rows = {
        "a": _row(backend_refused=0),
        "b": _row(backend_ok=0),
        "c": _row(),
    }
    failing, stale = oc.gaps(rows)
    assert failing == ["a (GET /p): no backend_refused"]
    assert stale == ["c: backend_ok exemption is stale", "gone: exempt but not an operation"]


def test_a_public_operation_needs_no_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oc, "EXEMPTIONS", tmp_path / "none.json")
    failing, _ = oc.gaps({"v": _row(backend_refused=0, public=True)})
    assert failing == []


def test_the_committed_snapshot_has_unique_operation_ids() -> None:
    ids = [op.id for op in oc.load_ops()]
    assert len(ids) == len(set(ids)) >= 100
