"""Load a dataset (an in-memory object or a file) as an Arrow table for the engine.

``load_dataset`` is the one entry point: it recognises a path (by ``format`` or by suffix, or a
directory with a Delta log) and reads it, otherwise it converts the object. Nothing leaves the
caller's process (D-20); only the columns the checks need are read from Parquet and Delta.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pyarrow as pa

from contractum.errors import UsageError
from contractum.sources.ddl import ddl_to_schema
from contractum.sources.delta import read_delta
from contractum.sources.files import (
    FileRead,
    PathLike,
    read_csv,
    read_json_array,
    read_ndjson,
    read_parquet,
)
from contractum.sources.formats import FILE_FORMATS, format_of
from contractum.sources.frames import frame_kind, frame_to_arrow, type_name
from contractum.sources.schema_only import schema_of

__all__ = ["FILE_FORMATS", "LoadedDataset", "ddl_to_schema", "load_dataset", "schema_of"]


@dataclass(frozen=True)
class LoadedDataset:
    """A dataset ready for the engine.

    ``schema`` is the full source schema (also the columns that were not read), ``kind`` the
    format or object type, ``location`` the path as given or the object's type name,
    ``rows_total`` the source size when known, and ``text_columns`` the columns that hold text
    to be coerced to the contract's types. ``schema_only`` marks a dataset that is only a
    schema: ``table`` is then empty and no data was read.
    """

    table: pa.Table
    schema: pa.Schema
    kind: str
    location: str | None
    rows_total: int | None
    text_columns: frozenset[str] = frozenset()
    schema_only: bool = False


def load_dataset(
    data: object,
    *,
    format: str | None = None,
    columns: Iterable[str] | None = None,
    limit: int | None = None,
    ignore_case: bool = False,
    schema_only: bool = False,
    dialect: str | None = None,
    object_name: str | Sequence[str] | None = None,
) -> LoadedDataset:
    """Load ``data``: a path (file or Delta directory) or a pandas, polars or Arrow object.

    ``columns`` names the columns to read (Parquet and Delta read only those); ``limit`` stops
    Parquet, CSV, NDJSON and Delta after that many rows. With ``schema_only`` (always for a SQL
    DDL file) no row is read: the result carries the schema and an empty table. ``dialect`` and
    ``object_name`` select how a DDL file is parsed and which of its tables is taken.
    """
    if isinstance(data, (str, os.PathLike)):
        kind = format_of(data, format)
        if schema_only or kind == "ddl":
            return _schema_dataset(data, kind, dialect, object_name)
        return _load_path(data, kind, columns, limit, ignore_case)
    if format is not None:
        raise UsageError("--format applies to files, not to an in-memory dataset")
    if schema_only:
        schema = schema_of(data)
        return LoadedDataset(
            schema.empty_table(), schema, frame_kind(data), type_name(data), None, schema_only=True
        )
    table, text = frame_to_arrow(data)
    return LoadedDataset(
        table, table.schema, frame_kind(data), type_name(data), table.num_rows, text
    )


def _schema_dataset(
    data: PathLike, kind: str, dialect: str | None, object_name: str | Sequence[str] | None
) -> LoadedDataset:
    schema = schema_of(data, format=kind, dialect=dialect, object_name=object_name)
    return LoadedDataset(
        schema.empty_table(), schema, kind, os.fspath(data), None, schema_only=True
    )


def _load_path(
    data: PathLike,
    kind: str,
    columns: Iterable[str] | None,
    limit: int | None,
    ignore_case: bool,
) -> LoadedDataset:
    wanted = None if columns is None else list(columns)
    read = _read(kind, data, wanted, limit, ignore_case)
    return LoadedDataset(
        read.table, read.schema, kind, os.fspath(data), read.rows_total, read.text_columns
    )


def _read(
    kind: str,
    path: PathLike,
    columns: list[str] | None,
    limit: int | None,
    ignore_case: bool,
) -> FileRead:
    if kind == "parquet":
        return read_parquet(path, columns=columns, limit=limit, ignore_case=ignore_case)
    if kind == "delta":
        return read_delta(path, columns=columns, limit=limit, ignore_case=ignore_case)
    if kind in ("csv", "tsv"):
        return read_csv(path, delimiter="\t" if kind == "tsv" else ",", limit=limit)
    if kind == "ndjson":
        return read_ndjson(path, limit=limit)
    return read_json_array(path)
