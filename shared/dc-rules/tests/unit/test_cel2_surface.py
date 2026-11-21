"""The cel-2 surface (D-152): bindings, coexistence with cel-1, the match cap and the worker.

`previous`, `changes` and `refs` exist only in a cel-2 environment, so a cel-1 rule set (every
already-published, immutable version) evaluates exactly as before.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from dc_rules.evaluator import references_document_binding
from dc_rules.model import RuleDef
from dc_rules.ports import ExpressionEngine, Limits, RuleCompileError, Targeter
from dc_rules.protocol import DoneMarker, EvaluateJob, EvalUnit, LimitsWire, UnitResult
from dc_rules.worker import serve

pytestmark = pytest.mark.unit

NO_BREAKING = "changes.all(c, c['class'] != 'breaking')"
CHANGES_BREAKING = [{"class": "breaking", "code": "property-removed", "path": "t.a"}]
CHANGES_INFO = [{"class": "info", "code": "property-added", "path": "t.b"}]


def make_rule(condition: str, target: str = "$") -> RuleDef:
    return RuleDef.model_validate(
        {
            "key": "r",
            "target": target,
            "condition": condition,
            "severity": "error",
            "message": "Rule under test.",
            "fixtures": {"pass": [{"document": {}}], "fail": [{"document": {}}]},
        }
    )


def run(
    engine: ExpressionEngine,
    targeter: Targeter,
    units: list[EvalUnit],
    document: Any = None,
    **job: Any,
) -> dict[int, UnitResult]:
    payload = EvaluateJob(units=units, document=document if document is not None else {}, **job)
    out = io.StringIO()
    serve([payload.model_dump_json() + "\n"], out, engine=engine, targeter=targeter)
    results: dict[int, UnitResult] = {}
    for line in out.getvalue().splitlines():
        message = json.loads(line)
        if "done" in message:
            done = DoneMarker.model_validate(message)
            results[done.done] = done.result
    return results


def unit(condition: str, dsl_version: str = "cel-2", target: str = "$") -> EvalUnit:
    return EvalUnit.model_validate(
        {
            "id": 0,
            "rule": make_rule(condition, target).model_dump(by_alias=True),
            "dsl_version": dsl_version,
        }
    )


# --- changes -----------------------------------------------------------------------------------


def test_a_cel2_rule_forbidding_breaking_changes_fails_on_a_breaking_change(
    engine: ExpressionEngine, targeter: Targeter
) -> None:
    result = run(engine, targeter, [unit(NO_BREAKING)], changes=CHANGES_BREAKING)[0]
    assert result.outcome == "fail"


def test_the_same_rule_passes_with_only_info_changes_and_with_none(
    engine: ExpressionEngine, targeter: Targeter
) -> None:
    assert run(engine, targeter, [unit(NO_BREAKING)], changes=CHANGES_INFO)[0].outcome == "pass"
    assert run(engine, targeter, [unit(NO_BREAKING)])[0].outcome == "pass"


def test_changes_is_a_list_never_null_by_default(
    engine: ExpressionEngine, targeter: Targeter
) -> None:
    assert (
        run(engine, targeter, [unit("type(changes) == list && changes.size() == 0")])[0].outcome
        == "pass"
    )


def test_the_same_expression_under_cel1_is_a_compile_error_never_a_pass(
    engine: ExpressionEngine, targeter: Targeter
) -> None:
    with pytest.raises(RuleCompileError) as caught:
        engine.compile(NO_BREAKING, "cel-1")
    assert caught.value.code == "compile_error"
    result = run(engine, targeter, [unit(NO_BREAKING, "cel-1")], changes=CHANGES_INFO)[0]
    assert result.outcome == "error"
    assert result.error_code == "engine_error"
    assert result.problem_code == "compile_error"


@pytest.mark.parametrize("name", ["previous", "changes", "refs"])
def test_a_cel1_environment_rejects_every_cel2_identifier(
    engine: ExpressionEngine, name: str
) -> None:
    with pytest.raises(RuleCompileError):
        engine.compile(f"{name} == null", "cel-1")
    assert engine.compile(f"{name} == null", "cel-2") is not None


# --- previous ----------------------------------------------------------------------------------

GUARDED = "previous == null || previous.version == '1.0.0'"


def test_a_guarded_previous_rule_is_safe_on_a_first_publish(
    engine: ExpressionEngine, targeter: Targeter
) -> None:
    assert run(engine, targeter, [unit(GUARDED)])[0].outcome == "pass"
    assert run(engine, targeter, [unit(GUARDED)], previous={"version": "1.0.0"})[0].outcome == (
        "pass"
    )
    assert run(engine, targeter, [unit(GUARDED)], previous={"version": "2.0.0"})[0].outcome == (
        "fail"
    )


def test_has_on_a_null_previous_is_an_evaluation_error_never_a_pass(
    engine: ExpressionEngine, targeter: Targeter
) -> None:
    result = run(engine, targeter, [unit("has(previous.version)")])[0]
    assert result.outcome == "error"
    assert result.findings[0].error_code is not None


def test_previous_is_bound_in_the_raw_document_shape(
    engine: ExpressionEngine, targeter: Targeter
) -> None:
    previous = {"schema": [{"name": "orders", "properties": [{"name": "id"}]}]}
    condition = "previous.schema[0].properties[0].name == 'id'"
    assert run(engine, targeter, [unit(condition)], previous=previous)[0].outcome == "pass"


def test_previous_is_normalised_like_the_document(
    engine: ExpressionEngine, targeter: Targeter
) -> None:
    big = 2**70
    previous = {"n": big}
    document = {"n": big}
    both = "previous.n > 1.0 && contract.n > 1.0"
    result = run(engine, targeter, [unit(both)], document, previous=previous)[0]
    assert result.outcome == "pass"


# --- refs --------------------------------------------------------------------------------------


def test_refs_has_the_five_maps_by_default_and_the_guard_idiom_works(
    engine: ExpressionEngine, targeter: Targeter
) -> None:
    keys = "refs.elements.size() == 0 && refs.teams.size() == 0 && refs.contracts.size() == 0"
    assert run(engine, targeter, [unit(keys)])[0].outcome == "pass"
    assert (
        run(engine, targeter, [unit("refs.principals.size() == 0 && refs.servers.size() == 0")])[
            0
        ].outcome
        == "pass"
    )


def test_refs_supplied_by_the_job_are_visible(engine: ExpressionEngine, targeter: Targeter) -> None:
    refs = {"elements": {"orders.id": "not_found"}}
    condition = "!('orders.id' in refs.elements) || refs.elements['orders.id'] != 'not_found'"
    assert run(engine, targeter, [unit(condition)], refs=refs)[0].outcome == "fail"


# --- match cap ---------------------------------------------------------------------------------


def test_previous_and_contract_select_the_lower_cap_but_changes_and_refs_do_not() -> None:
    assert references_document_binding("previous.x == 1")
    assert references_document_binding("has(contract.x)")
    assert not references_document_binding("changes.size() == 0 && refs.teams.size() == 0")
    assert not references_document_binding("this.previous == 1")
    assert not references_document_binding("this.a == 'previous'")


def test_a_cel2_condition_referencing_previous_is_capped_at_the_contract_limit(
    engine: ExpressionEngine, targeter: Targeter
) -> None:
    document = {"xs": list(range(200))}
    limits = LimitsWire.from_limits(Limits(max_matches=1000, max_matches_with_contract=100))
    over = run(
        engine, targeter, [unit("previous == null", target="$.xs[*]")], document, limits=limits
    )[0]
    assert over.error_code is None
    assert [f.error_code for f in over.findings] == ["too_many_matches"]
    free = run(
        engine, targeter, [unit("changes.size() >= 0", target="$.xs[*]")], document, limits=limits
    )[0]
    assert free.outcome == "pass"
    assert free.matched == 200


# --- cel-1 stays as it was --------------------------------------------------------------------


def test_a_cel1_unit_evaluates_as_before_and_ignores_the_cel2_job_bindings(
    engine: ExpressionEngine, targeter: Targeter
) -> None:
    result = run(
        engine,
        targeter,
        [unit("this.a == 1", "cel-1")],
        {"a": 1},
        changes=CHANGES_BREAKING,
        previous={"x": 1},
    )[0]
    assert result.outcome == "pass"


# --- dc.type_coherent (D-154) ------------------------------------------------------------------


def coherent(engine: ExpressionEngine, logical: str, physical: str) -> Any:
    condition = f"dc.type_coherent({json.dumps(logical)}, {json.dumps(physical)})"
    return engine.evaluate(
        engine.compile(condition, "cel-2"),
        {"this": None, "contract": {}, "ctx": {}, "previous": None, "changes": [], "refs": {}},
    )


@pytest.mark.parametrize(
    ("logical", "physical", "expected"),
    [
        ("string", "VARCHAR(50)", True),
        ("string", "text", True),
        ("integer", "varchar", False),
        ("integer", "BIGINT", True),
        ("number", "DECIMAL(10,2)", True),
        ("number", "int", True),
        ("number", "varchar(10)", False),
        ("boolean", "BOOL", True),
        ("boolean", "date", False),
        ("date", "DATE", True),
        ("date", "varchar", False),
        ("timestamp", "timestamptz", True),
        ("timestamp", "integer", False),
        ("object", "jsonb", True),
        ("array", "ARRAY<int>", True),
        ("date", "mystery_type", True),
        ("string", "", True),
        ("mystery_logical", "varchar", True),
        ("string", "x" * 257, True),
        ("x" * 257, "int", True),
    ],
)
def test_type_coherent_mapping(
    engine: ExpressionEngine, logical: str, physical: str, expected: bool
) -> None:
    assert coherent(engine, logical, physical) is expected


def test_dc_functions_do_not_exist_in_cel_1(engine: ExpressionEngine) -> None:
    with pytest.raises(RuleCompileError):
        engine.compile("dc.type_coherent('string', 'int')", "cel-1")


def test_an_exception_in_a_dc_function_is_the_fixed_engine_error_and_leaks_no_text() -> None:
    from dc_rules.cel_engine import CelEngine
    from dc_rules.functions import DcFunction

    def boom(logical: str, physical: str) -> bool:
        raise RuntimeError("SECRET-VALUE-IN-TEXT")

    engine = CelEngine(functions=(DcFunction("dc.type_coherent", "dc_type_coherent", boom),))
    result = engine.evaluate(
        engine.compile("dc.type_coherent('a', 'b')", "cel-2"),
        {"this": None, "contract": {}, "ctx": {}, "previous": None, "changes": [], "refs": {}},
    )
    assert result == "engine_error"


def test_an_exception_in_a_dc_function_reaches_no_unit_result(targeter: Targeter) -> None:
    from dc_rules.cel_engine import CelEngine
    from dc_rules.functions import DcFunction

    def boom(logical: str, physical: str) -> bool:
        raise RuntimeError("SECRET-VALUE-IN-TEXT")

    engine = CelEngine(functions=(DcFunction("dc.type_coherent", "dc_type_coherent", boom),))
    result = run(engine, targeter, [unit("dc.type_coherent('a', 'b')")])[0]
    assert result.outcome == "error"
    assert "SECRET" not in result.model_dump_json()
