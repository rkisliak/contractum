"""Run datacontract-cli's ``breaking`` over the golden corpus and print JSON (dev-only oracle).

Standalone script. It imports ``datacontract`` (datacontract-cli), which is NOT a workspace
dependency and never enters ``uv.lock`` (D-143, D-149): it is only ever run as

    uv run --no-project --with datacontract-cli==1.2.9 python -I \
        shared/dc-odcs/tests/oracle/run_oracle.py shared/dc-odcs/tests/golden/compat

Output: ``{"<file stem>::<case name>": {"consumer": [...], "producer": [...]}}`` where each entry
is ``{path, level, direction, rule_id}``. Paths are normalised to the platform's name-keyed style:
only the subtree root of an added or removed element is kept (the oracle also reports every leaf
below it) and an attribute entry is reported at its element, not at the attribute (Pitfall 9).
A case the oracle cannot process is reported as ``{"error": "..."}``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml
from datacontract.data_contract import DataContract
from datacontract.model.breaking import Perspective

ELEMENT_RULE_SUFFIX = "added-or-removed"


def normalise(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roots = [e["path"] for e in entries if str(e["rule_id"]).endswith(ELEMENT_RULE_SUFFIX)]
    kept: list[dict[str, Any]] = []
    for entry in entries:
        path = entry["path"]
        if any(path.startswith(root + ".") for root in roots):
            continue  # a leaf below an added or removed element
        if not str(entry["rule_id"]).endswith(ELEMENT_RULE_SUFFIX):
            path = path.rsplit(".", 1)[0] if "." in path else ""
            if path.endswith(".logicalTypeOptions"):
                path = path[: -len(".logicalTypeOptions")]
        kept.append({**entry, "path": path})
    return kept


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as folder:
        before, after = Path(folder, "before.yaml"), Path(folder, "after.yaml")
        before.write_text(yaml.safe_dump(case["before"]), encoding="utf-8")
        after.write_text(yaml.safe_dump(case["after"]), encoding="utf-8")
        out: dict[str, Any] = {}
        for perspective in (Perspective.CONSUMER, Perspective.PRODUCER):
            result = DataContract(data_contract_file=str(before), inline_references=False).breaking(
                DataContract(data_contract_file=str(after), inline_references=False),
                perspective=perspective,
            )
            out[perspective.value] = normalise(
                [
                    {
                        "path": entry.path,
                        "level": entry.level.value,
                        "direction": entry.direction.value if entry.direction else None,
                        "rule_id": entry.rule_id,
                    }
                    for entry in result.entries
                ]
            )
        return out


def main(corpus: Path) -> None:
    results: dict[str, Any] = {}
    for path in sorted(corpus.glob("*.yaml")):
        for case in yaml.safe_load(path.read_text(encoding="utf-8")):
            try:
                results[f"{path.stem}::{case['name']}"] = run_case(case)
            except Exception as error:  # noqa: BLE001 - one bad case must not hide the others
                results[f"{path.stem}::{case['name']}"] = {
                    "error": f"{type(error).__name__}: {error}"
                }
    json.dump(results, sys.stdout, indent=1, sort_keys=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
