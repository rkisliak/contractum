"""Constraint checks: enum, pattern, length, range, multipleOf, format and array bounds."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from support.engine import options, plan_properties, prop, run_properties
from support.odcs import contract_doc, orders_table

from contractum.engine import kernels
from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.backend import RunOptions
from contractum.engine.checks import CheckOutcome
from contractum.engine.plan import PlanOptions, plan
from contractum.odcs import find_object

UTC = dt.UTC


def _failed_positions(outcome: CheckOutcome) -> list[int]:
    return [sample.row for sample in outcome.samples]


def _check(
    properties: list[dict[str, Any]], table: pa.Table, rule: str, **plan_options: Any
) -> CheckOutcome:
    name = properties[0]["name"]
    return run_properties(properties, table, **plan_options)[f"t.{name}.{rule}"]


# --- enum -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "allowed",
    [["a", "b"], [{"value": "a"}, {"value": "b", "label": "Bee"}]],
    ids=["v3.0-scalars", "v3.2-objects"],
)
def test_enum_reads_scalars_and_value_objects(allowed: list[Any]) -> None:
    table = pa.table({"s": pa.array(["a", "c", None, "b", "d"])})

    outcome = _check([prop("s", "string", enum=allowed)], table, "enum")

    assert outcome.status == "failed"
    assert (outcome.failed_rows, outcome.total_rows) == (2, 5)
    assert [(s.row, s.value) for s in outcome.samples] == [(1, "c"), (4, "d")]


@pytest.mark.parametrize(
    ("column", "allowed", "failing"),
    [
        (pa.array([1, 2, 3], pa.int32()), [1, 2], [2]),
        (pa.array([1, 2, 3], pa.int64()), [1.0, 2.0], [2]),
        (pa.array([1, 2, 3], pa.int64()), ["1", "2"], [0, 1, 2]),
        (pa.array([1.5, 2.5], pa.float32()), [1.5], [1]),
        (pa.array([Decimal("1.10"), Decimal("1.20")], pa.decimal128(5, 2)), [1.1], [1]),
        (pa.array([Decimal("1.10"), Decimal("1.20")], pa.decimal128(5, 2)), [1.123], [0, 1]),
        (pa.array([True, False]), [True], [1]),
        (pa.array([dt.date(2026, 1, 1), dt.date(2026, 1, 2)], pa.date32()), ["2026-01-02"], [0]),
        (
            pa.array([dt.datetime(2026, 1, 1, tzinfo=UTC)], pa.timestamp("ms", tz="UTC")),
            ["2026-01-01T00:00:00Z"],
            [],
        ),
    ],
    ids=[
        "int",
        "integral-float-values",
        "text-never-equals-a-number",
        "float32",
        "decimal",
        "decimal-value-that-does-not-fit",
        "bool",
        "date",
        "timestamp",
    ],
)
def test_enum_compares_in_the_column_type(
    column: pa.Array, allowed: list[Any], failing: list[int]
) -> None:
    outcome = _check([prop("c", None, enum=allowed)], pa.table({"c": column}), "enum")

    assert _failed_positions(outcome) == failing
    assert outcome.status == ("failed" if failing else "passed")


@pytest.mark.parametrize(
    "column",
    [
        pa.array(["a"]),
        pa.array([1], pa.int64()),
        pa.array([1.5], pa.float64()),
        pa.array([Decimal("1.5")], pa.decimal128(5, 2)),
        pa.array([True]),
        pa.array([dt.date(2026, 1, 1)], pa.date32()),
        pa.array([dt.datetime(2026, 1, 1)], pa.timestamp("us")),
        pa.array([dt.time(9, 0)], pa.time64("us")),
    ],
    ids=str,
)
def test_contract_values_that_cannot_be_the_column_type_never_match(column: pa.Array) -> None:
    impossible = [[], {}, 10**400, "not a value", "09:00:00+02:00", "2026-13-45", float("nan")]
    if not pa.types.is_string(column.type):
        impossible.append("zzz")

    outcome = _check([prop("c", None, enum=impossible)], pa.table({"c": column}), "enum")

    assert (outcome.status, outcome.failed_rows) == ("failed", 1)


def test_enum_on_a_list_column_is_skipped() -> None:
    outcome = _check([prop("c", None, enum=["a"])], pa.table({"c": pa.array([["a"]])}), "enum")

    assert outcome.status == "skipped"


def test_enum_ignores_null_entries_of_the_contract() -> None:
    outcome = _check([prop("s", "string", enum=["a", None])], pa.table({"s": ["a"]}), "enum")

    assert outcome.status == "passed"


# --- pattern ----------------------------------------------------------------------------------


def test_pattern_is_evaluated_on_distinct_values_and_mapped_back_to_rows() -> None:
    values = ["ORD-000001", "bad", "ORD-000002", "bad", None, "bad"]
    properties = [options("c", "string", pattern="^ORD-[0-9]{6}$")]

    outcome = _check(properties, pa.table({"c": values}), "pattern")

    assert outcome.status == "failed"
    assert (outcome.failed_rows, outcome.total_rows) == (3, 6)
    assert _failed_positions(outcome) == [1, 3, 5]


def test_pattern_is_an_unanchored_search_like_json_schema() -> None:
    properties = [options("c", "string", pattern="[0-9]+")]

    outcome = _check(properties, pa.table({"c": ["abc123def", "none"]}), "pattern")

    assert _failed_positions(outcome) == [1]


def test_a_lookahead_pattern_is_skipped_with_the_engine_reason() -> None:
    the_plan = plan_properties([options("c", "string", pattern="^(?=a)a")])

    (entry,) = the_plan.skipped
    assert entry.check_id == "t.c.pattern"
    assert entry.reason == "pattern uses regex features unsupported by the linear-time engine"
    assert not any(check.rule == "pattern" for check in the_plan.checks)


def test_the_kernel_also_refuses_a_pattern_the_linear_engine_rejects() -> None:
    result = kernels.pattern(pa.chunked_array([pa.array(["a"], pa.large_string())]), "(?=a)")

    assert result.skipped_reason == (
        "pattern uses regex features unsupported by the linear-time engine"
    )


def test_a_pattern_that_is_not_text_is_skipped() -> None:
    the_plan = plan_properties([options("c", "string", pattern=5)])

    assert [entry.reason for entry in the_plan.skipped] == ["pattern is not a text value"]


def test_a_pattern_on_a_non_string_column_is_skipped() -> None:
    outcome = _check([options("c", "string", pattern="a")], pa.table({"c": [1]}), "pattern")

    assert outcome.status == "skipped"


def test_a_catastrophic_pattern_finishes_quickly_on_the_linear_engine() -> None:
    hostile = "a" * 5000 + "!"
    properties = [options("c", "string", pattern="^(a+)+$")]

    outcome = _check(properties, pa.table({"c": [hostile, "aaa"]}), "pattern")

    assert _failed_positions(outcome) == [0]


def test_contract_patterns_never_reach_the_python_regex_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a contract pattern reached the python re module")

    for name in ("compile", "search", "match", "fullmatch", "findall", "sub", "split"):
        monkeypatch.setattr(re, name, refuse)
    table = pa.table({"c": ["ORD-1", "x"]})
    properties = [options("c", "string", pattern="^ORD-[0-9]+$")]

    outcome = _check(properties, table, "pattern")
    skipped = plan_properties([options("c", "string", pattern="(?=x)")])

    assert _failed_positions(outcome) == [1]
    assert [entry.rule for entry in skipped.skipped] == ["pattern"]


def test_engine_modules_that_see_contract_patterns_do_not_import_re() -> None:
    from contractum.engine import kernels as kernel_module
    from contractum.engine import regex as regex_module

    for module in (kernel_module, regex_module):
        assert not hasattr(module, "re")


# --- length -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("constraint", "value", "fails"),
    [
        ({"minLength": 3}, "ab", True),
        ({"minLength": 3}, "abc", False),
        ({"minLength": 3}, "abcd", False),
        ({"maxLength": 3}, "abcd", True),
        ({"maxLength": 3}, "abc", False),
        ({"maxLength": 3}, "ab", False),
        ({"minLength": 2, "maxLength": 2}, "a", True),
        ({"minLength": 2, "maxLength": 2}, "ab", False),
        ({"minLength": 2, "maxLength": 2}, "abc", True),
        ({"minLength": 3.0}, "ab", True),
    ],
)
def test_length_bounds_at_and_around_the_limit(
    constraint: dict[str, Any], value: str, fails: bool
) -> None:
    outcome = _check([options("c", "string", **constraint)], pa.table({"c": [value]}), "length")

    assert (outcome.status == "failed") is fails


def test_length_counts_code_points_without_normalising() -> None:
    emoji = "a\U0001f600b"
    composed = "é"
    decomposed = "é"
    table = pa.table({"c": [emoji, composed, decomposed]})

    at_three = _check([options("c", "string", maxLength=3)], table, "length")
    at_one = _check([options("c", "string", maxLength=1)], table, "length")

    assert at_three.status == "passed"
    assert _failed_positions(at_one) == [0, 2]


def test_length_of_a_binary_column_counts_bytes() -> None:
    table = pa.table({"c": pa.array([b"ab", b"abc", None], pa.binary())})

    outcome = _check([options("c", "string", maxLength=2)], table, "length")

    assert _failed_positions(outcome) == [1]


def test_length_with_fractional_bounds_is_skipped_by_the_plan_and_the_kernel() -> None:
    the_plan = plan_properties([options("c", "string", minLength=2.5)])
    result = kernels.length(pa.chunked_array([pa.array(["a"])]), min_length="x")

    assert [entry.reason for entry in the_plan.skipped] == [
        "minLength and maxLength must be whole numbers"
    ]
    assert result.skipped_reason == "minLength and maxLength must be whole numbers"


def test_length_on_a_number_column_is_skipped() -> None:
    outcome = _check([options("c", "string", minLength=1)], pa.table({"c": [1]}), "length")

    assert outcome.status == "skipped"


# --- range ------------------------------------------------------------------------------------

# Values are bound-1, bound, bound+1 around 10.
_INTEGER_BOUNDS = [
    ({"minimum": 10}, [0]),
    ({"exclusiveMinimum": 10}, [0, 1]),
    ({"maximum": 10}, [2]),
    ({"exclusiveMaximum": 10}, [1, 2]),
    ({"minimum": 10, "maximum": 10}, [0, 2]),
    ({"exclusiveMinimum": 9, "exclusiveMaximum": 11}, [0, 2]),
]


@pytest.mark.parametrize(("constraint", "failing"), _INTEGER_BOUNDS)
@pytest.mark.parametrize("dtype", [pa.int8(), pa.int64(), pa.uint16()], ids=str)
def test_integer_bounds_inclusive_and_exclusive(
    constraint: dict[str, Any], failing: list[int], dtype: pa.DataType
) -> None:
    table = pa.table({"n": pa.array([9, 10, 11], dtype)})

    outcome = _check([options("n", "integer", **constraint)], table, "range")

    assert _failed_positions(outcome) == failing


@pytest.mark.parametrize(
    ("constraint", "failing"),
    [
        ({"minimum": 10.5}, [0]),
        ({"minimum": 11.0}, [0]),
        ({"exclusiveMinimum": 10.5}, [0]),
        ({"exclusiveMinimum": 11}, [0, 1]),
        ({"maximum": 11.5}, [2]),
        ({"maximum": 11}, [2]),
        ({"exclusiveMaximum": 11.5}, [2]),
        ({"exclusiveMaximum": 11}, [1, 2]),
        ({"exclusiveMaximum": 11.0}, [1, 2]),
    ],
)
def test_integer_columns_round_fractional_bounds_toward_the_inside(
    constraint: dict[str, Any], failing: list[int]
) -> None:
    table = pa.table({"n": pa.array([10, 11, 12])})

    outcome = _check([options("n", "integer", **constraint)], table, "range")

    assert _failed_positions(outcome) == failing


def test_the_minimum_ten_and_a_half_means_eleven_for_integers() -> None:
    table = pa.table({"n": pa.array([10, 11])})

    outcome = _check([options("n", "integer", minimum=10.5)], table, "range")

    assert _failed_positions(outcome) == [0]


@pytest.mark.parametrize(
    ("constraint", "failing"),
    [
        ({"minimum": 0.3}, [0]),
        ({"exclusiveMinimum": 0.3}, [0, 1]),
        ({"maximum": 0.3}, [2]),
        ({"exclusiveMaximum": 0.3}, [1, 2]),
    ],
)
def test_float64_bounds_compare_in_ieee_doubles(
    constraint: dict[str, Any], failing: list[int]
) -> None:
    just_below = 0.29999999999999993
    just_above = 0.30000000000000004
    table = pa.table({"x": pa.array([just_below, 0.3, just_above], pa.float64())})

    outcome = _check([options("x", "number", **constraint)], table, "range")

    assert _failed_positions(outcome) == failing


def test_float32_columns_compare_as_float64_values() -> None:
    table = pa.table({"x": pa.array([0.1], pa.float32())})

    at_float = _check([options("x", "number", minimum=0.1)], table, "range")
    above = _check([options("x", "number", maximum=0.1)], table, "range")

    assert at_float.status == "passed"
    assert above.status == "failed"


@pytest.mark.parametrize(
    ("constraint", "failing"),
    [
        ({"minimum": 10.5}, [0]),
        ({"exclusiveMinimum": 10.5}, [0, 1]),
        ({"maximum": 10.5}, [2]),
        ({"exclusiveMaximum": 10.5}, [1, 2]),
        ({"minimum": 10}, []),
    ],
)
def test_decimal_bounds_are_exact(constraint: dict[str, Any], failing: list[int]) -> None:
    column = pa.array([Decimal("10.49"), Decimal("10.50"), Decimal("10.51")], pa.decimal128(10, 2))

    outcome = _check([options("d", "number", **constraint)], pa.table({"d": column}), "range")

    assert _failed_positions(outcome) == failing


def test_a_decimal_equal_to_a_float_bound_passes_exactly() -> None:
    column = pa.array([Decimal("0.10")], pa.decimal128(5, 2))

    outcome = _check([options("d", "number", minimum=0.1)], pa.table({"d": column}), "range")

    assert outcome.status == "passed"


@pytest.mark.parametrize(
    ("column", "constraint", "failing"),
    [
        (
            pa.array([dt.date(2026, 1, 1), dt.date(2026, 1, 2), dt.date(2026, 1, 3)], pa.date32()),
            {"minimum": "2026-01-02", "exclusiveMaximum": "2026-01-03"},
            [0, 2],
        ),
        (
            pa.array([dt.date(2026, 1, 1), dt.date(2026, 1, 2)], pa.date64()),
            {"exclusiveMinimum": "2026-01-01"},
            [0],
        ),
        (
            pa.array(
                [
                    dt.datetime(2026, 11, 21, 9, 29, 59, tzinfo=UTC),
                    dt.datetime(2026, 11, 21, 9, 30, tzinfo=UTC),
                ],
                pa.timestamp("us", tz="UTC"),
            ),
            {"minimum": "2026-11-21T09:30:00Z"},
            [0],
        ),
        (
            pa.array(
                [
                    dt.datetime(2026, 11, 21, 9, 30, tzinfo=UTC),
                    dt.datetime(2026, 11, 21, 9, 30, 1, tzinfo=UTC),
                ],
                pa.timestamp("us", tz="UTC"),
            ),
            {"maximum": "2026-11-21T11:30:00+02:00"},
            [1],
        ),
        (
            pa.array(
                [dt.datetime(2026, 11, 21, 9, 29), dt.datetime(2026, 11, 21, 9, 31)],
                pa.timestamp("us"),
            ),
            {"minimum": "2026-11-21T11:30:00+02:00"},
            [0],
        ),
        (
            pa.array(
                [dt.datetime(2026, 11, 21, 9, 29), dt.datetime(2026, 11, 21, 9, 31)],
                pa.timestamp("us"),
            ),
            {"maximum": "2026-11-21T09:30:00"},
            [1],
        ),
        (
            pa.array(
                [dt.datetime(2026, 11, 21, 0, 0, 0), dt.datetime(2026, 11, 21, 0, 0, 1)],
                pa.timestamp("s"),
            ),
            {"exclusiveMinimum": "2026-11-21T00:00:00.500000"},
            [0],
        ),
        (
            pa.array([dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2)], pa.timestamp("ns")),
            {"minimum": "2026-01-02"},
            [0],
        ),
        (
            pa.array([dt.time(9, 0), dt.time(10, 0)], pa.time32("s")),
            {"minimum": "09:30:00"},
            [0],
        ),
        (
            pa.array([dt.time(9, 0), dt.time(10, 0)], pa.time64("us")),
            {"maximum": "09:30:00"},
            [1],
        ),
    ],
    ids=[
        "date32",
        "date64",
        "timestamp-at-bound",
        "timestamp-offset-bound",
        "naive-timestamp-offset-bound",
        "naive-timestamp-naive-bound",
        "second-resolution-half-second-bound",
        "ns-timestamp-date-bound",
        "time32",
        "time64",
    ],
)
def test_temporal_columns_compare_with_iso_string_bounds(
    column: pa.Array, constraint: dict[str, Any], failing: list[int]
) -> None:
    outcome = _check([options("c", "timestamp", **constraint)], pa.table({"c": column}), "range")

    assert _failed_positions(outcome) == failing


@pytest.mark.parametrize(
    ("column", "constraint"),
    [
        (pa.array([dt.date(2026, 1, 1)], pa.date32()), {"minimum": "not a date"}),
        (pa.array([dt.date(2026, 1, 1)], pa.date32()), {"minimum": 5}),
        (pa.array([dt.time(9, 0)], pa.time64("us")), {"minimum": "25:99"}),
        (pa.array([dt.datetime(2026, 1, 1)], pa.timestamp("us")), {"minimum": "yesterday"}),
        (pa.array([1], pa.int64()), {"minimum": "abc"}),
        (pa.array([1], pa.int64()), {"minimum": True}),
        (pa.array([1.0], pa.float64()), {"maximum": "abc"}),
        (pa.array(["a"]), {"minimum": 1}),
        (pa.array([True]), {"minimum": 0}),
        (pa.array([[1]]), {"minimum": 0}),
    ],
)
def test_a_bound_that_cannot_be_compared_skips_the_check(
    column: pa.Array, constraint: dict[str, Any]
) -> None:
    outcome = _check([options("c", "timestamp", **constraint)], pa.table({"c": column}), "range")

    assert outcome.status == "skipped"
    assert outcome.skipped_reason


def test_integer_bounds_beyond_the_column_type_are_handled_exactly() -> None:
    table = pa.table({"n": pa.array([-128, 0, 127], pa.int8())})

    def failing(**constraint: Any) -> list[int]:
        outcome = _check([options("n", "integer", **constraint)], table, "range")
        return _failed_positions(outcome)

    assert failing(minimum=1000) == [0, 1, 2]
    assert failing(maximum=-1000) == [0, 1, 2]
    assert failing(minimum=-1000, maximum=1000) == []
    assert failing(minimum=-128, maximum=127) == []
    assert failing(exclusiveMinimum=127) == [0, 1, 2]
    assert failing(exclusiveMaximum=-128) == [0, 1, 2]
    unsigned = pa.table({"n": pa.array([0, 5], pa.uint8())})
    assert _check([options("n", "integer", minimum=-5)], unsigned, "range").status == "passed"
    assert _check([options("n", "integer", maximum=2**70)], unsigned, "range").status == "passed"


def test_nulls_and_nan_never_fail_a_range_and_empty_tables_pass() -> None:
    nan = float("nan")
    table = pa.table({"x": pa.array([None, nan, 5.0, None], pa.float64())})
    empty = pa.table({"x": pa.array([], pa.float64())})
    properties = [options("x", "number", minimum=1, maximum=10)]

    mixed = _check(properties, table, "range")
    none = _check(properties, empty, "range")

    assert (mixed.status, mixed.failed_rows, mixed.total_rows) == ("passed", 0, 4)
    assert (none.status, none.failed_rows, none.total_rows) == ("passed", 0, 0)


@pytest.mark.parametrize(
    ("column", "constraint"),
    [
        (pa.array([1.5], pa.float64()), {"minimum": 10**400}),
        (pa.array([dt.datetime(2026, 1, 1)], pa.timestamp("ns")), {"minimum": "1000-01-01"}),
        (pa.array([dt.time(9, 0)], pa.time64("us")), {"minimum": "09:00:00+02:00"}),
    ],
    ids=["float-overflow", "ns-out-of-bounds", "time-with-offset"],
)
def test_a_bound_outside_the_column_representation_skips_the_check(
    column: pa.Array, constraint: dict[str, Any]
) -> None:
    outcome = _check([options("c", "timestamp", **constraint)], pa.table({"c": column}), "range")

    assert outcome.status == "skipped"
    assert outcome.skipped_reason


def test_a_decimal_bound_with_a_positive_exponent_is_compared_exactly() -> None:
    column = pa.array([Decimal("10.50"), Decimal("99999999.99")], pa.decimal128(12, 2))

    table = pa.table({"d": column})

    above = _check([options("d", "number", minimum=1e16)], table, "range")
    below = _check([options("d", "number", maximum=1e16)], table, "range")

    assert _failed_positions(above) == [0, 1]
    assert below.status == "passed"


def test_range_on_a_string_column_is_skipped() -> None:
    outcome = _check([options("c", "number", minimum=1)], pa.table({"c": ["1"]}), "range")

    assert outcome.status == "skipped"


def test_a_range_with_no_bound_values_is_not_planned() -> None:
    the_plan = plan_properties([options("n", "integer", minimum=None)])

    assert "t.n.range" not in the_plan.order


def test_an_out_of_range_integer_format_value_is_a_warning_by_default() -> None:
    table = pa.table({"n": pa.array([2**31 - 1, 2**31], pa.int64())})

    outcome = _check([options("n", "integer", format="i32")], table, "format")

    assert outcome.status == "warning"
    assert _failed_positions(outcome) == [1]


def test_an_in_range_integer_format_column_passes_by_default() -> None:
    table = pa.table({"n": pa.array([-(2**31), 0, 2**31 - 1], pa.int64())})

    outcome = _check([options("n", "integer", format="i32")], table, "format")

    assert outcome.status == "passed"


def test_an_out_of_range_integer_format_value_fails_with_strict_types() -> None:
    table = pa.table({"n": pa.array([2**31], pa.int64())})

    outcome = _check([options("n", "integer", format="i32")], table, "format", strict_types=True)

    assert outcome.status == "failed"


@pytest.mark.parametrize(
    ("fmt", "values", "failing"),
    [
        ("i32", [2**31 - 1, 2**31, -(2**31), -(2**31) - 1], [1, 3]),
        ("i8", [127, 128, -128, -129], [1, 3]),
        ("u8", [0, 255, 256, -1], [2, 3]),
        ("u32", [2**32 - 1, 2**32], [1]),
        ("i64", [2**63 - 1], []),
        ("i128", [2**63 - 1], []),
    ],
)
def test_integer_format_is_a_value_range_with_strict_types(
    fmt: str, values: list[int], failing: list[int]
) -> None:
    table = pa.table({"n": pa.array(values, pa.int64())})

    outcome = _check([options("n", "integer", format=fmt)], table, "format", strict_types=True)

    assert _failed_positions(outcome) == failing


def test_unsigned_64_bit_values_are_checked_against_i64_with_strict_types() -> None:
    table = pa.table({"n": pa.array([2**63 - 1, 2**63], pa.uint64())})

    outcome = _check([options("n", "integer", format="i64")], table, "format", strict_types=True)

    assert _failed_positions(outcome) == [1]


def test_number_formats_follow_the_same_policy() -> None:
    values = pa.table({"x": pa.array([1e38, 1e39, -1e39], pa.float64())})
    lenient = run_properties([options("x", "number", format="f32")], values)["t.x.format"]
    strict = run_properties([options("x", "number", format="f32")], values, strict_types=True)[
        "t.x.format"
    ]
    f64 = plan_properties([options("x", "number", format="f64")], strict_types=True)
    odd = plan_properties([options("n", "integer", format="i7")], strict_types=True)
    odd_lenient = plan_properties([options("n", "integer", format="i7")])

    assert lenient.status == "warning"
    assert _failed_positions(lenient) == [1, 2]
    assert strict.status == "failed"
    assert _failed_positions(strict) == [1, 2]
    assert "t.x.format" not in f64.order
    assert f64.skipped == ()
    assert "'i7'" in odd.skipped[0].reason
    assert odd_lenient.skipped[0].reason == "integer format 'i7' has no value range to enforce"


# --- multipleOf -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "failing"),
    [
        ([12.34, 12.0, 0.07, 0.0], []),
        ([12.345, 12.34], [0]),
        ([0.1 + 0.2, 1.005], [1]),
        ([-12.34, -12.345], [1]),
    ],
)
def test_float_multiple_of_uses_a_relative_tolerance(
    values: list[float], failing: list[int]
) -> None:
    table = pa.table({"x": pa.array(values, pa.float64())})

    outcome = _check([options("x", "number", multipleOf=0.01)], table, "multiple_of")

    assert _failed_positions(outcome) == failing


@pytest.mark.parametrize(
    ("factor", "values", "failing"),
    [
        (1, [1.0 + 5e-10, 1.0 + 2e-9], [1]),
        (1, [1_000_000.0 + 5e-4, 1_000_000.0 + 2e-3], [1]),
        (0.5, [1.5, 1.7], [1]),
    ],
)
def test_the_float_tolerance_is_one_nano_relative(
    factor: float, values: list[float], failing: list[int]
) -> None:
    table = pa.table({"x": pa.array(values, pa.float64())})

    outcome = _check([options("x", "number", multipleOf=factor)], table, "multiple_of")

    assert _failed_positions(outcome) == failing


def test_float_multiple_of_fails_infinity_and_ignores_nan_and_null() -> None:
    table = pa.table({"x": pa.array([float("inf"), float("nan"), None, 4.0], pa.float64())})

    outcome = _check([options("x", "number", multipleOf=2)], table, "multiple_of")

    assert _failed_positions(outcome) == [0]


def test_float32_columns_are_compared_as_float64() -> None:
    table = pa.table({"x": pa.array([0.5, 1.5, 0.3], pa.float32())})

    outcome = _check([options("x", "number", multipleOf=0.5)], table, "multiple_of")

    assert _failed_positions(outcome) == [2]


@pytest.mark.parametrize(
    ("dtype", "factor", "values", "failing"),
    [
        (pa.int64(), 5, [10, -10, 0, 7, -7, 12], [3, 4, 5]),
        (pa.int8(), 5, [-125, 125, 126], [2]),
        (pa.int8(), 1000, [0, 1], [1]),
        (pa.int64(), 3, [2**53 + 1, 2**53 + 2, 2**53], [1, 2]),
        (pa.uint64(), 2, [2**64 - 2, 2**64 - 1], [1]),
        (pa.int64(), 5.0, [10, 11], [1]),
        (pa.int64(), 0.5, [1, 2], []),
        (pa.int64(), 0.3, [1], [0]),
    ],
)
def test_integer_multiple_of_is_exact(
    dtype: pa.DataType, factor: float, values: list[int], failing: list[int]
) -> None:
    table = pa.table({"n": pa.array(values, dtype)})

    outcome = _check([options("n", "integer", multipleOf=factor)], table, "multiple_of")

    assert _failed_positions(outcome) == failing


def test_decimal_multiple_of_is_exact() -> None:
    column = pa.array(
        [Decimal("12.340"), Decimal("12.345"), Decimal("0.010"), Decimal("1000000.010")],
        pa.decimal128(12, 3),
    )

    outcome = _check(
        [options("d", "number", multipleOf=0.01)], pa.table({"d": column}), "multiple_of"
    )
    quarter = _check(
        [options("d", "number", multipleOf=0.25)], pa.table({"d": column}), "multiple_of"
    )

    assert _failed_positions(outcome) == [1]
    assert _failed_positions(quarter) == [0, 1, 2, 3]


@pytest.mark.parametrize("factor", [0, -1, "x", True, float("inf")])
def test_an_invalid_factor_is_skipped_by_the_plan_and_the_kernel(factor: Any) -> None:
    the_plan = plan_properties([options("x", "number", multipleOf=factor)])
    result = kernels.multiple_of(pa.chunked_array([pa.array([1.0])]), factor)

    assert [entry.reason for entry in the_plan.skipped] == [
        "multipleOf must be a number greater than 0"
    ]
    assert result.skipped_reason == "multipleOf must be a number greater than 0"


def test_multiple_of_on_a_text_column_is_skipped() -> None:
    outcome = _check([options("c", "number", multipleOf=2)], pa.table({"c": ["a"]}), "multiple_of")

    assert outcome.status == "skipped"


# --- format -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "good", "bad"),
    [
        (
            "uuid",
            ["123e4567-e89b-12d3-a456-426614174000", "123E4567-E89B-12D3-A456-426614174000"],
            ["123e4567e89b12d3a456426614174000", "g23e4567-e89b-12d3-a456-426614174000", ""],
        ),
        (
            "email",
            ["a@example.com", "first.last+tag@sub.example.org"],
            ["a@b", "no-at.example.com", "a b@example.com", "a@example.com\n", "@example.com"],
        ),
        ("ipv4", ["10.0.0.1", "255.255.255.255"], ["256.0.0.1", "1.2.3", "01.2.3.4", "1.2.3.4/24"]),
        (
            "ipv6",
            ["::1", "2001:db8::ff00:42:8329"],
            ["12345::", "fe80::1%eth0", "1.2.3.4", "::g"],
        ),
    ],
)
def test_string_formats(fmt: str, good: list[str], bad: list[str]) -> None:
    table = pa.table({"c": [*good, *bad, None]})

    outcome = _check([options("c", "string", format=fmt)], table, "format")

    assert _failed_positions(outcome) == list(range(len(good), len(good) + len(bad)))


@pytest.mark.parametrize("fmt", ["uri", "hostname", "password", "byte"])
def test_other_string_formats_are_skipped_naming_the_format(fmt: str) -> None:
    the_plan = plan_properties([options("c", "string", format=fmt)])

    (entry,) = the_plan.skipped
    assert entry.reason == f"format {fmt!r} is not evaluated by this engine"


def test_a_textual_date_format_on_a_typed_date_column_is_not_applicable() -> None:
    table = pa.table({"d": pa.array([dt.date(2026, 1, 1)], pa.date32())})

    outcome = _check([options("d", "date", format="yyyy-MM-dd")], table, "format")

    assert outcome.status == "skipped"
    assert outcome.skipped_reason == "format 'yyyy-MM-dd' is not applicable to a date32[day] column"


def test_a_textual_date_format_is_checked_on_a_text_column() -> None:
    table = pa.table({"d": ["2026-11-21", "2026-13-45", None, "21/11/2026"]})

    outcome = _check([options("d", "date", format="yyyy-MM-dd")], table, "format")

    assert _failed_positions(outcome) == [1, 3]


def test_a_date_format_with_untranslatable_tokens_is_skipped_by_the_plan() -> None:
    the_plan = plan_properties([options("d", "date", format="MMM d, yyyy")])

    assert [entry.reason for entry in the_plan.skipped] == [
        "format 'MMM d, yyyy' is not evaluated by this engine"
    ]


def test_the_kernel_skips_an_unknown_format_on_text() -> None:
    result = kernels.format_is(pa.chunked_array([pa.array(["a"], pa.large_string())]), "MMM")

    assert result.skipped_reason == "format 'MMM' is not evaluated by this engine"


def test_a_format_that_is_not_text_or_has_no_logical_type_is_skipped() -> None:
    not_text = plan_properties([options("c", "string", format=5)])
    bare = plan_properties([prop("c", None, logicalTypeOptions={"format": "uuid"})])
    boolean = plan_properties([options("c", "boolean", format="uuid")])

    assert not_text.skipped[0].reason == "format is not a text value"
    assert "not evaluated" in bare.skipped[0].reason
    assert "not evaluated" in boolean.skipped[0].reason


# --- array items ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("constraint", "failing"),
    [
        ({"minItems": 2}, [0, 1]),
        ({"maxItems": 1}, [2, 3]),
        ({"minItems": 1, "maxItems": 2}, [1, 3]),
        ({"uniqueItems": True}, [2]),
        ({"uniqueItems": False}, []),
        ({"maxItems": 2, "uniqueItems": True}, [2, 3]),
    ],
)
@pytest.mark.parametrize(
    "list_type", [pa.list_(pa.string()), pa.large_list(pa.string())], ids=["list", "large_list"]
)
def test_array_bounds(
    constraint: dict[str, Any], failing: list[int], list_type: pa.DataType
) -> None:
    table = pa.table({"t": pa.array([["a"], [], ["a", "a"], ["x", "y", "z"], None], list_type)})

    outcome = _check([options("t", "array", **constraint)], table, "items")

    assert _failed_positions(outcome) == failing


def test_array_bounds_on_fixed_size_lists_and_chunked_columns() -> None:
    fixed = pa.table({"t": pa.array([["a", "a"], ["a", "b"]], pa.list_(pa.string(), 2))})
    chunked = pa.concat_tables(
        [pa.table({"t": pa.array([["a", "a"]])}), pa.table({"t": pa.array([["b"]])})]
    )

    on_fixed = _check([options("t", "array", uniqueItems=True)], fixed, "items")
    on_chunks = _check([options("t", "array", uniqueItems=True)], chunked, "items")

    assert _failed_positions(on_fixed) == [0]
    assert _failed_positions(on_chunks) == [0]


def test_unique_items_treat_repeated_nulls_as_duplicates() -> None:
    table = pa.table({"t": pa.array([[None, None], [None, 1]])})

    outcome = _check([options("t", "array", uniqueItems=True)], table, "items")

    assert _failed_positions(outcome) == [0]


def test_unique_items_of_nested_elements_are_skipped_not_passed() -> None:
    table = pa.table({"t": pa.array([[[1], [1]]])})

    outcome = _check([options("t", "array", uniqueItems=True)], table, "items")

    assert outcome.status == "skipped"


def test_array_bounds_on_a_scalar_column_are_skipped() -> None:
    outcome = _check([options("t", "array", maxItems=1)], pa.table({"t": [1]}), "items")

    assert outcome.status == "skipped"


def test_invalid_item_bounds_are_skipped_by_the_plan_and_the_kernel() -> None:
    the_plan = plan_properties([options("t", "array", maxItems="x")])
    odd_unique = plan_properties([options("t", "array", uniqueItems="yes")])
    result = kernels.array_bounds(pa.chunked_array([pa.array([[1]])]), max_items="x")

    assert the_plan.skipped[0].rule == "items"
    assert odd_unique.skipped[0].rule == "items"
    assert result.skipped_reason == "minItems and maxItems must be whole numbers"


def test_array_bounds_of_an_empty_table_pass() -> None:
    table = pa.table({"t": pa.array([], pa.list_(pa.string()))})

    outcome = _check([options("t", "array", maxItems=1, uniqueItems=True)], table, "items")

    assert (outcome.status, outcome.total_rows) == ("passed", 0)


# --- nulls, empty tables and the fixture -----------------------------------------------------


def test_nulls_fail_only_required_and_never_a_value_check() -> None:
    properties = [
        prop(
            "s",
            "string",
            required=True,
            enum=["a"],
            logicalTypeOptions={"pattern": "^a$", "minLength": 1, "maxLength": 1, "format": "uuid"},
        )
    ]
    table = pa.table({"s": pa.array([None, None], pa.string())})

    outcomes = run_properties(properties, table)

    by_status = {check_id: outcome.status for check_id, outcome in outcomes.items()}
    assert by_status["t.s.required"] == "failed"
    for rule in ("enum", "pattern", "length", "format"):
        assert by_status[f"t.s.{rule}"] == "passed", rule


def test_nan_counts_as_null_for_required_and_is_ignored_by_value_checks() -> None:
    properties = [prop("x", "number", required=True, logicalTypeOptions={"minimum": 0})]
    table = pa.table({"x": pa.array([float("nan"), 1.0])})

    outcomes = run_properties(properties, table)

    assert outcomes["t.x.required"].failed_rows == 1
    assert outcomes["t.x.range"].status == "passed"


def test_a_zero_row_fixture_passes_every_value_check_with_zero_total_rows() -> None:
    the_plan = plan(find_object(contract_doc()), PlanOptions())
    empty = orders_table().slice(0, 0)

    outcomes = ArrowBackend(empty).evaluate(the_plan.checks, RunOptions())

    judged = [outcome for outcome in outcomes if outcome.category != "quality"]
    assert {outcome.status for outcome in judged} == {"passed"}
    value_checks = [o for o in outcomes if o.rule in {"pattern", "range", "enum", "format"}]
    assert value_checks
    assert {o.total_rows for o in value_checks} == {0}


def test_zero_rows_still_run_presence_and_type_checks() -> None:
    table = pa.table({"n": pa.array([], pa.int64()), "s": pa.array([], pa.string())})
    properties = [
        options("n", "integer", minimum=1),
        prop("s", "integer"),
        prop("gone", "string"),
    ]

    outcomes = run_properties(properties, table)

    assert outcomes["t.s.type"].status == "failed"
    assert outcomes["t.gone.present"].status == "failed"
    assert outcomes["t.n.range"].status == "passed"
    assert outcomes["t.n.range"].total_rows == 0


def test_the_fixture_contract_evaluates_every_constraint_on_the_good_table() -> None:
    the_plan = plan(find_object(contract_doc()), PlanOptions())

    outcomes = ArrowBackend(orders_table(good=True)).evaluate(the_plan.checks, RunOptions())

    rules = {outcome.rule for outcome in outcomes}
    assert {"pattern", "format", "range", "multiple_of", "enum", "items", "unique"} <= rules
    assert {outcome.status for outcome in outcomes} == {"passed"}


def test_failure_samples_are_bounded_and_masked_for_sensitive_columns() -> None:
    properties = [
        prop(
            "email",
            "string",
            classification="restricted",
            logicalTypeOptions={"format": "email"},
        )
    ]
    table = pa.table({"email": ["bad1", "bad22", "bad333", "ok@example.com"]})

    outcome = _check(properties, table, "format", mask="auto")
    limited = run_properties(properties, table, run=RunOptions(max_failures=1))["t.email.format"]

    assert [s.value for s in outcome.samples] == [
        "<masked large_string len=4>",
        "<masked large_string len=5>",
        "<masked large_string len=6>",
    ]
    assert len(limited.samples) == 1


# --- property test: range kernel against a pure Python reference ------------------------------

_BOUND_KEYS = ("minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum")
_INT_BOUNDS = st.one_of(
    st.integers(-(10**6), 10**6),
    st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False).map(lambda v: round(v, 2)),
)
_CONSTRAINTS = st.dictionaries(st.sampled_from(_BOUND_KEYS), _INT_BOUNDS, min_size=1, max_size=4)


def _violates(value: Fraction | float, key: str, bound: Fraction | float) -> bool:
    return {
        "minimum": value < bound,
        "exclusiveMinimum": value <= bound,
        "maximum": value > bound,
        "exclusiveMaximum": value >= bound,
    }[key]


@settings(max_examples=150, deadline=None)
@given(
    values=st.lists(st.integers(-(10**6), 10**6), max_size=30),
    constraint=_CONSTRAINTS,
)
def test_integer_range_matches_exact_rational_reference(
    values: list[int], constraint: dict[str, Any]
) -> None:
    table = pa.table({"n": pa.array(values, pa.int64())})

    outcome = _check([options("n", "integer", **constraint)], table, "range")

    expected = sum(
        any(_violates(Fraction(v), key, Fraction(bound)) for key, bound in constraint.items())
        for v in values
    )
    assert outcome.failed_rows == expected


@settings(max_examples=150, deadline=None)
@given(
    values=st.lists(st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False), max_size=30),
    constraint=_CONSTRAINTS,
)
def test_float_range_matches_float64_reference(
    values: list[float], constraint: dict[str, Any]
) -> None:
    table = pa.table({"x": pa.array(values, pa.float64())})

    outcome = _check([options("x", "number", **constraint)], table, "range")

    expected = sum(
        any(_violates(v, key, float(bound)) for key, bound in constraint.items()) for v in values
    )
    assert outcome.failed_rows == expected
