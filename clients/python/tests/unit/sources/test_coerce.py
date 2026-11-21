"""Contract-driven coercion of text columns: parsing rules, failure counts and samples."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from support.datasets import write_csv
from support.engine import options, prop
from support.odcs import contract_doc

from contractum.odcs import SchemaObject, find_object
from contractum.sources.coerce import coerce_to_contract
from contractum.testing import TestOptions, run_test

UTC = UTC


def _obj(*properties: dict[str, Any]) -> SchemaObject:
    return find_object(contract_doc(schema=[{"name": "t", "properties": list(properties)}]))


def _text(values: Sequence[str | None], name: str = "c") -> pa.Table:
    return pa.table({name: pa.array(values, pa.large_string())})


def _coerce(
    values: Sequence[str | None], logical: str, **constraints: Any
) -> tuple[list[Any], int, tuple[int, ...], tuple[Any, ...]]:
    obj = _obj(options("c", logical, **constraints) if constraints else prop("c", logical))
    table, failures = coerce_to_contract(_text(values), obj, text_columns={"c"})
    failure = failures["c"]
    return table.column("c").to_pylist(), failure.count, failure.rows, failure.values


def test_integer_text_parses_and_counts_what_does_not() -> None:
    values, count, rows, raw = _coerce(
        ["12", "-3", "+7", "007", "12.5", "abc", None, "", " 5", "1_0"], "integer"
    )

    assert values == [12, -3, 7, 7, None, None, None, None, None, None]
    assert count == 5
    assert rows == (4, 5, 7, 8, 9)
    assert raw == ("12.5", "abc", "", " 5", "1_0")


def test_integer_text_beyond_int64_is_a_failure_not_a_crash() -> None:
    values, count, _, raw = _coerce(
        ["9223372036854775807", "-9223372036854775808", "9223372036854775808", "7"], "integer"
    )

    assert values == [9223372036854775807, -9223372036854775808, None, 7]
    assert count == 1
    assert raw == ("9223372036854775808",)


def test_number_text_accepts_exponent_and_bare_fraction() -> None:
    values, count, _, raw = _coerce(
        ["1e3", ".5", "5.", "-2", "1.5E-3", "+2.5", "abc", "nan", "inf", "1e999", "0x10"],
        "number",
    )

    assert values[:6] == [1000.0, 0.5, 5.0, -2.0, 0.0015, 2.5]
    assert values[6:] == [None] * 5
    assert count == 5
    assert raw == ("abc", "nan", "inf", "1e999", "0x10")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("Yes", True),
        ("1", True),
        ("false", False),
        ("False", False),
        ("NO", False),
        ("0", False),
    ],
)
def test_boolean_text_accepts_the_known_words_in_any_case(text: str, expected: bool) -> None:
    values, count, _, _ = _coerce([text], "boolean")

    assert values == [expected]
    assert count == 0


def test_boolean_text_rejects_everything_else() -> None:
    values, count, _, raw = _coerce(["maybe", "2", "y", None, "true"], "boolean")

    assert values == [None, None, None, None, True]
    assert count == 3
    assert raw == ("maybe", "2", "y")


def test_date_text_uses_iso_without_a_format() -> None:
    values, count, _, raw = _coerce(
        ["2026-11-21", "2026-02-31", "21/11/2026", "2026-1-5", None], "date"
    )

    assert values == [date(2026, 11, 21), None, None, None, None]
    assert count == 3
    assert raw == ("2026-02-31", "21/11/2026", "2026-1-5")


def test_date_text_uses_the_contract_format() -> None:
    values, count, _, raw = _coerce(
        ["21/11/2026", "2026-11-21", "31/02/2026"], "date", format="dd/MM/yyyy"
    )

    assert values == [date(2026, 11, 21), None, None]
    assert count == 2
    assert raw == ("2026-11-21", "31/02/2026")


def test_date_text_falls_back_to_iso_for_an_untranslatable_format() -> None:
    values, count, _, _ = _coerce(["2026-11-21"], "date", format="MMMM d, yyyy")

    assert values == [date(2026, 11, 21)]
    assert count == 0


def test_date_format_with_a_clock_keeps_the_day() -> None:
    values, count, _, _ = _coerce(["2026-11-21 23:59:59"], "date", format="yyyy-MM-dd HH:mm:ss")

    assert values == [date(2026, 11, 21)]
    assert count == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-21T09:30:00Z", datetime(2026, 11, 21, 9, 30, tzinfo=UTC)),
        ("2026-11-21T09:30:00+01:00", datetime(2026, 11, 21, 8, 30, tzinfo=UTC)),
        ("2026-11-21T09:30:00-0530", datetime(2026, 11, 21, 15, 0, tzinfo=UTC)),
        ("2026-11-21T09:30:00.123456Z", datetime(2026, 11, 21, 9, 30, 0, 123456, tzinfo=UTC)),
        ("2026-11-21T09:30:00.5Z", datetime(2026, 11, 21, 9, 30, 0, 500000, tzinfo=UTC)),
        ("2026-11-21T09:30:00.123456789Z", datetime(2026, 11, 21, 9, 30, 0, 123456, tzinfo=UTC)),
        ("2026-11-21 09:30:00", datetime(2026, 11, 21, 9, 30, tzinfo=UTC)),
        ("2026-11-21T09:30:00", datetime(2026, 11, 21, 9, 30, tzinfo=UTC)),
    ],
)
def test_timestamp_text_accepts_iso_8601_forms(text: str, expected: datetime) -> None:
    values, count, _, _ = _coerce([text], "timestamp")

    assert values == [expected]
    assert count == 0


def test_timestamp_text_rejects_bad_values_and_impossible_dates() -> None:
    values, count, rows, raw = _coerce(
        [
            "2026-11-21T09:30:00Z",
            "2026-02-31T09:30:00Z",
            "yesterday",
            "2026-11-21",
            "2026-13-01T00:00:00Z",
            None,
            "2026-11-21T09:30:00.1234567890Z",
        ],
        "timestamp",
    )

    assert values[0] == datetime(2026, 11, 21, 9, 30, tzinfo=UTC)
    assert values[1:] == [None] * 6
    assert count == 5
    assert rows == (1, 2, 3, 4, 6)
    assert raw[0] == "2026-02-31T09:30:00Z"


def test_time_text_parses_clock_values() -> None:
    values, count, _, raw = _coerce(["09:30:00", "25:00:00", "9:30", "23:59:59"], "time")

    assert values == [time(9, 30), None, None, time(23, 59, 59)]
    assert count == 2
    assert raw == ("25:00:00", "9:30")


def test_real_nulls_are_not_failures() -> None:
    _, count, rows, _ = _coerce([None, None], "integer")

    assert (count, rows) == (0, ())


def test_every_coerced_column_gets_an_entry_even_without_failures() -> None:
    obj = _obj(prop("a", "integer"), prop("b", "number"), prop("s", "string"), prop("o", "object"))
    table = pa.table({name: pa.array(["1"], pa.large_string()) for name in ("a", "b", "s", "o")})

    result, failures = coerce_to_contract(table, obj, text_columns={"a", "b", "s", "o"})

    assert sorted(failures) == ["a", "b"]
    assert failures["a"].count == failures["b"].count == 0
    assert result.schema.field("a").type == pa.int64()
    assert result.schema.field("b").type == pa.float64()
    assert result.schema.field("s").type == pa.large_string()
    assert result.schema.field("o").type == pa.large_string()


def test_a_column_that_is_not_text_or_not_in_the_contract_is_left_alone() -> None:
    obj = _obj(prop("a", "integer"))
    table = pa.table({"a": pa.array([1, 2]), "unknown": pa.array(["x", "y"], pa.large_string())})

    same, failures = coerce_to_contract(table, obj, text_columns={"unknown"})

    assert failures == {}
    assert same.equals(table)


def test_failure_samples_are_bounded_but_the_count_is_exact() -> None:
    obj = _obj(prop("c", "integer"))
    table = _text(["x"] * 50)

    _, failures = coerce_to_contract(table, obj, text_columns={"c"}, max_samples=3)

    assert failures["c"].count == 50
    assert failures["c"].rows == (0, 1, 2)
    assert failures["c"].values == ("x", "x", "x")


def test_column_names_match_the_physical_name_and_optionally_ignore_case() -> None:
    obj = _obj(prop("quantity", "integer", physicalName="QTY"))
    table = _text(["1", "x"], name="qty")

    exact, exact_failures = coerce_to_contract(table, obj, text_columns={"qty"})
    folded, folded_failures = coerce_to_contract(table, obj, text_columns={"qty"}, ignore_case=True)

    assert exact_failures == {}
    assert exact.schema.field("qty").type == pa.large_string()
    assert folded_failures["qty"].count == 1
    assert folded.column("qty").to_pylist() == [1, None]


def test_text_in_a_string_array_or_dictionary_column_is_coerced() -> None:
    obj = _obj(prop("c", "integer"))
    for column in (
        pa.array(["1", "x"], pa.string()),
        pa.array(["1", "x"], pa.large_string()).dictionary_encode(),
    ):
        table = pa.table({"c": column})

        result, failures = coerce_to_contract(table, obj, text_columns={"c"})

        assert result.column("c").to_pylist() == [1, None]
        assert failures["c"].count == 1


def test_a_table_without_rows_still_gets_typed_columns() -> None:
    obj = _obj(prop("c", "integer"))
    empty = pa.schema([pa.field("c", pa.large_string())]).empty_table()

    result, failures = coerce_to_contract(empty, obj, text_columns={"c"})

    assert result.num_rows == 0
    assert result.schema.field("c").type == pa.int64()
    assert failures["c"].count == 0


_REFERENCE_INTEGER = re.compile(r"[+-]?[0-9]+")


def _reference_valid(text: str) -> bool:
    return _REFERENCE_INTEGER.fullmatch(text) is not None and -(2**63) <= int(text) < 2**63


@settings(max_examples=150, deadline=None)
@given(
    st.lists(
        st.one_of(
            st.integers(min_value=-(2**70), max_value=2**70).map(str),
            st.text(alphabet="0123456789+-.eE x_", max_size=8),
            st.none(),
        ),
        max_size=25,
    )
)
def test_integer_failure_count_matches_a_pure_python_reference(
    values: list[str | None],
) -> None:
    obj = _obj(prop("c", "integer"))

    table, failures = coerce_to_contract(_text(values), obj, text_columns={"c"})

    expected = [int(v) if v is not None and _reference_valid(v) else None for v in values]
    assert table.column("c").to_pylist() == expected
    assert failures["c"].count == sum(
        1 for v in values if v is not None and not _reference_valid(v)
    )


def _csv_contract(**quantity: Any) -> dict[str, Any]:
    return contract_doc(
        schema=[
            {
                "name": "t",
                "properties": [
                    prop("id", "string"),
                    prop("quantity", "integer", **quantity),
                    prop("note", "string"),
                ],
            }
        ]
    )


def test_unparseable_csv_values_become_type_failures_with_counts_and_samples(
    tmp_path: Path,
) -> None:
    rows = [
        {"id": str(n), "quantity": "x" if n in (2, 5) else str(n), "note": "n"} for n in range(8)
    ]
    path = write_csv(tmp_path / "t.csv", rows)

    report = run_test(_csv_contract(), path)

    checks = {check.id: check for check in report.checks}
    type_check = checks["t.quantity.type"]
    assert type_check.status == "failed"
    assert type_check.failed_rows == 2
    assert type_check.total_rows == 8
    assert [(sample.row, sample.value) for sample in type_check.samples] == [
        (2, "x"),
        (5, "x"),
    ]
    assert report.exit_code() == 1


def test_a_masked_column_never_shows_its_unparseable_text(tmp_path: Path) -> None:
    secret = "CANARY-4242-VALUE"
    rows = [{"id": "1", "quantity": secret, "note": "n"}, {"id": "2", "quantity": "5", "note": "n"}]
    path = write_csv(tmp_path / "t.csv", rows)
    contract = _csv_contract(classification="restricted")
    contract["schema"][0]["properties"][1]["classification"] = "restricted"

    report = run_test(contract, path)

    assert secret not in report.model_dump_json()
    assert secret not in report.summary_text()
    samples = {c.id: c.samples for c in report.checks}["t.quantity.type"]
    assert [sample.value for sample in samples] == [f"<masked large_string len={len(secret)}>"]


def test_unparseable_values_in_a_sample_cite_their_source_rows(tmp_path: Path) -> None:
    rows = [{"id": str(n), "quantity": "x" if n == 7 else "1", "note": "n"} for n in range(20)]
    path = write_csv(tmp_path / "t.csv", rows)
    cited: set[int] = set()

    for seed in range(12):
        report = run_test(_csv_contract(), path, options=TestOptions(sample=19, seed=seed))
        check = {c.id: c for c in report.checks}["t.quantity.type"]
        assert report.dataset.rows_tested == 19
        if check.status == "failed":
            cited.update(sample.row for sample in check.samples)
        else:
            assert check.status == "passed"

    assert cited == {7}
