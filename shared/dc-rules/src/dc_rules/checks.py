"""The rule-set check: static limits, the units a worker runs, and the report (D-112, D-116, D-137).

Everything here is pure: no engine, no I/O. The web process imports it to build the units it
sends to the sandbox (`check_units`) and to turn the answers into a report (`assemble_check`);
the worker's check op (`dc_rules.worker`) answers the units.

Per rule the units are, in this order: one ``compile`` unit, one ``fixture`` unit per pass
fixture and per fail fixture (by index), one ``probe`` unit. A rule that already has a static
size problem (expression or target too large) gets no unit at all: no engine work is done on an
oversized expression. A kind with too many fixtures, and a fixture that is too large, get no
fixture units either. Unit ids are the position in the list `check_units` returns, so
`assemble_check` rebuilds the same list to map results back.

Problems of one rule are always listed in the order compile, size, fixtures (pass fixtures by
index, then fail fixtures by index), probe, so repeated checks of the same content give identical
reports.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from dc_rules.model import (
    Fixture,
    FixtureCtx,
    RuleDef,
    RuleFixtures,
    RuleSetContent,
    canonical_json,
)
from dc_rules.ports import Limits
from dc_rules.protocol import CheckUnit, UnitResult

# Codes about a rule's fixtures. Every other problem code is a rule-validity code; rule-set
# publish uses the split to choose between ruleset-fixtures-failed and rule-invalid (Plan 06).
FIXTURE_CODES: frozenset[str] = frozenset(
    {
        "fixture_changes_unavailable",
        "missing_pass_fixture",
        "missing_fail_fixture",
        "too_many_fixtures",
        "fixture_too_large",
        "fixture_misbehaved",
    }
)

# Evaluation errors that mean "this rule does not scale", the only ones that refuse a rule at the
# scale probe. A missing key on the synthetic document is the author's guard problem, not a scale
# problem. The last two are sandbox verdicts: the probe never finished, so it cannot vouch.
SCALE_ERROR_CODES: frozenset[str] = frozenset(
    {
        "budget_exceeded",
        "timeout",
        "memory_exceeded",
        "too_many_matches",
        "run_budget_exceeded",
        "engine_error",
    }
)

PROBE_TABLES = 100
PROBE_COLUMNS = 100
PROBE_CHANGES = 200
PROBE_REFS = 100

# Bound as `refs` when the caller has no reference facts (cel-2).
EMPTY_REFS: dict[str, Any] = {
    "elements": {},
    "servers": {},
    "contracts": {},
    "principals": {},
    "teams": {},
}

_DETAIL_CHARS = 300
_NO_FIXTURES = RuleFixtures(pass_=[], fail=[])
_PLACEHOLDER = Fixture(document=None)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CheckProblem(_Frozen):
    rule_key: str | None = None
    code: str
    detail: str | None = None
    fixture_kind: Literal["pass", "fail"] | None = None
    fixture_index: int | None = None
    error_code: str | None = None


class RuleCheck(_Frozen):
    rule_key: str
    ok: bool
    problems: list[CheckProblem]


class RuleSetCheckReport(_Frozen):
    ok: bool
    rule_set_problems: list[CheckProblem]
    rules: list[RuleCheck]


def scale_probe_document() -> dict[str, Any]:
    """100 schema objects of 100 string properties each (10,000 properties)."""
    return {
        "schema": [
            {
                "name": f"t{t}",
                "properties": [
                    {"name": f"p{p}", "logicalType": "string"} for p in range(PROBE_COLUMNS)
                ],
            }
            for t in range(PROBE_TABLES)
        ]
    }


def probe_bindings(document: Any) -> dict[str, Any]:
    """The cel-2 bindings of the scale probe: ``previous`` is the probe document itself, plus a
    200-entry ``changes`` list and 100-entry ``refs`` maps, so a rule that scans any of them is
    exercised at a realistic size (A12)."""
    changes = [
        {
            "code": "property-removed" if n % 2 else "property-added",
            "class": "breaking" if n % 2 else "info",
            "element_kind": "property",
            "path": f"schema.t{n % PROBE_TABLES}.properties.p{n % PROBE_COLUMNS}",
            "pointer_before": f"/schema/{n % PROBE_TABLES}/properties/{n % PROBE_COLUMNS}",
            "pointer_after": None,
            "attribute": None,
            "before": None,
            "after": None,
        }
        for n in range(PROBE_CHANGES)
    ]
    refs = {
        "elements": {f"t0.p{n}": "found" for n in range(PROBE_REFS)},
        "servers": {f"server{n}": "found" for n in range(PROBE_REFS)},
        "contracts": {
            f"contract{n}": {"state": "published", "pinned_published": True}
            for n in range(PROBE_REFS)
        },
        "principals": {f"user{n}@example.com": "found" for n in range(PROBE_REFS)},
        "teams": {f"team{n}": "found" for n in range(PROBE_REFS)},
    }
    return {"previous": document, "changes": changes, "refs": refs}


def default_fixture_ctx() -> dict[str, Any]:
    return FixtureCtx().model_dump(mode="json")


def _fixture_bytes(fixture: Fixture) -> int:
    size = len(canonical_json(fixture.document))
    if fixture.previous is not None:
        size += len(canonical_json(fixture.previous))
    if fixture.refs is not None:
        size += len(canonical_json(fixture.refs))
    return size


def _rule_size_problems(rule: RuleDef, limits: Limits) -> list[CheckProblem]:
    problems: list[CheckProblem] = []
    condition_bytes = len(rule.condition.encode("utf-8"))
    if condition_bytes > limits.max_expression_bytes:
        problems.append(
            CheckProblem(
                rule_key=rule.key,
                code="expression_too_large",
                detail=f"{condition_bytes} bytes, the limit is {limits.max_expression_bytes}",
            )
        )
    if len(rule.target) > limits.max_target_chars:
        problems.append(
            CheckProblem(
                rule_key=rule.key,
                code="target_too_long",
                detail=f"{len(rule.target)} characters, the limit is {limits.max_target_chars}",
            )
        )
    return problems


def _fixture_problems(rule: RuleDef, limits: Limits) -> list[CheckProblem]:
    """Static fixture problems, ordered: pass kind (by index), then fail kind (by index)."""
    problems: list[CheckProblem] = []
    kinds: tuple[tuple[Literal["pass", "fail"], list[Fixture]], ...] = (
        ("pass", rule.fixtures.pass_),
        ("fail", rule.fixtures.fail),
    )
    for kind, fixtures in kinds:
        if not fixtures:
            problems.append(
                CheckProblem(
                    rule_key=rule.key,
                    code=f"missing_{kind}_fixture",
                    detail=f"the rule needs at least one {kind} fixture",
                    fixture_kind=kind,
                )
            )
            continue
        if len(fixtures) > limits.max_fixtures_per_kind:
            problems.append(
                CheckProblem(
                    rule_key=rule.key,
                    code="too_many_fixtures",
                    detail=(
                        f"{len(fixtures)} {kind} fixtures, "
                        f"the limit is {limits.max_fixtures_per_kind}"
                    ),
                    fixture_kind=kind,
                )
            )
            continue
        for index, fixture in enumerate(fixtures):
            size = _fixture_bytes(fixture)
            if size > limits.max_fixture_bytes:
                problems.append(
                    CheckProblem(
                        rule_key=rule.key,
                        code="fixture_too_large",
                        detail=f"{size} bytes, the limit is {limits.max_fixture_bytes}",
                        fixture_kind=kind,
                        fixture_index=index,
                    )
                )
    return problems


def static_problems(content: RuleSetContent, limits: Limits) -> list[CheckProblem]:
    """Every problem found without the engine: set-level first, then rule by rule."""
    problems: list[CheckProblem] = []
    if not content.rules:
        problems.append(
            CheckProblem(code="empty_rule_set", detail="a rule set needs at least one rule")
        )
    if len(content.rules) > limits.max_rules_per_set:
        problems.append(
            CheckProblem(
                code="too_many_rules",
                detail=f"{len(content.rules)} rules, the limit is {limits.max_rules_per_set}",
            )
        )
    seen: set[str] = set()
    reported: set[str] = set()
    for rule in content.rules:
        if rule.key in seen and rule.key not in reported:
            problems.append(
                CheckProblem(
                    rule_key=rule.key,
                    code="duplicate_rule_key",
                    detail="two rules share this key",
                )
            )
            reported.add(rule.key)
        seen.add(rule.key)
    for rule in content.rules:
        problems.extend(_rule_size_problems(rule, limits))
        problems.extend(_fixture_problems(rule, limits))
    return problems


def _has_size_problem(rule: RuleDef, limits: Limits) -> bool:
    return bool(_rule_size_problems(rule, limits))


def _stripped(rule: RuleDef, fixtures: RuleFixtures = _NO_FIXTURES) -> RuleDef:
    return rule.model_copy(update={"fixtures": fixtures})


def _fixture_unit_rule(rule: RuleDef, kind: Literal["pass", "fail"], index: int) -> RuleDef:
    """The rule carrying only the one fixture the unit needs, at its real index.

    Other positions hold an empty placeholder, so the wire never carries up to 640 KB of
    fixtures per unit.
    """
    chosen = (rule.fixtures.pass_ if kind == "pass" else rule.fixtures.fail)[index]
    # previous and refs travel on the unit itself, not twice.
    chosen = chosen.model_copy(update={"previous": None, "refs": None})
    padded = [_PLACEHOLDER] * index + [chosen]
    if kind == "pass":
        return _stripped(rule, RuleFixtures(pass_=padded, fail=[]))
    return _stripped(rule, RuleFixtures(pass_=[], fail=padded))


ChangesFor = Callable[[Any, Any], list[Any]]


def _fixture_bindings(fixture: Fixture, changes_for: ChangesFor | None) -> tuple[list[Any], bool]:
    """The fixture's ``changes`` and whether they are unavailable.

    ``changes`` is derived from (previous, document) by the caller's classifier. A fixture with a
    baseline but no classifier (or one that raises) is unavailable, never an empty list.
    """
    if fixture.previous is None:
        return [], False
    if changes_for is None:
        return [], True
    try:
        return list(changes_for(fixture.previous, fixture.document)), False
    except Exception:
        return [], True


def _plan(
    content: RuleSetContent, limits: Limits, changes_for: ChangesFor | None = None
) -> list[tuple[int, CheckUnit]]:
    """(rule position, unit) pairs in draft order; unit ids are the list positions."""
    pending: list[tuple[int, str, Literal["pass", "fail"] | None, int | None]] = []
    for position, rule in enumerate(content.rules):
        if _has_size_problem(rule, limits):
            continue
        pending.append((position, "compile", None, None))
        kinds: tuple[tuple[Literal["pass", "fail"], list[Fixture]], ...] = (
            ("pass", rule.fixtures.pass_),
            ("fail", rule.fixtures.fail),
        )
        for kind, fixtures in kinds:
            if len(fixtures) > limits.max_fixtures_per_kind:
                continue
            for index, fixture in enumerate(fixtures):
                if _fixture_bytes(fixture) <= limits.max_fixture_bytes:
                    pending.append((position, "fixture", kind, index))
        pending.append((position, "probe", None, None))
    plan: list[tuple[int, CheckUnit]] = []
    for unit_id, (position, kind_name, fixture_kind, fixture_index) in enumerate(pending):
        rule = content.rules[position]
        bindings: dict[str, Any] = {}
        if kind_name == "fixture":
            assert fixture_kind is not None and fixture_index is not None  # noqa: S101
            unit_rule = _fixture_unit_rule(rule, fixture_kind, fixture_index)
            if content.dsl_version == "cel-2":
                source = (rule.fixtures.pass_ if fixture_kind == "pass" else rule.fixtures.fail)[
                    fixture_index
                ]
                changes, unavailable = _fixture_bindings(source, changes_for)
                bindings = {
                    "previous": source.previous,
                    "changes": changes,
                    "refs": source.refs if source.refs is not None else EMPTY_REFS,
                    "changes_unavailable": unavailable,
                }
        else:
            unit_rule = _stripped(rule)
        unit_kind: Literal["compile", "fixture", "probe"] = (
            "fixture" if kind_name == "fixture" else "probe" if kind_name == "probe" else "compile"
        )
        plan.append(
            (
                position,
                CheckUnit(
                    id=unit_id,
                    rule=unit_rule,
                    kind=unit_kind,
                    fixture_kind=fixture_kind,
                    fixture_index=fixture_index,
                    dsl_version=content.dsl_version,
                    **bindings,
                ),
            )
        )
    return plan


def check_units(
    content: RuleSetContent,
    limits: Limits | None = None,
    *,
    changes_for: ChangesFor | None = None,
) -> list[CheckUnit]:
    """The units to run, in draft order; ids are the list positions.

    ``changes_for(previous, document)`` derives a cel-2 fixture's classified ``changes`` list. It
    is injected because dc_rules imports no other platform package: the caller (validation) passes
    the dc_odcs diff. Without it a fixture that has ``previous`` yields the problem
    ``fixture_changes_unavailable`` rather than silently binding an empty list.
    """
    plan = _plan(content, limits if limits is not None else Limits(), changes_for)
    return [unit for _, unit in plan]


def _error_code_of(result: UnitResult) -> str | None:
    if result.error_code is not None:
        return result.error_code
    for finding in result.findings:
        if finding.outcome == "error" and finding.error_code is not None:
            return finding.error_code
    return None


def _order(problem: CheckProblem) -> tuple[int, int, int]:
    """Sort key inside one rule: compile (0), size (1), fixtures (2), probe (3)."""
    if problem.code in ("target_invalid", "compile_error", "not_boolean"):
        return (0, 0, 0)
    if problem.code in ("expression_too_large", "target_too_long", "ast_too_large"):
        return (1, 0, 0)
    if problem.code == "scale_probe_failed":
        return (3, 0, 0)
    kind = 0 if problem.fixture_kind == "pass" else 1
    index = problem.fixture_index if problem.fixture_index is not None else -1
    return (2, kind, index)


_SET_CODES = frozenset({"empty_rule_set", "too_many_rules", "duplicate_rule_key"})


def assemble_check(
    content: RuleSetContent,
    static: list[CheckProblem],
    results: Mapping[int, UnitResult],
    limits: Limits | None = None,
) -> RuleSetCheckReport:
    """Turn the static problems and the unit answers into the report.

    ``static`` is what `static_problems` returned for the same content and limits; the set-level
    problems are taken from it, the rule-level ones are re-derived per rule position (identical
    by construction, and unambiguous when two rules share a key).
    """
    limits = limits if limits is not None else Limits()
    set_problems = [p for p in static if p.code in _SET_CODES]
    by_rule: list[list[CheckProblem]] = [
        _rule_size_problems(rule, limits) + _fixture_problems(rule, limits)
        for rule in content.rules
    ]
    compile_failed: set[int] = set()
    for position, unit in _plan(content, limits):
        rule = content.rules[position]
        result = results.get(unit.id)
        if result is None:
            # No answer at all: nothing vouches for this unit, so the rule is not ok.
            problem: CheckProblem | None = _unanswered(rule, unit)
        else:
            problem = _unit_problem(rule, unit, result, compile_failed, position)
        if problem is not None:
            by_rule[position].append(problem)

    checks: list[RuleCheck] = []
    for rule, rule_problems in zip(content.rules, by_rule, strict=True):
        problems = sorted(rule_problems, key=_order)
        checks.append(RuleCheck(rule_key=rule.key, ok=not problems, problems=problems))
    return RuleSetCheckReport(
        ok=not set_problems and all(c.ok for c in checks),
        rule_set_problems=set_problems,
        rules=checks,
    )


def _unanswered(rule: RuleDef, unit: CheckUnit) -> CheckProblem:
    if unit.kind == "fixture":
        return CheckProblem(
            rule_key=rule.key,
            code="fixture_misbehaved",
            detail="the fixture was not evaluated",
            fixture_kind=unit.fixture_kind,
            fixture_index=unit.fixture_index,
            error_code="engine_error",
        )
    if unit.kind == "probe":
        return CheckProblem(
            rule_key=rule.key,
            code="scale_probe_failed",
            detail="the scale probe did not finish",
            error_code="engine_error",
        )
    return CheckProblem(
        rule_key=rule.key,
        code="compile_error",
        detail="the check did not finish",
        error_code="engine_error",
    )


def _unit_problem(
    rule: RuleDef,
    unit: CheckUnit,
    result: UnitResult,
    compile_failed: set[int],
    rule_position: int,
) -> CheckProblem | None:
    if unit.kind == "compile":
        if result.outcome == "invalid":
            compile_failed.add(rule_position)
            return CheckProblem(
                rule_key=rule.key,
                code=result.problem_code or "compile_error",
                detail=(result.detail or "")[:_DETAIL_CHARS] or None,
            )
        if result.outcome == "error":
            compile_failed.add(rule_position)
            return CheckProblem(
                rule_key=rule.key,
                code="compile_error",
                detail="the check did not finish",
                error_code=_error_code_of(result),
            )
        return None
    unavailable = result.problem_code == "fixture_changes_unavailable"
    if rule_position in compile_failed or (result.outcome == "invalid" and not unavailable):
        # The compile problem already says everything; a fixture of a rule that does not
        # compile has no behaviour to judge.
        return None
    if unit.kind == "fixture":
        if result.problem_code == "fixture_changes_unavailable":
            return CheckProblem(
                rule_key=rule.key,
                code="fixture_changes_unavailable",
                detail="the fixture has a previous document but its changes could not be derived",
                fixture_kind=unit.fixture_kind,
                fixture_index=unit.fixture_index,
            )
        expected = "pass" if unit.fixture_kind == "pass" else "fail"
        if result.outcome == expected:
            return None
        return CheckProblem(
            rule_key=rule.key,
            code="fixture_misbehaved",
            detail=f"the {unit.fixture_kind} fixture evaluated to {result.outcome}",
            fixture_kind=unit.fixture_kind,
            fixture_index=unit.fixture_index,
            error_code=_error_code_of(result),
        )
    code = _error_code_of(result) if result.outcome == "error" else None
    if code is not None and code in SCALE_ERROR_CODES:
        return CheckProblem(
            rule_key=rule.key,
            code="scale_probe_failed",
            detail=f"the rule errored with {code} on {PROBE_TABLES} tables of "
            f"{PROBE_COLUMNS} properties",
            error_code=code,
        )
    return None
