"""Readers for Parquet, CSV/TSV, JSON-array and NDJSON files.

Every reader returns a ``FileRead``: the table, the full source schema (so a column that was not
read still counts as a column of the file), the source row count when it is known, and the names
of columns that hold text to be coerced to the contract's types (``sources.coerce``). A reader
error is re-raised as ``DataLoadError`` carrying only the path and the exception type, because
Arrow and JSON messages can contain data values.
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.json as pajson
import pyarrow.parquet as pq

from contractum.errors import DataLoadError, UnsupportedDatasetError

PathLike = str | os.PathLike[str]

_PARQUET_BATCH_ROWS = 1 << 16


@dataclass(frozen=True)
class FileRead:
    """What a reader produced.

    ``rows_total`` is None when the reader stopped early and the file size is unknown.
    """

    table: pa.Table
    schema: pa.Schema
    rows_total: int | None
    text_columns: frozenset[str] = frozenset()


@contextmanager
def reading(path: PathLike) -> Iterator[None]:
    """Turn a reader failure into a DataLoadError that names the file and the exception type."""
    try:
        yield
    except (OSError, ValueError, pa.ArrowException, RecursionError) as exc:
        raise DataLoadError(f"cannot read {os.fspath(path)}: {type(exc).__name__}") from None


def project_names(
    available: Sequence[str], wanted: Iterable[str] | None, ignore_case: bool = False
) -> list[str]:
    """The available columns to read, in file order: those wanted, or all when none are named."""
    if wanted is None:
        return list(available)
    fold = str.lower if ignore_case else str
    keep = {fold(name) for name in wanted}
    return [name for name in available if fold(name) in keep]


def read_parquet(
    path: PathLike,
    *,
    columns: Iterable[str] | None = None,
    limit: int | None = None,
    ignore_case: bool = False,
) -> FileRead:
    """Read a Parquet file, only the wanted columns and only the first ``limit`` rows.

    The row count comes from the footer; batches are read until ``limit`` rows are in hand.
    """
    with reading(path), pq.ParquetFile(path) as parquet:
        schema = parquet.schema_arrow
        by_name = {field.name: field for field in schema}
        names = project_names(schema.names, columns, ignore_case)
        projected = pa.schema([by_name[name] for name in names])
        batches = _parquet_batches(parquet, names, limit)
        table = pa.Table.from_batches(list(batches), schema=projected)
        if limit is not None:
            table = table.slice(0, limit)
        return FileRead(table, schema, int(parquet.metadata.num_rows))


def _parquet_batches(
    parquet: pq.ParquetFile, names: list[str], limit: int | None
) -> Iterator[pa.RecordBatch]:
    size = _PARQUET_BATCH_ROWS if limit is None else max(1, min(limit, _PARQUET_BATCH_ROWS))
    seen = 0
    for batch in parquet.iter_batches(batch_size=size, columns=names):
        yield batch
        seen += batch.num_rows
        if limit is not None and seen >= limit:
            return


def read_csv(path: PathLike, *, delimiter: str = ",", limit: int | None = None) -> FileRead:
    """Read a CSV or TSV file with every column as text; an empty field is null.

    The streaming reader stops once more than ``limit`` rows are in hand, so a huge file is not
    read to the end; ``rows_total`` is then None unless the file ended first.
    """
    with reading(path):
        with _csv_open(path, delimiter, None) as probe:
            names = list(probe.schema.names)
        types = {name: pa.large_string() for name in names}
        with _csv_open(path, delimiter, types) as reader:
            batches = list(_take_rows(iter(reader), limit))
        schema = pa.schema([pa.field(name, pa.large_string()) for name in names])
        table = pa.Table.from_batches(batches, schema=schema)
        exhausted = limit is None or table.num_rows <= limit
        total = table.num_rows if exhausted else None
        if limit is not None:
            table = table.slice(0, limit)
        return FileRead(table, schema, total, frozenset(names))


def _csv_open(
    path: PathLike, delimiter: str, column_types: dict[str, pa.DataType] | None
) -> pacsv.CSVStreamingReader:
    convert = pacsv.ConvertOptions(
        column_types=column_types or {}, strings_can_be_null=True, null_values=[""]
    )
    return pacsv.open_csv(
        os.fspath(path),
        read_options=pacsv.ReadOptions(encoding="utf-8-sig"),
        parse_options=pacsv.ParseOptions(delimiter=delimiter),
        convert_options=convert,
    )


def _take_rows(batches: Iterator[pa.RecordBatch], limit: int | None) -> Iterator[pa.RecordBatch]:
    """Batches until the rows exceed ``limit`` (one row of look-ahead tells the file went on)."""
    seen = 0
    for batch in batches:
        yield batch
        seen += batch.num_rows
        if limit is not None and seen > limit:
            return


def read_json_array(path: PathLike) -> FileRead:
    """Read a file holding one JSON array of objects (stdlib ``json``, so the whole file loads)."""
    with reading(path), open(path, encoding="utf-8-sig") as handle:
        document = json.load(handle)
    if not isinstance(document, list):
        raise UnsupportedDatasetError(
            f"{os.fspath(path)} is not a JSON array of objects",
            hint="for one JSON object per line use --format ndjson",
        )
    with reading(path):
        return _from_records(document)


def _from_records(records: list[Any]) -> FileRead:
    """A table from parsed JSON objects; a column of mixed types is kept as text."""
    if not all(isinstance(record, dict) for record in records):
        raise UnsupportedDatasetError("JSON data must be an array of objects")
    names = list(dict.fromkeys(key for record in records for key in record))
    arrays: list[pa.Array] = []
    text: set[str] = set()
    for name in names:
        values = [record.get(name) for record in records]
        array, as_text = _json_column(values)
        arrays.append(array)
        if as_text:
            text.add(name)
    table = pa.Table.from_arrays(arrays, names=names) if names else pa.table({})
    return FileRead(table, table.schema, len(records), frozenset(text))


def _json_column(values: list[Any]) -> tuple[pa.Array, bool]:
    """The column and whether it is text: strings stay text so the contract decides their type."""
    try:
        array = pa.array(values)
    except (pa.ArrowException, OverflowError):
        return pa.array([_as_text(value) for value in values], pa.large_string()), True
    return array, bool(pa.types.is_string(array.type) or pa.types.is_large_string(array.type))


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_ndjson(path: PathLike, *, limit: int | None = None) -> FileRead:
    """Read one JSON object per line with pyarrow; mixed-type columns fall back to stdlib json.

    With ``limit`` only the first lines are read; ``rows_total`` is then None unless the file
    ended first.
    """
    with reading(path):
        lines, exhausted = _head_lines(path, limit)
        read = _ndjson_table(lines)
    return FileRead(read.table, read.schema, len(lines) if exhausted else None, read.text_columns)


def _head_lines(path: PathLike, limit: int | None) -> tuple[list[bytes], bool]:
    """The non-blank lines up to ``limit`` and whether the file ended within them."""
    with open(path, "rb") as handle:
        content = (line for line in handle if line.strip())
        if limit is None:
            return list(content), True
        lines = list(islice(content, limit + 1))
    return lines[:limit], len(lines) <= limit


def _ndjson_table(lines: list[bytes]) -> FileRead:
    if not lines:
        return FileRead(pa.table({}), pa.schema([]), 0)
    payload = b"".join(_terminated(lines))
    try:
        table = _pyarrow_json(payload)
        temporal = [field.name for field in table.schema if _is_temporal(field.type)]
        if temporal:
            # Arrow turns date-like strings into timestamps; the contract decides what they are.
            keep_text = pa.schema([pa.field(name, pa.large_string()) for name in temporal])
            table = _pyarrow_json(payload, keep_text)
    except pa.ArrowInvalid:
        return _from_records([json.loads(line.decode("utf-8-sig")) for line in lines])
    return FileRead(table, table.schema, len(lines), _string_columns(table))


def _pyarrow_json(payload: bytes, schema: pa.Schema | None = None) -> pa.Table:
    parse = pajson.ParseOptions(explicit_schema=schema) if schema is not None else None
    return pajson.read_json(io.BytesIO(payload), parse_options=parse)


def _is_temporal(dtype: pa.DataType) -> bool:
    return bool(pa.types.is_timestamp(dtype) or pa.types.is_date(dtype) or pa.types.is_time(dtype))


def _terminated(lines: list[bytes]) -> Iterator[bytes]:
    for line in lines:
        yield line if line.endswith(b"\n") else line + b"\n"


def _string_columns(table: pa.Table) -> frozenset[str]:
    return frozenset(
        field.name
        for field in table.schema
        if pa.types.is_string(field.type) or pa.types.is_large_string(field.type)
    )
