"""The gateway's merge of the services' OpenAPI documents (PLAT-06, D-176, D-177, T-7-12, T-7-13).

Runs ``gateway/api-docs/merge.py`` (loaded by path: the gateway imports nothing from the
platform) over the four routed services' in-process documents, so a colliding model name or a
duplicate operation fails CI before any deploy, and pins the collision policy on fixtures.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.unit

MERGE = Path(__file__).resolve().parents[2] / "gateway" / "api-docs" / "merge.py"
ROUTED = ("identity", "contracts", "validation", "audit")
METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gateway_api_docs_merge", MERGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def merge() -> ModuleType:
    return _load()


def _op(operation_id: str) -> dict[str, Any]:
    return {"operationId": operation_id, "responses": {"200": {"description": "OK"}}}


def _spec(
    paths: dict[str, Any] | None = None, schemas: dict[str, Any] | None = None
) -> dict[str, Any]:
    spec: dict[str, Any] = {"openapi": "3.1.0", "paths": paths or {}}
    if schemas is not None:
        spec["components"] = {"schemas": schemas}
    return spec


# --- the real services ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def merged(merge: ModuleType, service_apps: dict[str, FastAPI]) -> dict[str, Any]:
    documents = {name: service_apps[name].openapi() for name in ROUTED}
    result: dict[str, Any] = merge.merge_specs(documents)
    return result


def test_the_four_services_merge_without_a_collision(
    merged: dict[str, Any], service_apps: dict[str, FastAPI]
) -> None:
    assert merged["openapi"] == "3.1.0"
    assert merged["info"] == {
        "title": "Contractum API",
        "description": "Contractum: data contract governance",
        "version": "v1",
    }
    assert merged["servers"] == [{"url": "/"}]
    assert merged["components"]["securitySchemes"] == {
        "bearerAuth": {"type": "http", "scheme": "bearer"}
    }
    expected_paths = {p for name in ROUTED for p in service_apps[name].openapi()["paths"]}
    assert set(merged["paths"]) == expected_paths
    for module in ("identity", "contracts", "validation"):
        assert any(p.startswith(f"/api/v1/{module}/") for p in merged["paths"]), module


def test_the_merged_document_has_no_internal_path_and_every_operation_id_once(
    merged: dict[str, Any],
) -> None:
    assert not [p for p in merged["paths"] if "/internal" in p]
    operation_ids = [
        op["operationId"]
        for item in merged["paths"].values()
        for method, op in item.items()
        if method in METHODS
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_every_ref_in_the_merged_document_resolves(merged: dict[str, Any]) -> None:
    refs: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    refs.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(merged)
    assert refs
    for ref in refs:
        _, section, name = ref.removeprefix("#/").split("/", 2)
        assert name in merged["components"][section], ref


# --- the collision policy (D-177) ---------------------------------------------------------------


def test_identical_components_are_kept_once(merge: ModuleType) -> None:
    problem = {"type": "object", "properties": {"type": {"type": "string"}}}
    out = merge.merge_specs(
        {
            "a": _spec({"/api/v1/a/x": {"get": _op("a_x")}}, {"Problem": problem}),
            "b": _spec({"/api/v1/b/y": {"get": _op("b_y")}}, {"Problem": dict(problem)}),
        }
    )
    assert out["components"]["schemas"] == {"Problem": problem}
    assert set(out["paths"]) == {"/api/v1/a/x", "/api/v1/b/y"}


def test_key_order_does_not_make_components_different(merge: ModuleType) -> None:
    first = {"type": "object", "title": "Thing"}
    second = {"title": "Thing", "type": "object"}
    out = merge.merge_specs({"a": _spec(schemas={"T": first}), "b": _spec(schemas={"T": second})})
    assert list(out["components"]["schemas"]) == ["T"]


def test_the_same_component_name_with_different_content_names_both_services(
    merge: ModuleType,
) -> None:
    with pytest.raises(merge.MergeError, match=r"component schemas/Thing differs: a vs b"):
        merge.merge_specs(
            {
                "a": _spec(schemas={"Thing": {"type": "object"}}),
                "b": _spec(schemas={"Thing": {"type": "string"}}),
            }
        )


def test_a_service_bearer_scheme_that_differs_from_the_gateway_one_is_refused(
    merge: ModuleType,
) -> None:
    spec = {
        "paths": {},
        "components": {"securitySchemes": {"bearerAuth": {"type": "apiKey", "in": "header"}}},
    }
    with pytest.raises(merge.MergeError, match=r"bearerAuth differs: gateway vs a"):
        merge.merge_specs({"a": spec})


def test_a_duplicate_operation_id_names_both_services(merge: ModuleType) -> None:
    with pytest.raises(merge.MergeError, match=r"operationId list_things in a and b"):
        merge.merge_specs(
            {
                "a": _spec({"/api/v1/a/things": {"get": _op("list_things")}}),
                "b": _spec({"/api/v1/b/things": {"get": _op("list_things")}}),
            }
        )


def test_a_duplicate_path_names_both_services(merge: ModuleType) -> None:
    # Service names are unique keys, so two owners of one path need nested prefixes: the path
    # sits under both /api/v1/a/ and /api/v1/a/sub/.
    with pytest.raises(merge.MergeError, match=r"path /api/v1/a/sub/x in a and a/sub"):
        merge.merge_specs(
            {
                "a": _spec({"/api/v1/a/sub/x": {"get": _op("one")}}),
                "a/sub": _spec({"/api/v1/a/sub/x": {"get": _op("two")}}),
            }
        )


def test_a_path_outside_the_owning_service_prefix_is_refused(merge: ModuleType) -> None:
    with pytest.raises(merge.MergeError, match=r"path /api/v1/b/x of a is outside"):
        merge.merge_specs({"a": _spec({"/api/v1/b/x": {"get": _op("x")}})})


@pytest.mark.parametrize("path", ["/internal/v1/events", "/healthz", "/api/v1/ab/x", "/api/v1/a"])
def test_internal_health_and_lookalike_paths_are_refused(merge: ModuleType, path: str) -> None:
    with pytest.raises(merge.MergeError, match="outside"):
        merge.merge_specs({"a": _spec({path: {"get": _op("x")}})})


def test_a_spec_with_no_paths_contributes_only_its_components(merge: ModuleType) -> None:
    out = merge.merge_specs(
        {
            "a": _spec({"/api/v1/a/x": {"get": _op("a_x")}}),
            "b": _spec(schemas={"OnlyB": {"type": "object"}}),
        }
    )
    assert set(out["paths"]) == {"/api/v1/a/x"}
    assert out["components"]["schemas"] == {"OnlyB": {"type": "object"}}


def test_tags_are_merged_by_name(merge: ModuleType) -> None:
    a = {"paths": {}, "tags": [{"name": "shared"}, {"name": "a"}]}
    b = {"paths": {}, "tags": [{"name": "shared"}, {"name": "b"}]}
    out = merge.merge_specs({"a": a, "b": b})
    assert [t["name"] for t in out["tags"]] == ["shared", "a", "b"]
    with pytest.raises(merge.MergeError, match=r"tag shared differs: a vs c"):
        merge.merge_specs({"a": a, "c": {"paths": {}, "tags": [{"name": "shared", "x": 1}]}})


def test_the_merge_never_mutates_its_inputs(merge: ModuleType) -> None:
    spec = _spec({"/api/v1/a/x": {"get": _op("a_x")}}, {"T": {"type": "object"}})
    out = merge.merge_specs({"a": spec})
    out["paths"]["/api/v1/a/x"]["get"]["operationId"] = "changed"
    out["components"]["schemas"]["T"]["type"] = "string"
    assert spec["paths"]["/api/v1/a/x"]["get"]["operationId"] == "a_x"
    assert spec["components"]["schemas"]["T"]["type"] == "object"
