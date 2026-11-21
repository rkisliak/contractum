"""Every document an e2e journey publishes conforms to the schema and the platform rules.

The compose stack's publish gate refuses a document the vendored ODCS schema or an
``odcs-structural`` rule rejects (VAL-07, D-129, D-130). The journeys ran before that gate existed,
so this fast repo test is what stops the cold e2e gate from being the first to find a fixture the
real gate rejects. It loads each journey's ``_contract_yaml`` and runs the same schema check and the
same seeded rule set in process, without the compose stack.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from dc_odcs import parse_contract, validate_structure
from dc_rules.cel_engine import CelEngine
from dc_rules.evaluator import evaluate_rule
from dc_rules.normalize import normalize_document
from dc_rules.ports import Limits
from dc_rules.targets import JsonPathTargeter
from dc_validation.seed import ODCS_STRUCTURAL_V1

pytestmark = pytest.mark.unit

E2E = Path(__file__).resolve().parents[1] / "e2e"
CTX = {"folder_kind": "team", "folder_path": "e2e", "organization_id": None}

JOURNEYS: dict[str, Callable[[Any], bytes]] = {
    "test_phase2_delegation_journey.py": lambda m: m._contract_yaml(
        "urn:e2e:conformance", "Conformance"
    ),
    "test_phase3_lifecycle_journey.py": lambda m: m._contract_yaml(
        "urn:e2e:conformance", "Conformance"
    ),
    "test_phase4_entra_machine_journey.py": lambda m: m._contract_yaml(
        "urn:e2e:conformance", "Conformance"
    ),
    "test_phase41_subscriptions_journey.py": lambda m: m._contract_yaml(
        "urn:e2e:conformance", "Conformance", "1.0.0"
    ),
    "test_phase7_engineer_journey.py": lambda m: m._contract_yaml(
        "urn:e2e:conformance", "Conformance", "1.1.0", extra_property=True
    ),
}


def _load(journey: str) -> ModuleType:
    """Import a journey file by path; its dataclasses need the module in ``sys.modules`` first."""
    name = f"_e2e_conformance_{journey.removesuffix('.py')}"
    spec = importlib.util.spec_from_file_location(name, E2E / journey)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.mark.parametrize("journey", sorted(JOURNEYS))
def test_a_journeys_published_document_passes_the_schema_and_every_platform_rule(
    journey: str,
) -> None:
    module = _load(journey)
    try:
        raw = JOURNEYS[journey](module)
        parsed = parse_contract(raw, "application/yaml")
        report = validate_structure(parsed.document, parsed.api_version)
        assert report.valid, f"{journey}: schema errors {report.errors[:5]}"

        engine, targeter = CelEngine(), JsonPathTargeter()
        document = normalize_document(parsed.document)
        problems: list[str] = []
        for rule in ODCS_STRUCTURAL_V1.rules:
            result = evaluate_rule(
                rule,
                engine.compile(rule.condition),
                targeter.compile(rule.target),
                document,
                CTX,
                engine=engine,
                targeter=targeter,
                limits=Limits(),
            )
            if result.outcome != "pass":
                where = [f.location.pointer for f in result.findings]
                problems.append(f"{rule.key}: {result.outcome} at {where}")
        assert not problems, f"{journey}: {problems}"
    finally:
        sys.modules.pop(module.__name__, None)
