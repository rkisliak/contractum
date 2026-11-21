"""The Parquet, CSV, TSV, JSON-array and NDJSON readers and the load_dataset dispatch."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from support.datasets import write_csv

from contractum.errors import DataLoadError, UnsupportedDatasetError, UsageError
from contractum.sources import FILE_FORMATS, files, load_dataset
from contractum.sources.files import (
    project_names,
    read_csv,
    read_json_array,
    read_ndjson,
    read_parquet,
)

CANARY = "CANARY-SECRET-9931"


def _parquet(tmp_path: Path, rows: int = 10, **kwargs: Any) -> Path:
    table = pa.table(
        {"id": list(range(rows)), "name": [f"n{n}" for n in range(rows)], "Extra": [1] * rows}
    )
    path = tmp_path / "t.parquet"
    pq.write_table(table, path, **kwargs)
    return path


# --- Parquet ---------------------------------------------------------------------------------


def test_parquet_reads_only_the_wanted_columns_and_knows_the_total(tmp_path: Path) -> None:
    result = read_parquet(_parquet(tmp_path), columns=["id", "missing"])

    assert result.table.column_names == ["id"]
    assert result.schema.names == ["id", "name", "Extra"]
    assert result.rows_total == 10
    assert result.text_columns == frozenset()


def test_parquet_projection_can_ignore_case(tmp_path: Path) -> None:
    path = _parquet(tmp_path)

    exact = read_parquet(path, columns=["extra"])
    folded = read_parquet(path, columns=["extra"], ignore_case=True)

    assert exact.table.column_names == []
    assert folded.table.column_names == ["Extra"]


def test_parquet_without_matching_columns_keeps_the_row_count(tmp_path: Path) -> None:
    result = read_parquet(_parquet(tmp_path, rows=7), columns=["nope"])

    assert result.table.num_columns == 0
    assert result.table.num_rows == 7


def test_parquet_limit_stops_reading_early(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _parquet(tmp_path, rows=100)
    monkeypatch.setattr(files, "_PARQUET_BATCH_ROWS", 10)
    produced: list[int] = []
    original = pq.ParquetFile.iter_batches

    def counting(self: pq.ParquetFile, *args: Any, **kwargs: Any) -> Iterator[pa.RecordBatch]:
        for batch in original(self, *args, **kwargs):
            produced.append(batch.num_rows)
            yield batch

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", counting)

    result = read_parquet(path, limit=25)

    assert result.table.num_rows == 25
    assert result.table.column("id").to_pylist() == list(range(25))
    assert result.rows_total == 100
    assert len(produced) == 3


def test_parquet_limit_larger_than_the_file_reads_everything(tmp_path: Path) -> None:
    result = read_parquet(_parquet(tmp_path, rows=4), limit=1000)

    assert result.table.num_rows == 4


def test_parquet_nested_columns_are_projected_whole(tmp_path: Path) -> None:
    table = pa.table({"s": [{"a": 1}, {"a": 2}], "x": [1, 2]})
    path = tmp_path / "n.parquet"
    pq.write_table(table, path)

    result = read_parquet(path, columns=["s"])

    assert result.table.column_names == ["s"]
    assert result.table.column("s").to_pylist() == [{"a": 1}, {"a": 2}]


# --- CSV and TSV -----------------------------------------------------------------------------


def test_csv_reads_every_column_as_text_and_an_empty_field_is_null(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    path.write_text('id,name,n\n1,"a, b",\n2,,3.50\n', encoding="utf-8")

    result = read_csv(path)

    assert result.table.schema.types == [pa.large_string()] * 3
    assert result.table.to_pydict() == {
        "id": ["1", "2"],
        "name": ["a, b", None],
        "n": [None, "3.50"],
    }
    assert result.text_columns == {"id", "name", "n"}
    assert result.rows_total == 2


def test_tsv_reads_with_a_tab_delimiter(tmp_path: Path) -> None:
    path = tmp_path / "t.tsv"
    path.write_text("id\tname\n1\ta,b\n", encoding="utf-8")

    result = read_csv(path, delimiter="\t")

    assert result.table.to_pydict() == {"id": ["1"], "name": ["a,b"]}


def test_csv_with_a_byte_order_mark_keeps_the_first_column_name(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    path.write_bytes(b"\xef\xbb\xbfid,name\n1,x\n")

    assert read_csv(path).table.column_names == ["id", "name"]


def test_csv_limit_stops_after_n_rows_and_the_total_is_unknown(tmp_path: Path) -> None:
    rows = [{"id": str(n), "v": "x" * 20} for n in range(60_000)]
    path = write_csv(tmp_path / "big.csv", rows)

    limited = read_csv(path, limit=10)
    everything = read_csv(path, limit=1_000_000)

    assert limited.table.num_rows == 10
    assert limited.table.column("id").to_pylist() == [str(n) for n in range(10)]
    assert limited.rows_total is None
    assert everything.table.num_rows == 60_000
    assert everything.rows_total == 60_000


def test_csv_take_rows_does_not_pull_batches_past_the_limit() -> None:
    pulled: list[int] = []

    def batches() -> Iterator[pa.RecordBatch]:
        for index in range(100):
            pulled.append(index)
            yield pa.RecordBatch.from_pylist([{"a": "x"}] * 4)

    taken = list(files._take_rows(batches(), 10))

    assert sum(batch.num_rows for batch in taken) == 12
    assert len(pulled) == 3


def test_csv_limit_equal_to_the_row_count_knows_the_total(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    path.write_text("a\n1\n2\n3\n", encoding="utf-8")

    result = read_csv(path, limit=3)

    assert result.table.num_rows == 3
    assert result.rows_total == 3


def test_a_header_only_csv_has_columns_and_no_rows(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    path.write_text("a,b\n", encoding="utf-8")

    result = read_csv(path)

    assert result.table.column_names == ["a", "b"]
    assert result.table.num_rows == 0
    assert result.rows_total == 0


def test_an_empty_csv_file_is_a_load_error_naming_the_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises(DataLoadError) as raised:
        read_csv(path)

    assert str(path) in str(raised.value)


# --- JSON array ------------------------------------------------------------------------------


def test_json_array_loads_with_the_union_of_keys_in_first_seen_order(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text(json.dumps([{"a": 1, "b": "x"}, {"c": 2.5, "a": 2}]), encoding="utf-8")

    result = read_json_array(path)

    assert result.table.column_names == ["a", "b", "c"]
    assert result.table.to_pydict() == {"a": [1, 2], "b": ["x", None], "c": [None, 2.5]}
    assert result.rows_total == 2
    assert result.text_columns == {"b"}


def test_json_array_mixed_type_column_becomes_text(tmp_path: Path) -> None:
    rows = [{"a": 1}, {"a": "abc"}, {"a": 2.5}, {"a": True}, {"a": None}, {"a": [1, 2]}]
    path = tmp_path / "t.json"
    path.write_text(json.dumps(rows), encoding="utf-8")

    result = read_json_array(path)

    assert result.table.column("a").to_pylist() == ["1", "abc", "2.5", "true", None, "[1,2]"]
    assert result.table.schema.field("a").type == pa.large_string()
    assert result.text_columns == {"a"}


def test_json_array_integer_beyond_int64_falls_back_to_text(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text('[{"a": 1}, {"a": 123456789012345678901234567890}]', encoding="utf-8")

    result = read_json_array(path)

    assert result.table.column("a").to_pylist() == ["1", "123456789012345678901234567890"]
    assert result.text_columns == {"a"}


def test_json_array_keeps_nested_objects_and_lists(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text('[{"s": {"x": 1}, "l": [1, 2]}, {"s": {"x": 2}, "l": []}]', encoding="utf-8")

    result = read_json_array(path)

    assert result.table.column("s").to_pylist() == [{"x": 1}, {"x": 2}]
    assert result.table.column("l").to_pylist() == [[1, 2], []]
    assert result.text_columns == frozenset()


def test_an_empty_json_array_is_an_empty_table(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text("[]", encoding="utf-8")

    result = read_json_array(path)

    assert result.table.num_rows == 0
    assert result.table.num_columns == 0
    assert result.rows_total == 0


def test_a_json_document_that_is_not_an_array_is_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text('{"a": 1}', encoding="utf-8")

    with pytest.raises(UnsupportedDatasetError) as raised:
        read_json_array(path)

    assert "not a JSON array" in str(raised.value)
    assert "ndjson" in (raised.value.hint or "")


def test_a_json_array_of_non_objects_is_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(UnsupportedDatasetError):
        read_json_array(path)


# --- NDJSON ----------------------------------------------------------------------------------


def test_ndjson_loads_through_pyarrow_and_tracks_text_columns(tmp_path: Path) -> None:
    path = tmp_path / "t.ndjson"
    path.write_text('{"a": 1, "s": "x"}\n{"a": 2, "s": "y"}\n', encoding="utf-8")

    result = read_ndjson(path)

    assert result.table.to_pydict() == {"a": [1, 2], "s": ["x", "y"]}
    assert result.text_columns == {"s"}
    assert result.rows_total == 2


def test_ndjson_keeps_date_like_strings_as_text(tmp_path: Path) -> None:
    path = tmp_path / "t.ndjson"
    path.write_text(
        '{"d": "2026-11-21", "t": "2026-11-21T09:30:00Z"}\n'
        '{"d": "2026-11-22", "t": "2026-11-21T09:31:00+01:00"}\n',
        encoding="utf-8",
    )

    result = read_ndjson(path)

    assert result.table.schema.types == [pa.large_string(), pa.large_string()]
    assert result.table.column("d").to_pylist() == ["2026-11-21", "2026-11-22"]
    assert result.text_columns == {"d", "t"}


def test_ndjson_mixed_type_column_falls_back_to_the_stdlib_path(tmp_path: Path) -> None:
    path = tmp_path / "t.ndjson"
    path.write_text('{"a": 1}\n{"a": "abc"}\n{"a": 2.5}\n', encoding="utf-8")

    result = read_ndjson(path)

    assert result.table.column("a").to_pylist() == ["1", "abc", "2.5"]
    assert result.text_columns == {"a"}
    assert result.rows_total == 3


def test_ndjson_ignores_blank_lines_crlf_and_a_missing_final_newline(tmp_path: Path) -> None:
    path = tmp_path / "t.ndjson"
    path.write_bytes(b'{"a": 1}\r\n\r\n   \n{"a": 2}')

    result = read_ndjson(path)

    assert result.table.column("a").to_pylist() == [1, 2]
    assert result.rows_total == 2


def test_ndjson_limit_reads_n_lines_and_the_total_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "t.ndjson"
    path.write_text("".join(f'{{"a": {n}}}\n' for n in range(50)), encoding="utf-8")

    limited = read_ndjson(path, limit=5)
    exact = read_ndjson(path, limit=50)

    assert limited.table.column("a").to_pylist() == [0, 1, 2, 3, 4]
    assert limited.rows_total is None
    assert exact.table.num_rows == 50
    assert exact.rows_total == 50


def test_an_empty_ndjson_file_is_an_empty_table(tmp_path: Path) -> None:
    path = tmp_path / "t.ndjson"
    path.write_text("\n\n", encoding="utf-8")

    result = read_ndjson(path)

    assert result.table.num_rows == 0
    assert result.rows_total == 0


def test_an_invalid_ndjson_line_is_a_load_error(tmp_path: Path) -> None:
    path = tmp_path / "t.ndjson"
    path.write_text('{"a": 1}\n{"a": \n', encoding="utf-8")

    with pytest.raises(DataLoadError) as raised:
        read_ndjson(path)

    assert str(path) in str(raised.value)


def test_an_ndjson_line_that_is_not_an_object_is_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "t.ndjson"
    path.write_text("[1, 2]\n[3, 4]\n", encoding="utf-8")

    with pytest.raises(UnsupportedDatasetError):
        read_ndjson(path)


# --- Errors never carry data -----------------------------------------------------------------


@pytest.mark.parametrize("error", [ValueError(CANARY), OSError(CANARY), pa.ArrowInvalid(CANARY)])
def test_reader_failures_name_the_path_and_exception_type_but_not_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    path = _parquet(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(pq, "ParquetFile", fail)

    with pytest.raises(DataLoadError) as raised:
        read_parquet(path)

    message = str(raised.value)
    assert str(path) in message
    assert type(error).__name__ in message
    assert CANARY not in message
    assert raised.value.__cause__ is None


def test_a_missing_file_is_a_load_error_with_the_path(tmp_path: Path) -> None:
    path = tmp_path / "gone.parquet"

    with pytest.raises(DataLoadError) as raised:
        read_parquet(path)

    assert str(path) in str(raised.value)
    assert "FileNotFoundError" in str(raised.value)


def test_a_file_that_is_not_parquet_is_a_load_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.parquet"
    path.write_text(CANARY, encoding="utf-8")

    with pytest.raises(DataLoadError) as raised:
        load_dataset(path)

    assert CANARY not in str(raised.value)


# --- project_names and the load_dataset dispatch ---------------------------------------------


def test_project_names_keeps_file_order_and_everything_without_a_wanted_list() -> None:
    assert project_names(["b", "A", "c"], ["c", "a"]) == ["c"]
    assert project_names(["b", "A", "c"], ["c", "a"], ignore_case=True) == ["A", "c"]
    assert project_names(["b", "A"], None) == ["b", "A"]


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("t.parquet", "parquet"),
        ("t.PQ", "parquet"),
        ("t.csv", "csv"),
        ("t.tsv", "tsv"),
        ("t.tab", "tsv"),
        ("t.json", "json"),
        ("t.ndjson", "ndjson"),
        ("t.jsonl", "ndjson"),
    ],
)
def test_the_format_is_taken_from_the_suffix(tmp_path: Path, name: str, kind: str) -> None:
    path = tmp_path / name
    if kind == "parquet":
        pq.write_table(pa.table({"a": [1]}), path)
    elif kind in ("csv", "tsv"):
        path.write_text("a\n1\n", encoding="utf-8")
    elif kind == "json":
        path.write_text('[{"a": 1}]', encoding="utf-8")
    else:
        path.write_text('{"a": 1}\n', encoding="utf-8")

    loaded = load_dataset(path)

    assert loaded.kind == kind
    assert loaded.location == str(path)
    assert loaded.table.num_rows == 1


def test_an_explicit_format_overrides_the_suffix(tmp_path: Path) -> None:
    path = tmp_path / "data.txt"
    path.write_text("a;b\n1;2\n", encoding="utf-8")
    other = tmp_path / "t.dat"
    other.write_text("a\tb\n1\t2\n", encoding="utf-8")

    loaded = load_dataset(other, format="tsv")

    assert loaded.kind == "tsv"
    assert loaded.table.column_names == ["a", "b"]
    with pytest.raises(UnsupportedDatasetError):
        load_dataset(path)


def test_an_unknown_suffix_asks_for_a_format(tmp_path: Path) -> None:
    path = tmp_path / "data.xyz"
    path.write_text("a", encoding="utf-8")

    with pytest.raises(UnsupportedDatasetError) as raised:
        load_dataset(path)

    assert "--format" in (raised.value.hint or "")


def test_an_unknown_format_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError) as raised:
        load_dataset(tmp_path / "t.csv", format="xlsx")

    assert "xlsx" in str(raised.value)
    assert all(name in (raised.value.hint or "") for name in FILE_FORMATS)


def test_a_directory_that_is_not_a_delta_table_is_unsupported(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedDatasetError):
        load_dataset(tmp_path)


def test_load_dataset_accepts_a_plain_string_path(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    path.write_text("a\n1\n", encoding="utf-8")

    assert load_dataset(str(path)).table.to_pydict() == {"a": ["1"]}


def test_a_format_makes_no_sense_for_an_in_memory_dataset() -> None:
    with pytest.raises(UsageError):
        load_dataset(pa.table({"a": [1]}), format="csv")
