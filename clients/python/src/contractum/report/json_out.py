"""The JSON of a report and the committed JSON Schema that pins it for CI consumers.

``python -m contractum.report.json_out --write-schema`` regenerates ``report.schema.json`` next
to this module; ``--check-schema`` exits 1 when the committed file differs from the model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from contractum._atomic import write_atomic
from contractum.report.model import Report

SCHEMA_PATH = Path(__file__).with_name("report.schema.json")
STDOUT = "-"
REGENERATE = "python -m contractum.report.json_out --write-schema"


def report_to_json(report: Report) -> str:
    """The report as indented JSON with a trailing newline."""
    return json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def write_json(report: Report, target: str | os.PathLike[str]) -> None:
    """Write the JSON to ``target`` atomically; ``-`` writes it to stdout."""
    text = report_to_json(report)
    if os.fspath(target) == STDOUT:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    write_atomic(target, text.encode("utf-8"))


def report_json_schema() -> dict[str, Any]:
    """The JSON Schema of the report model."""
    return Report.model_json_schema()


def schema_text(schema: dict[str, Any] | None = None) -> str:
    """The schema as committed: sorted keys, 2-space indent, trailing newline."""
    body = report_json_schema() if schema is None else schema
    return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _read_committed() -> str | None:
    try:
        return SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    """``--write-schema`` writes the committed schema, ``--check-schema`` verifies it."""
    parser = argparse.ArgumentParser(prog="python -m contractum.report.json_out")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write-schema", action="store_true", help="regenerate the schema file")
    group.add_argument("--check-schema", action="store_true", help="fail when it is out of date")
    args = parser.parse_args(argv)
    expected = schema_text()
    if args.write_schema:
        write_atomic(SCHEMA_PATH, expected.encode("utf-8"))
        return 0
    if _read_committed() == expected:
        return 0
    print(f"report.schema.json is out of date; regenerate it with: {REGENERATE}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
