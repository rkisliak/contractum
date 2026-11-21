"""Library quality metrics: severities, operators, units, honest skips."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest
from support.engine import prop, run_properties
from support.odcs import contract_doc, orders_table

from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.backend import RunOptions
from contractum.engine.checks import CheckOutcome, ColumnPresent, QualityMetric, RowCount
from contractum.engine.nested import NestedColumn
from contractum.engine.plan import PlanOptions, plan
from contractum.engine.quality import evaluate_quality
from contractum.odcs import find_object
from contractum.report.model import CheckResult, ContractInfo, DatasetInfo, ToolInfo, build_report


def _amount_rule(document: dict[str, Any]) -> dict[str, Any]:
    properties = document["schema"][0]["properties"]
    amount = next(item for item in properties if item["name"] == "amount")
    return amount["quality"][0]  # type: ignore[no-any-return]


def _run_fixture(table: pa.Table, *, severity: str | None = "warning") -> CheckOutcome:
    document = contract_doc()
    document = copy.deepcopy(document)
    rule = _amount_rule(document)
    if severity is None:
        rule.pop("severity")
    else:
        rule["severity"] = severity
    the_plan = plan(find_object(document), PlanOptions())
    outcomes = ArrowBackend(table).evaluate(the_plan.checks, RunOptions())
    return next(o for o in outcomes if o.check_id == "orders.amount.quality[0]")


def _with_amounts(*amounts: Decimal | None) -> pa.Table:
    table = orders_table()
    return table.set_column(
        table.schema.get_field_index("amount"), "amount", pa.array(amounts, pa.decimal128(10, 2))
    )


def test_tracer_a_warning_null_rule_reaches_the_outcome_as_warning() -> None:
    table = _with_amounts(None, Decimal("1.00"), None)

    outcome = _run_fixture(table)

    assert outcome.status == "warning"
    assert outcome.severity == "warning"
    assert outcome.observed == 2
    assert outcome.threshold == 0
    assert outcome.failed_rows == 2
    assert outcome.total_rows == 3
    assert [sample.row for sample in outcome.samples] == [0, 2]


def test_the_same_failing_rule_is_failed_when_its_severity_is_error_or_missing() -> None:
    table = _with_amounts(None, Decimal("1.00"), Decimal("2.00"))

    explicit = _run_fixture(table, severity="error")
    absent = _run_fixture(table, severity=None)

    assert (explicit.status, explicit.observed) == ("failed", 1)
    assert (absent.status, absent.observed) == ("failed", 1)


def test_a_rule_without_nulls_passes_and_keeps_the_observed_count() -> None:
    outcome = _run_fixture(_with_amounts(Decimal("1.00"), Decimal("2.00"), Decimal("3.00")))

    assert outcome.status == "passed"
    assert outcome.observed == 0
    assert outcome.failed_rows == 0
    assert outcome.samples == ()


# --- operators and units ----------------------------------------------------------------------

_NULLS = pa.table({"x": pa.array([1, None, 2, None], pa.int64())})  # two nulls of four rows


def _quality_outcome(
    rule: dict[str, Any],
    table: pa.Table = _NULLS,
    *,
    column: str | None = "x",
    run: RunOptions | None = None,
    **plan_options: Any,
) -> CheckResult:
    """The report entry of one library rule placed on property ``x`` (or on the object)."""
    full = {"type": "library", **rule}
    schema: dict[str, Any] = {
        "name": "t",
        "properties": [prop("x", "integer", quality=[full] if column else [])],
    }
    if column is None:
        schema["quality"] = [full]
    the_plan = plan(find_object(contract_doc(schema=[schema])), PlanOptions(**plan_options))
    backend = ArrowBackend(table, sampled=bool(plan_options.get("sampled")))
    outcomes = backend.evaluate(the_plan.checks, run or RunOptions())
    report = build_report(
        tool=ToolInfo(version="0"),
        contract=ContractInfo(source="file"),
        dataset=DatasetInfo(kind="arrow"),
        plan=the_plan,
        outcomes=outcomes,
    )
    return next(check for check in report.checks if check.category == "quality")


@pytest.mark.parametrize(
    ("operator", "holds", "breaks"),
    [
        ("mustBe", 2, 3),
        ("mustNotBe", 3, 2),
        ("mustBeGreaterThan", 1, 2),
        ("mustBeGreaterOrEqualTo", 2, 3),
        ("mustBeLessThan", 3, 2),
        ("mustBeLessOrEqualTo", 2, 1),
        ("mustBeBetween", [1, 3], [3, 5]),
        ("mustNotBeBetween", [3, 5], [1, 3]),
    ],
)
def test_the_eight_operators_pass_and_fail_on_two_nulls(
    operator: str, holds: Any, breaks: Any
) -> None:
    rule = {"metric": "nullValues"}

    passing = _quality_outcome({**rule, operator: holds})
    failing = _quality_outcome({**rule, operator: breaks})

    assert (passing.status, passing.observed) == ("passed", 2)
    assert (failing.status, failing.observed) == ("failed", 2)
    assert failing.message is not None
    assert "nullValues is 2" in failing.message


@pytest.mark.parametrize(
    ("operator", "threshold", "status"),
    [
        ("mustBe", 50, "passed"),
        ("mustBe", 25, "failed"),
        ("mustBeLessThan", 60.5, "passed"),
        ("mustBeBetween", [10, 40], "failed"),
    ],
)
def test_percent_compares_the_share_of_rows(operator: str, threshold: Any, status: str) -> None:
    outcome = _quality_outcome({"metric": "nullValues", "unit": "percent", operator: threshold})

    assert outcome.status == status
    assert outcome.observed == 50.0
    assert (outcome.total_rows, outcome.threshold) == (4, threshold)


def test_percent_of_zero_rows_is_zero() -> None:
    empty = pa.table({"x": pa.array([], pa.int64())})

    outcome = _quality_outcome({"metric": "nullValues", "unit": "percent", "mustBe": 0}, empty)

    assert (outcome.status, outcome.observed, outcome.total_rows) == ("passed", 0.0, 0)


def test_nan_counts_as_null() -> None:
    table = pa.table({"x": pa.array([1.0, float("nan"), None], pa.float64())})

    outcome = _quality_outcome({"metric": "nullValues", "mustBe": 2}, table)

    assert (outcome.status, outcome.observed) == ("passed", 2)


# --- the other metrics ------------------------------------------------------------------------


def test_missing_values_count_nulls_and_the_listed_values() -> None:
    table = pa.table({"x": pa.array(["a", "", "N/A", None, "b"], pa.string())})
    rule = {"metric": "missingValues", "arguments": {"missingValues": ["", "N/A"]}}

    outcome = _quality_outcome({**rule, "mustBe": 3}, table)
    fails = _quality_outcome({**rule, "mustBe": 0}, table)

    assert (outcome.status, outcome.observed) == ("passed", 3)
    assert [s.row for s in fails.samples] == [1, 2, 3]


def test_missing_values_without_a_list_count_only_nulls() -> None:
    outcome = _quality_outcome({"metric": "missingValues", "mustBe": 2})

    assert (outcome.status, outcome.observed) == ("passed", 2)


@pytest.mark.parametrize(
    ("arguments", "rows"),
    [
        ({"validValues": ["a", "b"]}, [2, 3, 4]),
        ({"pattern": "^[a-z]$"}, [2, 3]),
        ({"minLength": 2}, [0, 1, 2, 4]),
        ({"maxLength": 1}, [3]),
        ({"validValues": ["a", "ddd"], "pattern": "^[a-z]+$"}, [1, 2, 4]),
    ],
)
def test_invalid_values_judge_valid_values_pattern_and_length(
    arguments: dict[str, Any], rows: list[int]
) -> None:
    table = pa.table({"x": pa.array(["a", "b", "C", "ddd", "z", None], pa.string())})

    outcome = _quality_outcome(
        {"metric": "invalidValues", "mustBe": 0, "arguments": arguments},
        table,
        run=RunOptions(max_failures=10),
    )

    assert outcome.status == "failed"
    assert outcome.observed == len(rows)
    assert [sample.row for sample in outcome.samples] == rows


def test_duplicate_values_of_a_property_count_surplus_rows() -> None:
    table = pa.table({"x": pa.array([1, 1, 1, 2, 2, 3, None, None], pa.int64())})

    passing = _quality_outcome({"metric": "duplicateValues", "mustBe": 3}, table)
    failing = _quality_outcome(
        {"metric": "duplicateValues", "mustBe": 0}, table, run=RunOptions(max_failures=10)
    )

    assert (passing.status, passing.observed) == ("passed", 3)
    assert failing.status == "failed"
    assert [sample.row for sample in failing.samples] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize(
    "arguments",
    [
        {"properties": ["a", "b"]},
        {"arguments": {"properties": ["a", "b"]}},
    ],
)
def test_object_level_duplicate_values_use_composite_keys_nested_or_sibling(
    arguments: dict[str, Any],
) -> None:
    table = pa.table({"a": ["x", "x", "y", "x"], "b": [1, 1, 1, 2], "x": [0, 0, 0, 0]})
    flat = arguments if "arguments" in arguments else {"arguments": arguments}
    rule = {"type": "library", "metric": "duplicateValues", "mustBe": 0, **flat}
    schema = {
        "name": "t",
        "properties": [prop("a", "string"), prop("b", "integer")],
        "quality": [rule],
    }
    the_plan = plan(find_object(contract_doc(schema=[schema])))

    outcome = next(
        o
        for o in ArrowBackend(table).evaluate(the_plan.checks, RunOptions())
        if o.category == "quality"
    )

    assert (outcome.status, outcome.observed) == ("failed", 1)
    assert [sample.row for sample in outcome.samples] == [0, 1]
    assert outcome.samples[0].value == ["x", "1"]


def test_row_count_uses_the_operator_on_the_rows_tested() -> None:
    passing = _quality_outcome({"metric": "rowCount", "mustBeBetween": [3, 5]}, column=None)
    failing = _quality_outcome({"metric": "rowCount", "mustBeLessThan": 4}, column=None)

    assert (passing.status, passing.observed, passing.failed_rows) == ("passed", 4, None)
    assert (failing.status, failing.observed, failing.total_rows) == ("failed", 4, 4)


# --- honest skips -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "column", "reason"),
    [
        ({"metric": "nullValues"}, "x", "library rule has no threshold operator"),
        ({"metric": "madeUp", "mustBe": 0}, "x", "unknown quality metric 'madeUp'"),
        ({"mustBe": 0}, "x", "library rule has no metric"),
        ({"type": "text", "description": "docs"}, "x", "text rule: documentation only"),
        ({"type": "sql", "query": "SELECT 1", "mustBe": 0}, "x", "sql rules are not evaluated"),
        ({"type": "custom", "engine": "soda"}, "x", "custom engine rule: soda"),
        ({"type": "custom"}, "x", "custom engine rule: unspecified"),
        ({"type": "weird"}, "x", "quality rule type 'weird' is not recognised"),
        ({"metric": "nullValues", "mustBe": 0}, None, "nullValues needs a property"),
        ({"metric": "nullValues", "mustBe": "zero"}, "x", "mustBe needs a number threshold"),
        ({"metric": "nullValues", "mustBeBetween": [5, 1]}, "x", "needs two numbers"),
        ({"metric": "nullValues", "mustBeBetween": 3}, "x", "needs two numbers"),
        ({"metric": "nullValues", "mustBe": 0, "unit": "bytes"}, "x", "unit 'bytes'"),
        ({"metric": "rowCount", "mustBe": 0, "unit": "percent"}, None, "unit percent"),
        ({"metric": "invalidValues", "mustBe": 0}, "x", "needs validValues, pattern"),
        (
            {"metric": "invalidValues", "mustBe": 0, "arguments": {"pattern": "(?=a)"}},
            "x",
            "unsupported by the linear-time engine",
        ),
        (
            {"metric": "invalidValues", "mustBe": 0, "arguments": {"validValues": "a"}},
            "x",
            "validValues must be a list",
        ),
        (
            {"metric": "invalidValues", "mustBe": 0, "arguments": {"pattern": 5}},
            "x",
            "pattern is not a text value",
        ),
        (
            {"metric": "invalidValues", "mustBe": 0, "arguments": {"minLength": "a"}},
            "x",
            "whole numbers",
        ),
        (
            {"metric": "missingValues", "mustBe": 0, "arguments": {"missingValues": "a"}},
            "x",
            "missingValues must be a list",
        ),
        ({"metric": "duplicateValues", "mustBe": 0}, None, "needs arguments.properties"),
        (
            {"metric": "duplicateValues", "mustBe": 0, "arguments": {"properties": ["nope"]}},
            None,
            "unknown property 'nope'",
        ),
    ],
)
def test_rules_that_cannot_be_evaluated_are_skipped_with_a_reason(
    rule: dict[str, Any], column: str | None, reason: str
) -> None:
    outcome = _quality_outcome(rule, column=column)

    assert outcome.status == "skipped"
    assert outcome.skipped_reason is not None
    assert reason in outcome.skipped_reason


def test_a_skipped_rule_keeps_its_severity_and_never_passes() -> None:
    outcome = _quality_outcome({"type": "sql", "severity": "info"})

    assert (outcome.status, outcome.severity) == ("skipped", "warning")


def test_a_sql_rule_is_never_executed() -> None:
    dangerous = "SELECT 1; DROP TABLE t -- __import__('os').system('echo pwned')"

    outcome = _quality_outcome({"type": "sql", "query": dangerous, "mustBe": 0})

    assert outcome.skipped_reason == "sql rules are not evaluated locally"
    assert dangerous not in outcome.model_dump_json()


def test_metrics_the_column_type_cannot_judge_are_skipped() -> None:
    table = pa.table({"x": pa.array([[1], [1]], pa.list_(pa.int64()))})
    duplicate = _quality_outcome({"metric": "duplicateValues", "mustBe": 0}, table)
    missing = _quality_outcome(
        {"metric": "missingValues", "mustBe": 0, "arguments": {"missingValues": [1]}}, table
    )

    assert duplicate.status == "skipped"
    assert "duplicateValues is not evaluated on" in str(duplicate.skipped_reason)
    assert missing.status == "skipped"
    assert "missingValues is not evaluated on" in str(missing.skipped_reason)


def test_a_missing_column_skips_the_rule() -> None:
    outcome = _quality_outcome({"metric": "nullValues", "mustBe": 0}, pa.table({"y": [1]}))

    assert outcome.status == "skipped"
    assert outcome.skipped_reason == "column is missing from the dataset"


# --- sampling ---------------------------------------------------------------------------------


def test_row_count_of_a_sample_is_skipped() -> None:
    outcome = _quality_outcome(
        {"metric": "rowCount", "mustBeGreaterThan": 0}, column=None, sampled=True
    )

    assert outcome.status == "skipped"
    assert outcome.skipped_reason == "row count of a sample is meaningless"


def test_a_duplicate_pass_on_a_sample_says_so_and_carries_the_sampled_flag() -> None:
    table = pa.table({"x": pa.array([1, 2, 3, 4], pa.int64())})

    outcome = _quality_outcome({"metric": "duplicateValues", "mustBe": 0}, table, sampled=True)
    unique = run_properties([prop("x", "integer", unique=True)], table)["t.x.unique"]

    assert outcome.status == "passed"
    assert outcome.sampled is True
    assert outcome.message == "passed on a sample only"
    assert unique.status == "passed"


def test_a_null_pass_on_a_sample_carries_no_sample_note() -> None:
    outcome = _quality_outcome({"metric": "nullValues", "mustBeGreaterThan": 1}, sampled=True)

    assert outcome.status == "passed"
    assert outcome.message is None


# --- defensive paths for hand-built specs -----------------------------------------------------


def _metric(**fields: Any) -> QualityMetric:
    base: dict[str, Any] = {
        "check_id": "t.x.quality[0]",
        "category": "quality",
        "rule": "quality",
        "column": "x",
        "metric": "nullValues",
        "operator": "mustBe",
        "threshold": 0,
    }
    return QualityMetric(**{**base, **fields})


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        (_metric(operator=None), "library rule has no threshold operator"),
        (_metric(operator="mustBeSomething"), "library rule has no threshold operator"),
        (_metric(threshold="zero"), "mustBe needs a number threshold"),
        (_metric(metric="madeUp"), "unknown quality metric 'madeUp'"),
        (RowCount(check_id="r", category="quality", rule="quality", operator=None), "operator"),
    ],
)
def test_evaluate_quality_skips_specs_that_were_not_planned_by_the_planner(
    spec: QualityMetric | RowCount, reason: str
) -> None:
    result = evaluate_quality(spec, [NestedColumn(pa.chunked_array([[1]]))], rows=1)

    assert result.passed is None
    assert reason in str(result.skipped_reason)


def test_evaluate_quality_skips_a_sampled_row_count() -> None:
    spec = RowCount(
        check_id="r", category="quality", rule="quality", operator="mustBe", threshold=1
    )

    result = evaluate_quality(spec, [], rows=1, sampled=True)

    assert result.skipped_reason == "row count of a sample is meaningless"


def test_an_invalid_values_condition_the_column_cannot_judge_skips_the_rule() -> None:
    table = pa.table({"x": pa.array([1, 2], pa.int64())})

    outcome = _quality_outcome(
        {"metric": "invalidValues", "mustBe": 0, "arguments": {"pattern": "^a"}}, table
    )

    assert outcome.status == "skipped"
    assert "pattern applies to string columns" in str(outcome.skipped_reason)


def test_an_object_level_duplicate_key_with_a_missing_column_is_skipped() -> None:
    rule = {
        "type": "library",
        "metric": "duplicateValues",
        "mustBe": 0,
        "arguments": {"properties": ["x"]},
    }
    schema = {"name": "t", "properties": [prop("x", "integer")], "quality": [rule]}
    the_plan = plan(find_object(contract_doc(schema=[schema])))

    outcomes = ArrowBackend(pa.table({"y": [1]})).evaluate(the_plan.checks, RunOptions())
    quality = next(o for o in outcomes if o.category == "quality")

    assert quality.status == "skipped"
    assert quality.skipped_reason == "column 'x' is missing from the dataset"


def test_a_check_type_the_backend_does_not_know_is_skipped_never_passed() -> None:
    @dataclass(frozen=True, kw_only=True)
    class Unknown(ColumnPresent):
        pass

    spec = Unknown(check_id="t.x.unknown", category="schema", rule="unknown", column="x")

    (outcome,) = ArrowBackend(pa.table({"x": [1]})).evaluate([spec], RunOptions())

    assert outcome.status == "skipped"
    assert "not evaluated by the arrow backend" in str(outcome.skipped_reason)
