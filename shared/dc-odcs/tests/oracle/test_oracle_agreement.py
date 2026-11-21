"""Opt-in agreement check between the classifier and datacontract-cli (dev-only oracle, D-149).

Run it by hand: ``uv run pytest -m oracle shared/dc-odcs`` (needs ``uv`` and network access).
It carries only the ``oracle`` marker, so ``pytest -m unit``, ``-m integration`` and
``scripts/ci.sh`` never select it, and the default ``addopts`` deselect it. A full run takes
several minutes; set ``DC_ORACLE_JSON`` to the saved output of ``run_oracle.py`` to reuse it.
datacontract-cli is fetched into a throwaway environment (``uv run --no-project --with``) pinned
to one version; it is never a workspace dependency and never appears in ``uv.lock`` (D-143).

Three agreement rules (RESEARCH "Oracle comparison harness"), evaluated per element path:

1. an oracle ``both`` direction (in either perspective) means the platform says ``breaking``;
2. an oracle ERROR in either perspective means the platform class is not ``info``;
3. a platform ``breaking`` change means some oracle ERROR in some perspective.

Every disagreement must be an ``allowlist.yaml`` row keyed ``(path, code)`` with a one-line
reason; ``path: "*"`` covers the same code at any path. Unused rows fail the test, so the list
cannot rot.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from dc_odcs.diff import compare
from dc_odcs.loader import load_yaml

pytestmark = pytest.mark.oracle

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parent / "golden" / "compat"
ORACLE_VERSION = "1.2.9"
TIMEOUT_SECONDS = 1800
CLASS_RANK = {"info": 0, "warning": 1, "breaking": 2}
# The oracle keys quality rules and relationships its own way (by name or by target); the platform
# keys them by name, content or ``from>to``. Both sides are compared at the owning collection.
_COLLECTION = re.compile(r"^(.*\.(?:quality|relationships))(?:\.|\[).*$")


def collection_path(path: str) -> str:
    match = _COLLECTION.match(path)
    return match.group(1) if match else path


def run_oracle() -> dict[str, Any]:
    saved = os.environ.get("DC_ORACLE_JSON")  # a saved run_oracle.py output skips the slow run
    if saved:
        return dict(json.loads(Path(saved).read_text(encoding="utf-8")))
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    command = [
        "uv",
        "run",
        "--no-project",
        "--with",
        f"datacontract-cli=={ORACLE_VERSION}",
        "python",
        "-I",
        str(HERE / "run_oracle.py"),
        str(CORPUS),
    ]
    done = subprocess.run(  # noqa: S603 - fixed argv, a developer-run opt-in tool
        command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
    )
    assert done.returncode == 0, done.stderr[-2000:]
    loaded = json.loads(done.stdout)
    assert isinstance(loaded, dict)
    return loaded


def corpus() -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for path in sorted(CORPUS.glob("*.yaml")):
        for case in load_yaml(path.read_text(encoding="utf-8")):
            cases[f"{path.stem}::{case['name']}"] = case
    return cases


def allowlist() -> list[dict[str, str]]:
    rows = load_yaml((HERE / "allowlist.yaml").read_text(encoding="utf-8"))
    assert isinstance(rows, list)
    for row in rows:
        assert set(row) == {"path", "code", "reason"} and row["reason"].strip(), row
    return rows


def test_every_allowlist_row_has_a_reason() -> None:
    assert len(allowlist()) >= 5


def test_classifier_agrees_with_datacontract_cli() -> None:
    oracle = run_oracle()
    cases = corpus()
    rows = allowlist()
    used: set[int] = set()
    problems: list[str] = []

    def allowed(path: str, code: str) -> bool:
        for index, row in enumerate(rows):
            if row["code"] == code and row["path"] in (path, "*"):
                used.add(index)
                return True
        return False

    for case_id, case in cases.items():
        verdict = oracle.get(case_id)
        if verdict is None or "error" in verdict:
            problems.append(f"{case_id}: oracle could not run: {verdict}")
            continue
        result = compare(case["before"], case["after"])
        platform: dict[str, list[Any]] = {}
        for change in result.changes:
            platform.setdefault(collection_path(change.path), []).append(change)
        entries = [
            {**entry, "path": collection_path(entry["path"])}
            for entry in verdict["consumer"] + verdict["producer"]
        ]

        def top(path: str, platform: dict[str, list[Any]] = platform) -> tuple[str, str]:
            changes = platform.get(path, [])
            if not changes:
                return "none", "none"
            best = max(changes, key=lambda item: CLASS_RANK[item.cls])
            return best.cls, best.code

        for path in sorted({entry["path"] for entry in entries}):
            here = [entry for entry in entries if entry["path"] == path]
            cls, code = top(path)
            if any(e["direction"] == "both" for e in here) and cls != "breaking":
                if not allowed(path, code):
                    problems.append(
                        f"{case_id}: rule 1: oracle both at {path!r}, platform {cls}/{code}"
                    )
            if any(e["level"] == "error" for e in here) and cls in ("info", "none"):
                if not allowed(path, code):
                    problems.append(
                        f"{case_id}: rule 2: oracle error at {path!r}, platform {cls}/{code}"
                    )
        for change in result.changes:
            if change.cls != "breaking":
                continue
            where = collection_path(change.path)
            errors = [e for e in entries if e["path"] == where and e["level"] == "error"]
            if not errors and not allowed(change.path, change.code):
                problems.append(
                    f"{case_id}: rule 3: platform breaking {change.code} at {change.path!r}, "
                    "no oracle error"
                )

    assert not problems, "\n".join(problems)
    unused = [rows[i] for i in range(len(rows)) if i not in used]
    assert not unused, f"unused allowlist rows: {unused}"
