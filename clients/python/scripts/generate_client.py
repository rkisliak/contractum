"""Generate the typed low-level client of the SDK from the merged Contractum OpenAPI document.

Purpose (D-10): the SDK talks to the server through a client generated from
``ui/openapi/openapi.json``, restricted to the consumer operations listed in
``openapi/operations.txt`` (GET consumer operations only; the SDK sends no POST).

Pipeline:

1. Read the merged spec, keep only the allow-listed operations, drop the ``X-Token-Delivery``
   header and the refresh cookie parameters should an operation carry them (the SDK never uses
   browser sessions or cookies), prune unreachable component schemas and write the result to
   ``openapi/consumer.openapi.json`` (sorted keys, two-space indent, trailing newline).
2. Run ``openapi-python-client`` pinned to 0.30.0 (D-45) with ``--fail-on-warning``.
3. Post-process: 0.30 writes its code against ``httpx2``, the successor fork of ``httpx`` with
   the same API. The SDK keeps one HTTP stack, ``httpx`` (its transport guard and the ``respx``
   test mocks are built on it), so every ``httpx2`` import and reference becomes ``httpx``.
4. Swap the result into ``src/contractum/_generated/`` atomically.

Usage (run from ``clients/python``; no server code is imported)::

    uv run python scripts/generate_client.py            regenerate the committed client
    uv run python scripts/generate_client.py --check    exit 1 when the committed client is stale

``--check`` (D-11) regenerates into a temporary directory and never writes to the committed tree.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess  # noqa: S404 - the generator is run with a fixed argv, never a shell
import sys
import tempfile
from pathlib import Path
from typing import Any

SDK_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_SPEC = REPO_ROOT / "ui" / "openapi" / "openapi.json"
OPENAPI_DIR = SDK_ROOT / "openapi"
OPERATIONS = OPENAPI_DIR / "operations.txt"
GENERATOR_CONFIG = OPENAPI_DIR / "generator.yaml"
PRUNED_SPEC = OPENAPI_DIR / "consumer.openapi.json"
GENERATED = SDK_ROOT / "src" / "contractum" / "_generated"

GENERATOR = "openapi-python-client@0.30.0"
GENERATOR_PYTHON = "3.13"
STALE_HINT = (
    'clients/python client is stale: run "cd clients/python && '
    'uv run python scripts/generate_client.py"'
)
DROPPED_HEADERS = frozenset({"x-token-delivery"})
DROPPED_COOKIES = frozenset({"__secure-dc_refresh"})
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
HTTPX2 = re.compile(r"\bhttpx2\b")
IGNORED_PARTS = frozenset({"__pycache__"})


def read_operations(path: Path = OPERATIONS) -> list[tuple[str, str]]:
    """Return the allow-listed ``(METHOD, path)`` pairs from the operations file."""
    pairs: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            method, route = text.split(None, 1)
            pairs.append((method.upper(), route))
    return pairs


def _refs(node: Any) -> set[str]:
    """Collect every ``#/components/schemas/<name>`` reference below ``node``."""
    found: set[str] = set()
    prefix = "#/components/schemas/"
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            ref = item.get("$ref")
            if isinstance(ref, str) and ref.startswith(prefix):
                found.add(ref[len(prefix) :])
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _keep_parameter(parameter: dict[str, Any]) -> bool:
    name = str(parameter.get("name", "")).lower()
    location = parameter.get("in")
    if location == "header" and name in DROPPED_HEADERS:
        return False
    return not (location == "cookie" and name in DROPPED_COOKIES)


def prune_spec(spec: dict[str, Any], operations: list[tuple[str, str]]) -> dict[str, Any]:
    """Restrict ``spec`` to ``operations`` and to the component schemas they reach."""
    allowed: dict[str, set[str]] = {}
    for method, route in operations:
        allowed.setdefault(route, set()).add(method.lower())
    paths: dict[str, Any] = {}
    seen: set[tuple[str, str]] = set()
    for route, item in spec.get("paths", {}).items():
        methods = allowed.get(route)
        if not methods:
            continue
        kept: dict[str, Any] = {}
        for key, value in item.items():
            if key in HTTP_METHODS:
                if key not in methods:
                    continue
                seen.add((key.upper(), route))
                operation = dict(value)
                if "parameters" in operation:
                    operation["parameters"] = [
                        p for p in operation["parameters"] if _keep_parameter(p)
                    ]
                    if not operation["parameters"]:
                        del operation["parameters"]
                kept[key] = operation
            else:
                kept[key] = value
        paths[route] = kept
    missing = sorted(set(operations) - seen)
    if missing:
        raise SystemExit(f"operations missing from {SOURCE_SPEC.name}: {missing}")
    pruned: dict[str, Any] = {k: v for k, v in spec.items() if k not in ("paths", "components")}
    pruned["paths"] = paths
    components = dict(spec.get("components", {}))
    schemas = components.get("schemas", {})
    reachable: set[str] = set()
    pending = _refs(paths)
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending |= _refs(schemas.get(name, {})) - reachable
    components["schemas"] = {name: schemas[name] for name in sorted(reachable) if name in schemas}
    pruned["components"] = components
    return pruned


def render_spec(spec: dict[str, Any]) -> str:
    """Render deterministically: sorted keys, two-space indent, one trailing newline."""
    return json.dumps(spec, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def build_pruned_text() -> str:
    """Prune the merged spec to the allow-list and render it."""
    spec = json.loads(SOURCE_SPEC.read_text(encoding="utf-8"))
    return render_spec(prune_spec(spec, read_operations()))


def patch_httpx2(root: Path) -> int:
    """Rewrite every ``httpx2`` import and reference below ``root`` to ``httpx``."""
    total = 0
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        patched, count = HTTPX2.subn("httpx", text)
        if count:
            path.write_text(patched, encoding="utf-8")
            total += count
    if total == 0:
        raise SystemExit("post-processing found no httpx2 reference to replace")
    return total


def run_generator(pruned: Path, output: Path) -> None:
    """Run the pinned generator with a fixed argv."""
    command = [
        "uvx",
        "--python",
        GENERATOR_PYTHON,
        GENERATOR,
        "generate",
        "--path",
        str(pruned),
        "--output-path",
        str(output),
        "--meta",
        "none",
        "--config",
        str(GENERATOR_CONFIG),
        "--fail-on-warning",
    ]
    result = subprocess.run(command, check=False, cwd=SDK_ROOT)  # noqa: S603
    if result.returncode != 0:
        raise SystemExit(f"{GENERATOR} failed with exit code {result.returncode}")


def generate(scratch: Path) -> tuple[str, Path]:
    """Prune, generate and post-process inside ``scratch``; return spec text and tree."""
    text = build_pruned_text()
    pruned = scratch / "consumer.openapi.json"
    pruned.write_text(text, encoding="utf-8")
    output = scratch / "out"
    run_generator(pruned, output)
    for cache in output.rglob("__pycache__"):
        shutil.rmtree(cache)
    patch_httpx2(output)
    return text, output


def snapshot_tree(root: Path) -> dict[str, bytes]:
    """Map sorted relative paths to file bytes, ignoring bytecode caches."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_file() and not IGNORED_PARTS.intersection(relative.parts):
            files[relative.as_posix()] = path.read_bytes()
    return files


def swap_in(new_tree: Path, target: Path) -> None:
    """Replace ``target`` with ``new_tree``: sibling temp dir, old aside, new in, old deleted."""
    staging = target.with_name(f".{target.name}.new")
    aside = target.with_name(f".{target.name}.old")
    for leftover in (staging, aside):
        if leftover.exists():
            shutil.rmtree(leftover)
    shutil.copytree(new_tree, staging)
    if target.exists():
        os.replace(target, aside)
    os.replace(staging, target)
    if aside.exists():
        shutil.rmtree(aside)


def check() -> int:
    """Return 1 when the committed pruned spec or generated tree differs from a regeneration."""
    with tempfile.TemporaryDirectory(prefix="contractum-gen-") as scratch:
        text, output = generate(Path(scratch))
        committed = PRUNED_SPEC.read_text(encoding="utf-8") if PRUNED_SPEC.exists() else None
        fresh = snapshot_tree(output)
        current = snapshot_tree(GENERATED) if GENERATED.exists() else {}
        if committed != text or fresh != current:
            print(STALE_HINT, file=sys.stderr)
            return 1
    return 0


def regenerate() -> int:
    """Regenerate the pruned spec and the generated tree in place."""
    with tempfile.TemporaryDirectory(prefix="contractum-gen-") as scratch:
        text, output = generate(Path(scratch))
        swap_in(output, GENERATED)
        PRUNED_SPEC.write_text(text, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the committed client is stale; never writes to the committed tree",
    )
    arguments = parser.parse_args(argv)
    return check() if arguments.check else regenerate()


if __name__ == "__main__":
    sys.exit(main())
