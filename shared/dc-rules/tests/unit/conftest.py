"""Fixtures for the dc-rules unit suites.

`engine` is parameterised so a second adapter (for example the pure-Python `cel-python` fallback)
is added by appending one param here; it must then pass the whole conformance suite unchanged.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from typing import Any

import pytest
from dc_rules.cel_engine import CelEngine
from dc_rules.checks import (
    RuleSetCheckReport,
    assemble_check,
    check_units,
    static_problems,
)
from dc_rules.model import RuleSetContent
from dc_rules.ports import ExpressionEngine, Limits, Targeter
from dc_rules.protocol import CheckJob, DoneMarker, LimitsWire, UnitResult
from dc_rules.targets import JsonPathTargeter
from dc_rules.worker import serve

ENGINE_FACTORIES: dict[str, Callable[[], ExpressionEngine]] = {
    "cel-expr-python": CelEngine,
}


@pytest.fixture(scope="session", params=list(ENGINE_FACTORIES), ids=list(ENGINE_FACTORIES))
def engine(request: pytest.FixtureRequest) -> ExpressionEngine:
    return ENGINE_FACTORIES[request.param]()


@pytest.fixture(scope="session")
def targeter() -> Targeter:
    return JsonPathTargeter()


# -- rule-set check helpers (plan 05-04) ---------------------------------------------------------

UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"


def table_doc(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def rule_dict(
    condition: str = UNIQUE_NAMES,
    *,
    key: str = "unique-names",
    target: str = "$.schema[*]",
    passing: list[Any] | None = None,
    failing: list[Any] | None = None,
    match: str = "each",
    on_empty: str = "pass",
) -> dict[str, Any]:
    """A rule as plain data; ``None`` fixtures default to one passing and one failing table."""
    pass_docs = [table_doc("id", "name")] if passing is None else passing
    fail_docs = [table_doc("id", "id")] if failing is None else failing
    return {
        "key": key,
        "target": target,
        "condition": condition,
        "severity": "error",
        "message": "Rule under test.",
        "match": match,
        "on_empty": on_empty,
        "fixtures": {
            "pass": [{"document": d} for d in pass_docs],
            "fail": [{"document": d} for d in fail_docs],
        },
    }


def content_of(*rules: dict[str, Any]) -> RuleSetContent:
    return RuleSetContent.model_validate({"rules": list(rules)})


def run_check(
    content: RuleSetContent,
    *,
    engine: ExpressionEngine,
    targeter: Targeter,
    limits: Limits | None = None,
    changes_for: Callable[[Any, Any], list[Any]] | None = None,
) -> RuleSetCheckReport:
    """The full check, with the worker loop driven in-process (no kill, so nothing may hang)."""
    limits = limits if limits is not None else Limits()
    static = static_problems(content, limits)
    units = check_units(content, limits, changes_for=changes_for)
    results: dict[int, UnitResult] = {}
    if units:
        job = CheckJob(units=units, limits=LimitsWire.from_limits(limits))
        out = io.StringIO()
        serve([job.model_dump_json() + "\n"], out, engine=engine, targeter=targeter)
        for line in out.getvalue().splitlines():
            message = json.loads(line)
            if "done" in message:
                done = DoneMarker.model_validate(message)
                results[done.done] = done.result
    return assemble_check(content, static, results, limits)


class Kit:
    """Helpers for the rule-set check tests (test modules cannot import conftest)."""

    UNIQUE = UNIQUE_NAMES
    rule = staticmethod(rule_dict)
    content = staticmethod(content_of)
    table = staticmethod(table_doc)

    def __init__(self, engine: ExpressionEngine, targeter: Targeter) -> None:
        self._engine = engine
        self._targeter = targeter
        self.engine = engine
        self.targeter = targeter

    def check(
        self,
        content: RuleSetContent,
        limits: Limits | None = None,
        changes_for: Callable[[Any, Any], list[Any]] | None = None,
    ) -> RuleSetCheckReport:
        return run_check(
            content,
            engine=self._engine,
            targeter=self._targeter,
            limits=limits,
            changes_for=changes_for,
        )

    @staticmethod
    def codes(report: RuleSetCheckReport, index: int = 0) -> list[str]:
        return [p.code for p in report.rules[index].problems]


@pytest.fixture
def kit(engine: ExpressionEngine, targeter: Targeter) -> Kit:
    return Kit(engine, targeter)
