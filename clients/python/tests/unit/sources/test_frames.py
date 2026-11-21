"""In-memory dataset adapters: pandas, polars, pyarrow and Arrow C-stream objects."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pandas as pd
import polars as pl
import pyarrow as pa
import pytest
from support.datasets import arrow_table, orders_rows

from contractum.errors import DataLoadError, UnsupportedDatasetError
from contractum.sources import load_dataset
from contractum.sources.frames import frame_kind, frame_to_arrow
from contractum.testing import run_test


class _StreamOf:
    """Any object that exposes an Arrow C stream, like a database cursor or a dataframe."""

    def __init__(self, producer: Any) -> None:
        self._producer = producer

    def __arrow_c_stream__(self, requested_schema: Any = None) -> Any:
        return self._producer.__arrow_c_stream__(requested_schema)


class _BrokenStream:
    def __arrow_c_stream__(self, requested_schema: Any = None) -> Any:
        raise TypeError("SECRET-cursor-detail")


def test_pandas_frame_converts_without_the_index() -> None:
    frame = pd.DataFrame({"a": [1, 2], "b": ["x", None]}, index=[10, 20])

    table, text = frame_to_arrow(frame)

    assert table.column_names == ["a", "b"]
    assert table.column("a").to_pylist() == [1, 2]
    assert table.column("b").to_pylist() == ["x", None]
    assert text == frozenset()


def test_pandas_mixed_object_column_becomes_text_and_keeps_nulls() -> None:
    frame = pd.DataFrame({"ok": [1, 2, 3, 4], "mixed": [1, "x", None, 2.5]})

    table, text = frame_to_arrow(frame)

    assert text == {"mixed"}
    assert table.column("ok").to_pylist() == [1, 2, 3, 4]
    assert table.column("mixed").to_pylist() == ["1", "x", None, "2.5"]
    assert table.schema.field("mixed").type == pa.large_string()


def test_pandas_non_string_column_names_become_strings() -> None:
    frame = pd.DataFrame({1: [1, 2], 2: [1, "x"]})

    table, text = frame_to_arrow(frame)

    assert table.column_names == ["1", "2"]
    assert text == {"2"}


def test_pandas_categorical_and_timezone_columns_convert() -> None:
    frame = pd.DataFrame(
        {
            "c": pd.Categorical(["a", "b", "a"]),
            "t": pd.to_datetime(["2026-11-21T09:30:00Z"] * 3),
        }
    )

    table, _ = frame_to_arrow(frame)

    assert pa.types.is_dictionary(table.schema.field("c").type)
    assert pa.types.is_timestamp(table.schema.field("t").type)


def test_a_pandas_object_that_is_not_a_frame_is_unsupported() -> None:
    with pytest.raises(UnsupportedDatasetError) as raised:
        frame_to_arrow(pd.Series([1, 2]))

    assert "Series" in str(raised.value)
    assert "DataFrame" in (raised.value.hint or "")


def test_polars_dataframe_converts() -> None:
    table, text = frame_to_arrow(pl.DataFrame({"a": [1, 2], "s": ["x", None]}))

    assert table.column("a").to_pylist() == [1, 2]
    assert table.column("s").to_pylist() == ["x", None]
    assert text == frozenset()


def test_a_polars_lazy_frame_must_be_collected_first() -> None:
    with pytest.raises(UnsupportedDatasetError) as raised:
        frame_to_arrow(pl.DataFrame({"a": [1]}).lazy())

    assert "collect the LazyFrame first: lf.collect()" in (raised.value.hint or "")


def test_a_polars_series_is_unsupported() -> None:
    with pytest.raises(UnsupportedDatasetError) as raised:
        frame_to_arrow(pl.Series("a", [1, 2]))

    assert "polars" in str(raised.value)
    assert "DataFrame" in (raised.value.hint or "")


def test_a_polars_conversion_failure_names_only_the_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pl.DataFrame({"a": [1]})

    def fail(*args: Any, **kwargs: Any) -> None:
        raise ValueError("SECRET-value")

    monkeypatch.setattr(type(frame), "to_arrow", fail)

    with pytest.raises(DataLoadError) as raised:
        frame_to_arrow(frame)

    assert "ValueError" in str(raised.value)
    assert "SECRET" not in str(raised.value)


def test_pyarrow_table_record_batch_and_reader_convert() -> None:
    table = pa.table({"a": [1, 2, 3]})
    batch = pa.RecordBatch.from_pylist([{"a": 1}, {"a": 2}])
    reader = pa.RecordBatchReader.from_batches(table.schema, table.to_batches())

    assert frame_to_arrow(table)[0] is table
    assert frame_to_arrow(batch)[0].column("a").to_pylist() == [1, 2]
    assert frame_to_arrow(reader)[0].column("a").to_pylist() == [1, 2, 3]


def test_a_pyarrow_array_is_unsupported() -> None:
    with pytest.raises(UnsupportedDatasetError) as raised:
        frame_to_arrow(pa.array([1, 2]))

    assert "Array" in str(raised.value)


def test_a_failing_record_batch_reader_names_only_the_exception_type() -> None:
    def batches() -> Iterator[pa.RecordBatch]:
        yield pa.RecordBatch.from_pylist([{"a": 1}])
        raise ValueError("SECRET-batch")

    reader = pa.RecordBatchReader.from_batches(pa.schema([("a", pa.int64())]), batches())

    with pytest.raises(DataLoadError) as raised:
        frame_to_arrow(reader)

    assert "SECRET" not in str(raised.value)


def test_an_arrow_c_stream_object_converts() -> None:
    table, text = frame_to_arrow(_StreamOf(pa.table({"a": [1, 2]})))

    assert table.column("a").to_pylist() == [1, 2]
    assert text == frozenset()


def test_a_broken_arrow_c_stream_names_only_the_exception_type() -> None:
    with pytest.raises(DataLoadError) as raised:
        frame_to_arrow(_BrokenStream())

    assert "TypeError" in str(raised.value)
    assert "SECRET" not in str(raised.value)


@pytest.mark.parametrize("data", [[1, 2], {"a": 1}, 42, None, object()])
def test_anything_else_is_unsupported_and_named_by_type(data: object) -> None:
    with pytest.raises(UnsupportedDatasetError) as raised:
        frame_to_arrow(data)

    assert type(data).__name__ in str(raised.value)
    assert "pandas" in (raised.value.hint or "")


def test_frame_kind_names_the_source() -> None:
    assert frame_kind(pd.DataFrame()) == "pandas"
    assert frame_kind(pl.DataFrame()) == "polars"
    assert frame_kind(pa.table({})) == "arrow"
    assert frame_kind(_StreamOf(pa.table({}))) == "arrow-stream"


def test_load_dataset_reports_the_type_name_as_location() -> None:
    loaded = load_dataset(pd.DataFrame({"a": [1, 2]}))

    assert loaded.kind == "pandas"
    assert loaded.location == "pandas.DataFrame"
    assert loaded.rows_total == 2
    assert loaded.schema.names == ["a"]


def test_polars_string_view_data_gives_the_same_report_as_pyarrow() -> None:
    from support.datasets import flat_orders_contract

    rows = orders_rows(good=False, flat=True)
    frame = pl.from_arrow(arrow_table(rows))
    views = pa.table(_StreamOf(frame))
    assert any(pa.types.is_string_view(field.type) for field in views.schema)
    baseline = run_test(flat_orders_contract(), arrow_table(rows))

    for data in (_StreamOf(frame), views):
        report = run_test(flat_orders_contract(), data)
        assert [(c.id, c.status, c.failed_rows) for c in report.checks] == [
            (c.id, c.status, c.failed_rows) for c in baseline.checks
        ]
