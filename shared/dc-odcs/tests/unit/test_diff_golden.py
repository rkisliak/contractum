"""VAL-02: the golden corpus pins the classifier's semantics (D-149, ROADMAP SC1).

Every ``tests/golden/compat/*.yaml`` file is a list of cases, each ``name``, ``before``,
``after`` and ``expected`` (a list of ``{path, code, class}``). The corpus documents are minimal
valid ODCS documents, so the same files feed the opt-in datacontract-cli oracle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from dc_odcs.diff import compare, table
from dc_odcs.loader import load_yaml

pytestmark = pytest.mark.unit

CORPUS = Path(__file__).resolve().parent.parent / "golden" / "compat"
MIN_CASES = 50
# Codes the table produces outside its RULES values (see ``table.classify_element``).
EXTRA_CODES = frozenset({"required-property-added"})


def load_cases() -> list[tuple[str, dict[str, Any]]]:
    cases: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(CORPUS.glob("*.yaml")):
        loaded = load_yaml(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, list), f"{path.name} must hold a list of cases"
        cases.extend((f"{path.stem}::{case['name']}", case) for case in loaded)
    return cases


CASES = load_cases()


def triples(items: Any) -> list[tuple[str, str, str]]:
    return sorted((item["path"], item["code"], item["class"]) for item in items)


@pytest.mark.parametrize("case", [case for _, case in CASES], ids=[cid for cid, _ in CASES])
def test_corpus_case(case: dict[str, Any]) -> None:
    result = compare(case["before"], case["after"])
    got = sorted((c.path, c.code, c.cls) for c in result.changes)
    assert got == triples(case["expected"]), case["name"]
    assert result.changes_total == len(case["expected"])


def test_corpus_has_at_least_fifty_uniquely_named_cases() -> None:
    names = [cid for cid, _ in CASES]
    assert len(names) >= MIN_CASES
    assert len(set(names)) == len(names)


def test_corpus_covers_both_api_versions() -> None:
    versions = {case[side]["apiVersion"] for _, case in CASES for side in ("before", "after")}
    assert {"v3.1.0", "v3.2.0"} <= versions


def test_every_table_code_appears_in_an_expected_list() -> None:
    wanted = {rule.code for rule in table.RULES.values()} | EXTRA_CODES
    seen = {item["code"] for _, case in CASES for item in case["expected"]}
    assert wanted - seen == set(), "no golden case exercises these codes"


def test_exact_number_cases_are_in_the_corpus() -> None:
    by_name = {cid.split("::", 1)[1]: case for cid, case in CASES}
    assert by_name["maximum 10 to 10.0 is no change"]["expected"] == []
    fifty = by_name["maxLength 50 to 49 tightened by exact number"]
    assert [item["code"] for item in fifty["expected"]] == ["constraint-tightened"]
