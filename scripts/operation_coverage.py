"""Operation coverage across every test layer, and the full gate's operation-coverage check.

For each operation of the merged OpenAPI snapshot (``ui/openapi/openapi.json``) it reports:

* ``backend_ok``: a backend test (unit or integration, in-process through ``httpx.ASGITransport``)
  got a 2xx or 3xx from it. Source: the tracer of the root ``conftest.py``, enabled with
  ``DC_OP_TRACE=<file>`` (default file ``.coverage-data/optrace.jsonl``).
* ``backend_refused``: a backend test got a 401, 403 or 404 from it (an authorization refusal or
  an existence-hiding 404). Not gated for an operation the matrix lists as ``public``.
* ``authz_matrix``: the operation has a row in ``tests/repo/authz_matrix.golden.json`` (the
  generated role x operation matrix, ``tests/repo/test_authz_route_matrix.py``).
* ``e2e``: the gateway e2e suite answered it with a 2xx or 3xx
  (``.dev/phase7-operation-coverage.json``, written by
  ``tests/e2e/test_zz_operation_coverage.py``), or it is exempt there with a reason.
* ``ui_client``: the SPA calls it (a literal path in ``ui/src``, tests excluded).
* ``ui_unit``: a Vitest test mocks it with an msw handler (``ui/src/**``).
* ``ui_e2e``: a Playwright journey received a response from it
  (``ui/test-results/**/api-calls/*.jsonl``, written by the ``apiCalls`` fixture of
  ``ui/e2e/fixtures.ts``), with the specs that did.

    uv run python scripts/operation_coverage.py                 print the summary
    uv run python scripts/operation_coverage.py --markdown F    also write the per-operation table
    uv run python scripts/operation_coverage.py --gate          exit 1 when an operation lacks
                                                                backend_ok, backend_refused, a
                                                                matrix row or a UI screen
                                                                (ui_client) and is not exempt

Exemptions live in ``tests/repo/operation_coverage_exemptions.json`` as
``{"<operationId>": {"<column>": "<reason>"}}`` (column one of ``GATED``; ``ui_client``
exemptions are UI-16's justified exclusions of an operation from every screen); a stale
exemption is printed, never a failure. The JSON report is written to
``.coverage-data/operation-coverage.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "ui" / "openapi" / "openapi.json"
TRACE = Path(os.environ.get("DC_OP_TRACE", ROOT / ".coverage-data" / "optrace.jsonl"))
MATRIX = ROOT / "tests" / "repo" / "authz_matrix.golden.json"
E2E_REPORT = ROOT / ".dev" / "phase7-operation-coverage.json"
E2E_EXEMPTIONS = ROOT / "tests" / "e2e" / "operation_exemptions.json"
EXEMPTIONS = ROOT / "tests" / "repo" / "operation_coverage_exemptions.json"
UI_SRC = ROOT / "ui" / "src"
UI_RESULTS = ROOT / "ui" / "test-results"
REPORT = ROOT / ".coverage-data" / "operation-coverage.json"
METHODS = ("get", "post", "put", "patch", "delete")
GATED = ("backend_ok", "backend_refused", "authz_matrix", "ui_client")
_PARAM = re.compile(r"^\{[^/{}]+\}$")
_CLIENT_CALL = re.compile(r"\.(GET|POST|PUT|PATCH|DELETE)\(\s*[\"'`](/api/v1/[^\"'`]+)[\"'`]")
_TEMPLATE = re.compile(r"`(/api/v1/[^`]*\$\{[^`]*)`")
_MSW = re.compile(r"http\.(get|post|put|patch|delete)\(\s*[\"'`]([^\"'`]+)[\"'`]")


class Op:
    def __init__(self, method: str, path: str, operation_id: str) -> None:
        self.method = method
        self.path = path
        self.id = operation_id
        segments = path.strip("/").split("/")
        self.literals = sum(1 for s in segments if not _PARAM.match(s))
        parts = ["[^/]+" if _PARAM.match(s) else re.escape(s) for s in segments]
        self.pattern = re.compile("^/" + "/".join(parts) + "/?$")


def load_ops() -> list[Op]:
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return [
        Op(method.upper(), path, operation["operationId"])
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in METHODS
    ]


def match(ops: list[Op], method: str, path: str) -> Op | None:
    path = path.split("?", 1)[0]
    found = [o for o in ops if o.method == method.upper() and o.pattern.match(path)]
    return max(found, key=lambda o: o.literals) if found else None


def _client_path(raw: str) -> str:
    """A UI path literal with ``{param}``, ``:param`` or ``${expr}`` segments, made concrete."""
    raw = re.sub(r"\$\{[^}]*\}", "x", raw)
    raw = re.sub(r"\{[^}/]+\}", "x", raw)
    raw = re.sub(r":[A-Za-z_]\w*", "x", raw)
    raw = raw.lstrip("*")
    return raw.split("?", 1)[0]


def _template_pattern(raw: str) -> re.Pattern[str]:
    parts = re.split(r"(\$\{[^}]*\}|\{[^}/]+\})", raw.split("?", 1)[0])
    body = "".join("[^/]+" if part.startswith(("${", "{")) else re.escape(part) for part in parts)
    return re.compile("^" + body + "$")


def _ui_files(tests: bool) -> list[Path]:
    files = [p for p in UI_SRC.rglob("*") if p.suffix in (".ts", ".tsx") and p.is_file()]
    if tests:
        return [p for p in files if ".test." in p.name or "/src/test/" in str(p)]
    return [
        p
        for p in files
        if ".test." not in p.name and "/src/test/" not in str(p) and not p.name.endswith(".gen.ts")
    ]


def collect(ops: list[Op]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {
        o.id: {
            "method": o.method,
            "path": o.path,
            "backend_ok": 0,
            "backend_refused": 0,
            "backend_tests": set(),
            "authz_matrix": False,
            "public": False,
            "e2e": False,
            "ui_client": False,
            "ui_unit": False,
            "ui_e2e": set(),
        }
        for o in ops
    }
    if TRACE.exists():
        for line in TRACE.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            op = match(ops, record["method"], record["path"])
            if op is None:
                continue
            row = rows[op.id]
            status = int(record["status"])
            if status < 400:
                row["backend_ok"] += 1
                row["backend_tests"].add(record["test"].split("::", 1)[0])
            elif status in (401, 403, 404):
                row["backend_refused"] += 1
    if MATRIX.exists():
        for op_id, matrix_row in json.loads(MATRIX.read_text(encoding="utf-8")).items():
            if op_id in rows:
                rows[op_id]["authz_matrix"] = True
                rows[op_id]["public"] = matrix_row.get("target") == "public"
    if E2E_REPORT.exists():
        report = json.loads(E2E_REPORT.read_text(encoding="utf-8"))
        exempt = json.loads(E2E_EXEMPTIONS.read_text(encoding="utf-8"))
        for op_id in list(report.get("covered", [])) + list(exempt):
            if op_id in rows:
                rows[op_id]["e2e"] = True
    for path in _ui_files(tests=False):
        text = path.read_text(encoding="utf-8")
        for method, raw in _CLIENT_CALL.findall(text):
            op = match(ops, method, _client_path(raw))
            if op is not None:
                rows[op.id]["ui_client"] = True
        # A path built in a template literal (`.../{user_id}/${action}`): every operation whose
        # path fits it, whatever the method (the literal is passed to the client elsewhere).
        for raw in _TEMPLATE.findall(text):
            pattern = _template_pattern(raw)
            for op in ops:
                if pattern.match(re.sub(r"\{[^}/]+\}", "x", op.path)):
                    rows[op.id]["ui_client"] = True
    for path in _ui_files(tests=True):
        for method, raw in _MSW.findall(path.read_text(encoding="utf-8")):
            op = match(ops, method, _client_path(raw))
            if op is not None:
                rows[op.id]["ui_unit"] = True
    if UI_RESULTS.exists():
        for calls in UI_RESULTS.rglob("api-calls/*.jsonl"):
            for line in calls.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                op = match(ops, record["method"], record["path"])
                if op is not None and int(record["status"]) < 400:
                    rows[op.id]["ui_e2e"].add(record.get("spec", "?"))
    return rows


def load_exemptions() -> dict[str, dict[str, str]]:
    if not EXEMPTIONS.exists():
        return {}
    data: dict[str, dict[str, str]] = json.loads(EXEMPTIONS.read_text(encoding="utf-8"))
    return data


def gaps(rows: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    exemptions = load_exemptions()
    failing: list[str] = []
    stale: list[str] = []
    for op_id, row in sorted(rows.items()):
        exempt = exemptions.get(op_id, {})
        for column in GATED:
            if column == "backend_refused" and row.get("public"):
                continue  # a public() operation has no authorization to refuse
            if not row[column]:
                if column not in exempt:
                    failing.append(f"{op_id} ({row['method']} {row['path']}): no {column}")
            elif column in exempt:
                stale.append(f"{op_id}: {column} exemption is stale")
    for op_id in exemptions:
        if op_id not in rows:
            stale.append(f"{op_id}: exempt but not an operation")
    return failing, stale


def summary(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    by_service: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    columns = (
        "backend_ok",
        "backend_refused",
        "authz_matrix",
        "e2e",
        "ui_client",
        "ui_unit",
        "ui_e2e",
    )
    for row in rows.values():
        service = row["path"].split("/")[3]
        by_service[service]["operations"] += 1
        for column in columns:
            if row[column]:
                by_service[service][column] += 1
    totals = {c: sum(1 for r in rows.values() if r[c]) for c in columns}
    return {
        "operations": total,
        "totals": totals,
        "by_service": {k: dict(v) for k, v in sorted(by_service.items())},
    }


def markdown(rows: dict[str, dict[str, Any]]) -> str:
    def mark(value: Any) -> str:
        return "yes" if value else "**no**"

    lines = [
        "| Operation | Backend 2xx | Backend 401/403/404 | Authz matrix | Gateway e2e "
        "| UI client | UI unit (msw) | UI e2e (Playwright) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for _op_id, row in sorted(rows.items(), key=lambda kv: (kv[1]["path"], kv[1]["method"])):
        ui_e2e = ", ".join(sorted(row["ui_e2e"])) if row["ui_e2e"] else "-"
        ui_client = "yes" if row["ui_client"] else "-"
        ui_unit = "yes" if row["ui_unit"] else ("**no**" if row["ui_client"] else "-")
        lines.append(
            f"| `{row['method']} {row['path']}` | {mark(row['backend_ok'])} "
            f"| {mark(row['backend_refused'])} | {mark(row['authz_matrix'])} | {mark(row['e2e'])} "
            f"| {ui_client} | {ui_unit} | {ui_e2e} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)

    ops = load_ops()
    rows = collect(ops)
    report = summary(rows)
    failing, stale = gaps(rows)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {
        k: {c: (sorted(v) if isinstance(v, set) else v) for c, v in row.items()}
        for k, row in rows.items()
    }
    REPORT.write_text(
        json.dumps(
            {**report, "failing": failing, "stale": stale, "operations_detail": serialisable},
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=1))
    if not TRACE.exists():
        print(f"warning: no backend trace at {TRACE} (set DC_OP_TRACE)", file=sys.stderr)
    for line in stale:
        print(f"stale: {line}")
    if args.markdown:
        args.markdown.write_text(markdown(rows), encoding="utf-8")
    if failing:
        print(f"{len(failing)} operation coverage gaps:", file=sys.stderr)
        for line in failing:
            print(f"  {line}", file=sys.stderr)
        return 1 if args.gate else 0
    print("operation coverage: every operation has backend 2xx, backend refusal and matrix tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
