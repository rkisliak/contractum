"""ArrowBackend kernels, the Backend protocol and the parse-failure contract."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.backend import Backend, ParseFailure, RunOptions
from contractum.engine.checks import (
    ColumnPresent,
    ExtraColumns,
    NotNull,
    PhysicalTypeIs,
    QualityMetric,
    TypeIs,
    Unique,
)

OPTS = RunOptions()


def _present(column: str, **fields: Any) -> ColumnPresent:
    return ColumnPresent(
        check_id=f"t.{column}.present", category="schema", rule="present", column=column, **fields
    )


def _type(column: str, logical: str, **fields: Any) -> TypeIs:
    return TypeIs(
        check_id=f"t.{column}.type",
        category="schema",
        rule="type",
        column=column,
        logical_type=logical,
        **fields,
    )


def _required(column: str, **fields: Any) -> NotNull:
    return NotNull(
        check_id=f"t.{column}.required",
        category="schema",
        rule="required",
        column=column,
        **fields,
    )


def test_arrow_backend_satisfies_the_backend_protocol() -> None:
    backend = ArrowBackend(pa.table({"a": [1]}))

    assert isinstance(backend, Backend)
    assert backend.name == "arrow"


def test_schema_lists_names_and_arrow_types() -> None:
    schema = ArrowBackend(pa.table({"a": [1], "b": ["x"]})).schema()

    assert schema.names == ("a", "b")
    assert [column.dtype for column in schema.columns] == ["int64", "string"]


def test_from_schema_evaluates_presence_and_type_on_an_empty_table() -> None:
    backend = ArrowBackend.from_schema(pa.schema([("a", pa.int64())]))

    outcomes = backend.evaluate(
        [_present("a"), _present("b"), _type("a", "integer"), _required("a")], OPTS
    )

    assert [o.status for o in outcomes] == ["passed", "failed", "passed", "passed"]
    assert backend.rows_tested == 0


def test_physical_name_is_the_dataset_column_else_the_name() -> None:
    backend = ArrowBackend(pa.table({"PHYS": [1], "plain": [2]}))

    renamed = backend.evaluate(
        [_present("logical", physical_name="PHYS"), _present("plain"), _present("PHYS")], OPTS
    )

    assert [o.status for o in renamed] == ["passed", "passed", "passed"]
    assert backend.evaluate([_present("logical")], OPTS)[0].status == "failed"


def test_column_names_are_case_sensitive_unless_ignore_case() -> None:
    backend = ArrowBackend(pa.table({"Order_ID": [1]}))

    assert backend.evaluate([_present("order_id")], OPTS)[0].status == "failed"
    folded = backend.evaluate(
        [_present("order_id"), _type("order_id", "integer")], RunOptions(ignore_case=True)
    )
    assert [o.status for o in folded] == ["passed", "passed"]


def test_missing_column_skips_type_and_required_with_a_reason() -> None:
    outcomes = ArrowBackend(pa.table({"a": [1]})).evaluate(
        [_type("gone", "string"), _required("gone")], OPTS
    )

    assert [o.status for o in outcomes] == ["skipped", "skipped"]
    assert all(o.skipped_reason for o in outcomes)


def test_type_mismatch_fails_with_observed_type_and_counts() -> None:
    table = pa.table({"q": ["1", "2"]})

    (outcome,) = ArrowBackend(table).evaluate([_type("q", "integer")], OPTS)

    assert outcome.status == "failed"
    assert (outcome.observed, outcome.threshold) == ("string", "integer")
    assert (outcome.failed_rows, outcome.total_rows) == (2, 2)
    assert outcome.message is not None
    assert "string" in outcome.message


def test_nulls_and_nan_fail_required_with_bounded_samples() -> None:
    nan = float("nan")
    table = pa.table({"x": pa.array([1.0, None, nan, 2.0, None, nan, None], pa.float64())})

    (outcome,) = ArrowBackend(table).evaluate([_required("x")], RunOptions(max_failures=3))

    assert outcome.status == "failed"
    assert (outcome.failed_rows, outcome.total_rows) == (5, 7)
    assert [(s.row, s.value) for s in outcome.samples] == [(1, None), (2, "nan"), (4, None)]


def test_samples_can_be_switched_off() -> None:
    table = pa.table({"x": [None, 1]})

    (outcome,) = ArrowBackend(table).evaluate([_required("x")], RunOptions(samples=False))

    assert outcome.status == "failed"
    assert outcome.samples == ()


def test_chunked_columns_and_row_index_map_samples_to_source_rows() -> None:
    table = pa.concat_tables([pa.table({"x": ["a", None]}), pa.table({"x": [None, "b"]})])

    (outcome,) = ArrowBackend(table, row_index=[10, 20, 30, 40], sampled=True).evaluate(
        [_required("x")], OPTS
    )

    assert [s.row for s in outcome.samples] == [20, 30]
    assert outcome.sampled is True


def test_empty_chunked_column_passes_required() -> None:
    table = pa.table({"x": pa.chunked_array([], pa.int64())})

    (outcome,) = ArrowBackend(table).evaluate([_required("x")], OPTS)

    assert outcome.status == "passed"
    assert outcome.total_rows == 0


def test_a_warning_severity_failure_is_a_warning() -> None:
    table = pa.table({"x": [None]})

    outcomes = ArrowBackend(table).evaluate(
        [_required("x", severity="warning"), _present("y", severity="warning")], OPTS
    )

    assert [o.status for o in outcomes] == ["warning", "warning"]
    assert [o.severity for o in outcomes] == ["warning", "warning"]


def test_masked_columns_hide_sample_values() -> None:
    table = pa.table({"email": pa.array([None, "a@example.com"])})
    parse = {"email": ParseFailure(1, rows=(1,), values=("a@example.com",))}

    secret = ArrowBackend(table, parse_failures=parse).evaluate(
        [_type("email", "integer", masked=True)], OPTS
    )

    assert secret[0].samples[0].value == "<masked large_string len=13>"


def test_text_coerced_column_type_uses_the_parse_failure_count_and_samples() -> None:
    table = pa.table({"n": pa.array([1, None, 3, None, None], pa.int64()), "ok": [1, 2, 3, 4, 5]})
    failures = {
        "n": ParseFailure(3, rows=(1, 3, 4), values=("x", "y", "z")),
        "ok": ParseFailure(0),
    }
    backend = ArrowBackend(table, parse_failures=failures)

    failed, passed = backend.evaluate(
        [_type("n", "integer"), _type("ok", "integer")], RunOptions(max_failures=2)
    )
    unsampled = backend.evaluate([_type("n", "integer")], RunOptions(samples=False))[0]

    assert failed.status == "failed"
    assert (failed.failed_rows, failed.total_rows) == (3, 5)
    assert [(s.row, s.value) for s in failed.samples] == [(1, "x"), (3, "y")]
    assert passed.status == "passed"
    assert passed.failed_rows == 0
    assert unsampled.samples == ()


def test_unsupported_check_types_are_skipped_never_passed() -> None:
    spec = QualityMetric(
        check_id="t.a.quality", category="quality", rule="quality", metric="nullValues"
    )

    (outcome,) = ArrowBackend(pa.table({"a": [1]})).evaluate([spec], OPTS)

    assert outcome.status == "skipped"
    assert outcome.skipped_reason is not None


def test_a_kernel_exception_becomes_an_error_with_only_the_type_name() -> None:
    class Boom(ArrowBackend):
        def _not_null(self, spec: Any, opts: RunOptions) -> Any:
            raise ValueError("secret value 123 in message")

    backend = Boom(pa.table({"a": [1]}))

    (outcome,) = backend.evaluate([_required("a")], OPTS)

    assert outcome.status == "error"
    assert outcome.message == "check could not be evaluated: ValueError"
    assert "secret" not in repr(outcome)


def test_extra_columns_follow_the_planned_severity() -> None:
    table = pa.table({"a": [1], "b": [2], "C": [3]})

    def spec(severity: str) -> ExtraColumns:
        return ExtraColumns(
            check_id="t.extra_columns",
            category="schema",
            rule="extra_columns",
            severity=severity,  # type: ignore[arg-type]
            expected=("a", "c"),
        )

    backend = ArrowBackend(table)
    warn = backend.evaluate([spec("warning")], OPTS)[0]
    fail = backend.evaluate([spec("error")], OPTS)[0]
    folded = backend.evaluate([spec("error")], RunOptions(ignore_case=True))[0]

    assert (warn.status, fail.status, folded.status) == ("warning", "failed", "failed")
    assert warn.message == "2 columns are not in the contract: b, C"
    assert folded.observed == 1


def test_extra_columns_pass_when_none_and_truncate_long_lists() -> None:
    wide = pa.table({f"c{i}": [i] for i in range(14)})
    spec = ExtraColumns(check_id="t.x", category="schema", rule="extra_columns", expected=("c0",))

    ok = ArrowBackend(pa.table({"c0": [1]})).evaluate([spec], OPTS)[0]
    many = ArrowBackend(wide).evaluate([spec], OPTS)[0]

    assert ok.status == "passed"
    assert many.message is not None
    assert many.message.startswith("13 columns are not in the contract: c1")
    assert many.message.endswith(", ...")


def test_unwrappable_sample_values_are_plain_text() -> None:
    import datetime

    table = pa.table({"d": pa.array([datetime.date(2026, 11, 21), None], pa.date32())})
    parse = {"d": ParseFailure(1, rows=(0,), values=(datetime.date(2026, 11, 21),))}

    (outcome,) = ArrowBackend(table, parse_failures=parse).evaluate([_type("d", "integer")], OPTS)

    assert outcome.samples[0].value == "2026-11-21"


def test_a_physical_type_that_is_not_comparable_is_skipped_by_the_backend_too() -> None:
    spec = PhysicalTypeIs(
        check_id="t.a.physical_type",
        category="schema",
        rule="physical_type",
        column="a",
        physical_type="GEOGRAPHY",
    )

    (outcome,) = ArrowBackend(pa.table({"a": ["x"]})).evaluate([spec], OPTS)

    assert outcome.status == "skipped"
    assert outcome.skipped_reason == "physicalType 'GEOGRAPHY' has no Arrow mapping"


def test_value_checks_on_a_missing_column_are_skipped_and_samples_can_be_off() -> None:
    gone = Unique(check_id="t.gone.unique", category="schema", rule="unique", column="gone")
    dupes = Unique(check_id="t.a.unique", category="schema", rule="unique", column="a")
    backend = ArrowBackend(pa.table({"a": [1, 1]}))

    missing, quiet = backend.evaluate([gone, dupes], RunOptions(samples=False))

    assert missing.status == "skipped"
    assert missing.skipped_reason == "column is missing from the dataset"
    assert (quiet.status, quiet.failed_rows, quiet.samples) == ("failed", 2, ())
