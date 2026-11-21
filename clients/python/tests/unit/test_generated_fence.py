"""The generated client is fenced: one importer, a closed read-only operation set, httpx as the
only HTTP library, and a drift check that never writes to the committed tree."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
import shutil
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from contractum import _api

SDK_ROOT = Path(__file__).resolve().parents[2]
SRC = SDK_ROOT / "src"
PACKAGE = SRC / "contractum"
GENERATED = PACKAGE / "_generated"
API_PREFIX = "contractum._generated.api"
ALLOWED_THIRD_PARTY = {"httpx", "attrs", "contractum"}


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    """Load scripts/generate_client.py by path (it is not part of the package)."""
    path = SDK_ROOT / "scripts" / "generate_client.py"
    spec = importlib.util.spec_from_file_location("generate_client_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(SRC).with_suffix("").parts)


def _imported_names(path: Path) -> set[str]:
    """Every dotted name a file imports, relative imports resolved against its package."""
    package = _module_name(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - (node.level - 1)] if node.level else []
            module = ".".join([*base, *(node.module.split(".") if node.module else [])])
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def _sources() -> list[Path]:
    return [p for p in sorted(PACKAGE.rglob("*.py")) if GENERATED not in p.parents]


def test_only_api_imports_generated_operation_modules() -> None:
    importers = {
        path.name: sorted(n for n in _imported_names(path) if n.startswith(API_PREFIX + "."))
        for path in _sources()
    }
    assert {name for name, hits in importers.items() if hits} == {"_api.py"}
    imported_operations = {
        n
        for n in _imported_names(PACKAGE / "_api.py")
        if n.startswith(API_PREFIX + ".") and n.count(".") == API_PREFIX.count(".") + 2
    }
    assert imported_operations == set(_api.ALLOWED_OPERATION_MODULES)


def test_no_dynamic_import_of_generated_operations_outside_api() -> None:
    for path in _sources():
        if path.name != "_api.py":
            assert "_generated.api" not in path.read_text(encoding="utf-8"), path.name


def _sample_arguments(function: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, parameter in inspect.signature(function).parameters.items():
        if parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.annotation is uuid.UUID:
            values[name] = uuid.uuid4()
        else:
            values[name] = "1.0.0"
    return values


def test_allowed_operations_are_all_get() -> None:
    assert len(_api.ALLOWED_OPERATION_MODULES) == 10
    for name in _api.ALLOWED_OPERATION_MODULES:
        module = importlib.import_module(name)
        kwargs = module._get_kwargs(**_sample_arguments(module._get_kwargs))
        assert kwargs["method"] == "get", name
        assert kwargs["url"].startswith("/api/v1/")


def test_no_generated_operation_module_sends_anything_but_get() -> None:
    operations = sorted((GENERATED / "api").rglob("*.py"))
    names = [path.stem for path in operations if path.stem != "__init__"]
    assert names
    assert all(name.endswith("_get") for name in names), names


def test_operations_file_matches_the_committed_pruned_spec() -> None:
    lines = (SDK_ROOT / "openapi" / "operations.txt").read_text(encoding="utf-8").splitlines()
    assert lines == sorted(lines)
    listed = {(m, p) for m, p in (line.split(None, 1) for line in lines)}
    spec = json.loads((SDK_ROOT / "openapi" / "consumer.openapi.json").read_text("utf-8"))
    in_spec = {
        (method.upper(), route)
        for route, item in spec["paths"].items()
        for method in item
        if method in {"get", "put", "post", "delete", "patch", "head", "options"}
    }
    assert listed == in_spec
    assert len(listed) == 10
    assert {method for method, _route in listed} == {"GET"}


def test_pruned_spec_has_no_browser_cookie_or_delivery_header_parameter() -> None:
    spec = json.loads((SDK_ROOT / "openapi" / "consumer.openapi.json").read_text("utf-8"))
    names = {
        (parameter["in"], parameter["name"].lower())
        for item in spec["paths"].values()
        for operation in item.values()
        for parameter in operation.get("parameters", [])
    }
    assert ("header", "x-token-delivery") not in names
    assert ("cookie", "__secure-dc_refresh") not in names
    assert not {location for location, _name in names} & {"cookie"}


def test_committed_pruned_spec_equals_a_fresh_prune(generator: ModuleType) -> None:
    if not generator.SOURCE_SPEC.exists():
        pytest.skip("merged OpenAPI snapshot not present")
    assert generator.build_pruned_text() == generator.PRUNED_SPEC.read_text(encoding="utf-8")


def test_generated_code_uses_httpx_only_and_imports_are_closed() -> None:
    stdlib = set(sys.stdlib_module_names)
    for path in sorted(GENERATED.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "httpx2" not in text, path
        for node in ast.walk(ast.parse(text)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                assert top in stdlib or top in ALLOWED_THIRD_PARTY, (path.name, name)


def _copy_committed_tree(tmp_path: Path) -> tuple[Path, Path]:
    tree = tmp_path / "tree"
    shutil.copytree(GENERATED, tree, ignore=shutil.ignore_patterns("__pycache__"))
    spec = tmp_path / "consumer.openapi.json"
    shutil.copyfile(SDK_ROOT / "openapi" / "consumer.openapi.json", spec)
    return tree, spec


def _arrange_check(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path, Path]:
    """Point the script at copies; ``generate`` returns a pristine regeneration (no uvx run)."""
    committed, spec = _copy_committed_tree(tmp_path)
    fresh = tmp_path / "fresh"
    shutil.copytree(GENERATED, fresh, ignore=shutil.ignore_patterns("__pycache__"))
    fresh_text = (SDK_ROOT / "openapi" / "consumer.openapi.json").read_text("utf-8")
    monkeypatch.setattr(generator, "GENERATED", committed)
    monkeypatch.setattr(generator, "PRUNED_SPEC", spec)
    monkeypatch.setattr(generator, "generate", lambda _scratch: (fresh_text, fresh))
    return committed, spec, fresh


def test_check_passes_on_an_identical_tree(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _arrange_check(generator, monkeypatch, tmp_path)
    assert generator.check() == 0


def test_check_fails_on_one_changed_byte_and_never_writes(
    generator: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    committed, spec, _fresh = _arrange_check(generator, monkeypatch, tmp_path)
    victim = committed / "client.py"
    victim.write_bytes(victim.read_bytes() + b"#")
    before = generator.snapshot_tree(committed)
    spec_before = spec.read_bytes()
    assert generator.check() == 1
    assert 'clients/python client is stale: run "cd clients/python' in capsys.readouterr().err
    assert generator.snapshot_tree(committed) == before
    assert spec.read_bytes() == spec_before


def test_check_fails_when_the_pruned_spec_or_a_file_set_differs(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    committed, spec, _fresh = _arrange_check(generator, monkeypatch, tmp_path)
    spec.write_text("{}\n", encoding="utf-8")
    assert generator.check() == 1
    spec.write_bytes((SDK_ROOT / "openapi" / "consumer.openapi.json").read_bytes())
    assert generator.check() == 0
    (committed / "extra.py").write_text("x = 1\n", encoding="utf-8")
    assert generator.check() == 1
    shutil.rmtree(committed)
    assert generator.check() == 1


def test_check_fails_when_the_pruned_spec_is_missing(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _committed, spec, _fresh = _arrange_check(generator, monkeypatch, tmp_path)
    spec.unlink()
    assert generator.check() == 1


def test_swap_in_replaces_the_target_and_leaves_no_siblings(
    generator: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "_generated"
    target.mkdir()
    (target / "old.py").write_text("old\n", encoding="utf-8")
    new = tmp_path / "new"
    new.mkdir()
    (new / "fresh.py").write_text("fresh\n", encoding="utf-8")
    generator.swap_in(new, target)
    assert sorted(p.name for p in target.iterdir()) == ["fresh.py"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["_generated", "new"]
    stale = tmp_path / "._generated.old"
    stale.mkdir()
    generator.swap_in(new, target)
    assert not stale.exists()


def test_prune_spec_keeps_only_allowed_operations_and_reachable_schemas(
    generator: ModuleType,
) -> None:
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/a": {
                "get": {
                    "parameters": [
                        {"name": "X-Token-Delivery", "in": "header"},
                        {"name": "__Secure-dc_refresh", "in": "cookie"},
                        {"name": "q", "in": "query"},
                    ],
                    "responses": {"200": {"$ref": "#/components/schemas/A"}},
                },
                "delete": {"responses": {}},
            },
            "/b": {"get": {"responses": {}}},
            "/c": {"post": {"parameters": [{"name": "X-Token-Delivery", "in": "header"}]}},
        },
        "components": {
            "schemas": {
                "A": {"properties": {"b": {"$ref": "#/components/schemas/B"}}},
                "B": {"type": "string"},
                "Unused": {"type": "string"},
            },
            "securitySchemes": {"s": {}},
        },
    }
    pruned = generator.prune_spec(spec, [("GET", "/a"), ("POST", "/c")])
    assert sorted(pruned["paths"]) == ["/a", "/c"]
    assert list(pruned["paths"]["/a"]) == ["get"]
    assert pruned["paths"]["/a"]["get"]["parameters"] == [{"name": "q", "in": "query"}]
    assert "parameters" not in pruned["paths"]["/c"]["post"]
    assert sorted(pruned["components"]["schemas"]) == ["A", "B"]
    assert "securitySchemes" in pruned["components"]
    with pytest.raises(SystemExit):
        generator.prune_spec(spec, [("GET", "/missing")])


def test_patch_httpx2_rewrites_imports_and_references_only(
    generator: ModuleType, tmp_path: Path
) -> None:
    api = tmp_path / "api"
    api.mkdir()
    module = api / "op.py"
    module.write_text(
        "import httpx2\n\n"
        "def f(response: httpx2.Response) -> httpx2.Client:\n"
        '    """Raises httpx2.TimeoutException."""\n'
        "    my_httpx2_value = 1\n"
        "    return httpx2.Client()\n",
        encoding="utf-8",
    )
    (tmp_path / "plain.py").write_text("x = 1\n", encoding="utf-8")
    assert generator.patch_httpx2(tmp_path) == 5
    text = module.read_text(encoding="utf-8")
    assert "import httpx\n" in text
    assert "response: httpx.Response) -> httpx.Client:" in text
    assert "Raises httpx.TimeoutException." in text
    assert "my_httpx2_value = 1" in text
    assert "return httpx.Client()" in text
    assert (tmp_path / "plain.py").read_text(encoding="utf-8") == "x = 1\n"
    ast.parse(text)


def test_patch_httpx2_aborts_when_nothing_is_replaced(
    generator: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "empty.py").write_text("import httpx\nx = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        generator.patch_httpx2(tmp_path)


def test_read_operations_skips_blank_and_comment_lines(
    generator: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "ops.txt"
    path.write_text("# c\n\nget /a\nPOST /b/{id}\n", encoding="utf-8")
    assert generator.read_operations(path) == [("GET", "/a"), ("POST", "/b/{id}")]
