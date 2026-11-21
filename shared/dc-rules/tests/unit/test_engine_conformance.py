"""Engine conformance suite: the spike findings that the rule-evaluation design rests on (D-138).

Decision recorded here. In the blocking spike, evaluation released the GIL and a shared `Env`
survived 8 threads, but an evaluation cannot be cancelled and its memory is unbounded, and the
iteration budget (about 10,000 loop iterations) is a fixed engine constant. Evaluation therefore
runs in worker subprocesses with RLIMIT_AS and a parent-enforced deadline (plan 05-03). These
tests pin the behaviours that decision depends on: the iteration budget, runtime errors being
returned rather than raised, RE2 regex, the parser limits and the cel-1 surface. They are
parameterised over the `engine` fixture and must be re-run, unchanged, against any new engine pin
or second adapter before it replaces the current one.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from dc_rules.normalize import normalize_document
from dc_rules.ports import ExpressionEngine, RuleCompileError

pytestmark = pytest.mark.unit


def evaluate(engine: ExpressionEngine, condition: str, this: Any = None, **extra: Any) -> Any:
    bindings = {"this": this, "contract": extra.get("contract", {}), "ctx": extra.get("ctx", {})}
    return engine.evaluate(engine.compile(condition), bindings)


def evaluate_in(
    engine: ExpressionEngine, dsl_version: str, condition: str, this: Any = None
) -> Any:
    bindings: dict[str, Any] = {"this": this, "contract": {}, "ctx": {}}
    if dsl_version == "cel-2":
        bindings.update({"previous": None, "changes": [], "refs": {}})
    return engine.evaluate(engine.compile(condition, dsl_version), bindings)


def compile_code(engine: ExpressionEngine, condition: str) -> str:
    with pytest.raises(RuleCompileError) as caught:
        engine.compile(condition)
    return caught.value.code


# --- iteration budget (D-138, T-5-02) ---------------------------------------------------------


def test_an_all_over_9999_elements_is_within_the_iteration_budget(
    engine: ExpressionEngine,
) -> None:
    assert evaluate(engine, "this.xs.all(x, x >= 0)", {"xs": list(range(9_999))}) is True


def test_an_all_over_10000_elements_exceeds_the_iteration_budget(
    engine: ExpressionEngine,
) -> None:
    result = evaluate(engine, "this.xs.all(x, x >= 0)", {"xs": list(range(10_000))})
    assert result == "budget_exceeded"


def test_a_nested_comprehension_hits_the_same_budget_as_its_total_iteration_count(
    engine: ExpressionEngine,
) -> None:
    condition = "this.xs.all(a, this.xs.all(b, a >= 0 && b >= 0))"
    assert evaluate(engine, condition, {"xs": list(range(50))}) is True
    assert evaluate(engine, condition, {"xs": list(range(100))}) == "budget_exceeded"


# --- error channel: returned, never raised ----------------------------------------------------


def test_a_missing_key_is_returned_as_no_such_key_not_raised(engine: ExpressionEngine) -> None:
    assert evaluate(engine, "this.a.y == 1", {"a": {}}) == "no_such_key"


def test_has_guards_a_missing_key_so_it_is_false_not_an_error(engine: ExpressionEngine) -> None:
    assert evaluate(engine, "has(this.a.y)", {"a": {}}) is False


# --- error channel (continued) --------------------------------------------------


def test_adding_a_string_and_an_int_is_a_type_error(engine: ExpressionEngine) -> None:
    assert evaluate(engine, "this.s + 1 == 2", {"s": "x"}) == "type_error"


def test_the_same_mistake_with_literals_is_caught_at_compile_time(
    engine: ExpressionEngine,
) -> None:
    assert compile_code(engine, '"x" + 1 == 2') == "compile_error"


def test_division_by_zero_is_an_invalid_argument(engine: ExpressionEngine) -> None:
    assert evaluate(engine, "1 / 0 == 1") == "invalid_argument"


def test_an_invalid_regular_expression_is_an_invalid_argument(engine: ExpressionEngine) -> None:
    assert evaluate(engine, '"a".matches("(")') == "invalid_argument"


def test_a_non_boolean_result_is_not_boolean(engine: ExpressionEngine) -> None:
    # `this.a` compiles (DYN); the value bound at evaluation time is an int.
    assert evaluate(engine, "this.a", {"a": 3}) == "not_boolean"


# --- RE2 -------------------------------------------------------------------------------------


def test_a_catastrophic_regex_returns_quickly_because_matches_is_re2(
    engine: ExpressionEngine,
) -> None:
    started = time.perf_counter()
    result = evaluate(engine, 'this.s.matches("(x+x+)+y")', {"s": "x" * 1_000})
    assert result is False
    assert time.perf_counter() - started < 1.0


# --- parser and surface limits (D-111, D-136) -------------------------------------------------


@pytest.mark.parametrize(
    "condition",
    [
        "previous.x == 1",
        "now() > 0",
        "(" * 33 + "true" + ")" * 33,
        "true || " * 20_000 + "true",
    ],
    ids=["previous-undeclared", "now-undeclared", "recursion-limit-33", "over-100000-codepoints"],
)
def test_conditions_outside_the_cel_1_surface_fail_to_compile(
    engine: ExpressionEngine, condition: str
) -> None:
    assert compile_code(engine, condition) == "compile_error"


@pytest.mark.parametrize(
    "condition",
    [
        "this.?a.?y.orValue(3) == 3",
        '"A".lowerAscii() == "a"',
        "[3,1].sort() == [1,3]",
        "cel.bind(x, 1, x == 1)",
        "math.greatest(1, 2) == 2",
    ],
    ids=["optional", "strings", "lists", "bindings", "math"],
)
def test_the_cel_1_extension_set_is_available(engine: ExpressionEngine, condition: str) -> None:
    assert evaluate(engine, condition, {}) is True


def test_a_condition_returning_an_int_is_not_boolean_at_compile_time(
    engine: ExpressionEngine,
) -> None:
    assert compile_code(engine, "1 + 1") == "not_boolean"


def test_a_dynamic_condition_compiles(engine: ExpressionEngine) -> None:
    assert engine.compile("this.a") is not None


def test_contract_and_ctx_are_bound_dynamic_variables(engine: ExpressionEngine) -> None:
    result = evaluate(
        engine,
        'contract.id == "c1" && ctx.folder_kind == "team"',
        {},
        contract={"id": "c1"},
        ctx={"folder_kind": "team"},
    )
    assert result is True


# --- out-of-range integers (pitfall 8) --------------------------------------------------------


def test_an_out_of_range_integer_errors_until_the_document_is_normalised(
    engine: ExpressionEngine,
) -> None:
    document = {"n": 2**70}
    assert evaluate(engine, "this.n > 0", document) == "invalid_argument"
    assert evaluate(engine, "this.n > 0", normalize_document(document)) is True


def test_an_out_of_range_list_index_is_an_engine_error_and_a_size_guard_avoids_it(
    engine: ExpressionEngine,
) -> None:
    # Decision (plan 05-02): no dedicated error code. The engine reports this as UNKNOWN text
    # ("Index error"), which maps to engine_error; authors guard with size(). An engine upgrade
    # that reclassifies it should be a deliberate change to the error-mapping table.
    document = {"xs": [1]}
    assert evaluate(engine, "this.xs[5] == 1", document) == "engine_error"
    assert evaluate(engine, "size(this.xs) > 5 && this.xs[5] == 1", document) is False


# --- cel-1 is a strict subset of cel-2 (promote invariant, D-152) ----------------------------

CEL1_CASES: list[tuple[str, Any, Any]] = [
    ("this.xs.all(x, x >= 0)", {"xs": list(range(9_999))}, True),
    ("this.xs.all(x, x >= 0)", {"xs": list(range(10_000))}, "budget_exceeded"),
    ("this.a.y == 1", {"a": {}}, "no_such_key"),
    ("has(this.a.y)", {"a": {}}, False),
    ("this.s + 1 == 2", {"s": "x"}, "type_error"),
    ("1 / this.z == 1", {"z": 0}, "invalid_argument"),
    ('this.s.matches("(a+)+$")', {"s": "a" * 40 + "b"}, False),
    ("this.?a.?y.orValue(3) == 3", {}, True),
    ('"A".lowerAscii() == "a"', {}, True),
    ("[3,1].sort() == [1,3]", {}, True),
    ("cel.bind(x, 1, x == 1)", {}, True),
    ("math.greatest(1, 2) == 2", {}, True),
    ("this.a", {"a": 1}, "not_boolean"),
    ("size(this.xs) > 5 && this.xs[5] == 1", {"xs": [1]}, False),
]


@pytest.mark.parametrize(("condition", "this", "expected"), CEL1_CASES)
def test_every_cel_1_case_gives_the_same_answer_under_cel_2(
    engine: ExpressionEngine, condition: str, this: Any, expected: Any
) -> None:
    assert evaluate_in(engine, "cel-1", condition, this) == expected
    assert evaluate_in(engine, "cel-2", condition, this) == expected


@pytest.mark.parametrize("condition", ["now() > 0", "(" * 33 + "true" + ")" * 33])
def test_the_cel_1_compile_failures_are_compile_failures_under_cel_2_too(
    engine: ExpressionEngine, condition: str
) -> None:
    for dsl_version in ("cel-1", "cel-2"):
        with pytest.raises(RuleCompileError):
            engine.compile(condition, dsl_version)


@pytest.mark.parametrize("dsl_version", ["cel-3", "CEL-2", ""])
def test_an_unknown_dsl_version_is_a_compile_error_never_a_fallback(
    engine: ExpressionEngine, dsl_version: str
) -> None:
    with pytest.raises(RuleCompileError) as caught:
        engine.compile("true", dsl_version)
    assert caught.value.code == "compile_error"
    assert "dsl_version" in caught.value.detail


# --- cel-2 conformance: dc.type_coherent and the refs guard idiom (D-152, D-154) ---------------

REFS_GUARD = "!(this.ref in refs.elements) || refs.elements[this.ref] != 'not_found'"


def evaluate_cel2(engine: ExpressionEngine, condition: str, this: Any, refs: Any) -> Any:
    bindings = {
        "this": this,
        "contract": {},
        "ctx": {},
        "previous": None,
        "changes": [],
        "refs": refs,
    }
    return engine.evaluate(engine.compile(condition, "cel-2"), bindings)


@pytest.mark.parametrize(
    ("this", "expected"),
    [
        ({"ref": "a.b"}, False),  # not_found fails
        ({"ref": "a.c"}, True),  # found passes
        ({"ref": "a.d"}, True),  # unverifiable passes
        ({"ref": "a.zzz"}, True),  # absent key is guarded, not a NOT_FOUND error
    ],
)
def test_the_refs_guard_idiom_is_tri_state_aware_and_never_errors_on_an_absent_key(
    engine: ExpressionEngine, this: Any, expected: bool
) -> None:
    refs = {"elements": {"a.b": "not_found", "a.c": "found", "a.d": "unverifiable"}}
    assert evaluate_cel2(engine, REFS_GUARD, this, refs) is expected


def test_the_unguarded_refs_lookup_of_an_absent_key_is_an_error_not_a_pass(
    engine: ExpressionEngine,
) -> None:
    result = evaluate_cel2(engine, "refs.elements['nope'] != 'not_found'", {}, {"elements": {}})
    assert result == "no_such_key"


@pytest.mark.parametrize(
    ("logical", "physical", "expected"),
    [("string", "VARCHAR(50)", True), ("integer", "varchar", False), ("date", "mystery", True)],
)
def test_dc_type_coherent_is_registered_in_the_cel_2_environment(
    engine: ExpressionEngine, logical: str, physical: str, expected: bool
) -> None:
    condition = f"dc.type_coherent('{logical}', '{physical}')"
    assert evaluate_cel2(engine, condition, {}, {}) is expected
