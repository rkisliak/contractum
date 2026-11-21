"""The schema of a dataset without its data (schema-only runs, DATA-05).

``schema_of`` reads only what describes the columns: an Arrow schema, a Parquet footer, a Delta
log, the dtypes of a pandas or polars frame (from at most its first rows) or a SQL DDL file.
No data page is read, so a huge file costs the same as a small one.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from contractum.errors import DataLoadError, UnsupportedDatasetError, UsageError
from contractum.sources.ddl import ddl_to_schema
from contractum.sources.delta import delta_schema
from contractum.sources.files import PathLike, reading
from contractum.sources.formats import format_of
from contractum.sources.frames import frame_kind, frame_to_arrow

# A frame's dtypes are read from this many rows: enough to type an object column, cheap to copy.
FRAME_SCHEMA_ROWS = 1000

_STREAM_ERRORS = (pa.ArrowException, TypeError, ValueError)


def schema_of(
    data: object,
    *,
    format: str | None = None,
    dialect: str | None = None,
    object_name: str | Sequence[str] | None = None,
) -> pa.Schema:
    """The Arrow schema of ``data``: a schema, table, frame, stream, file path or DDL file.

    ``dialect`` and ``object_name`` apply to SQL DDL only: the dialect to parse it with and the
    table (or tables, by any of the names given) to take when the file creates several.
    """
    if isinstance(data, (str, os.PathLike)):
        return _path_schema(data, format, dialect, object_name)
    if format is not None:
        raise UsageError("--format applies to files, not to an in-memory dataset")
    return _object_schema(data)


def _path_schema(
    path: PathLike,
    declared: str | None,
    dialect: str | None,
    object_name: str | Sequence[str] | None,
) -> pa.Schema:
    kind = format_of(path, declared)
    if kind == "ddl":
        return ddl_to_schema(_read_text(path), dialect=dialect, table=object_name)
    if kind == "parquet":
        with reading(path):
            return pq.read_schema(path)
    if kind == "delta":
        return delta_schema(path)
    raise UnsupportedDatasetError(
        f"a {kind} dataset has no schema that can be read without its data",
        hint="use Parquet, Delta, an Arrow object, a dataframe or a SQL DDL file, "
        "or leave out --schema-only",
    )


def _read_text(path: PathLike) -> str:
    with reading(path):
        return Path(path).read_text(encoding="utf-8")


def _object_schema(data: object) -> pa.Schema:
    if isinstance(data, pa.Schema):
        return data
    if isinstance(data, (pa.Table, pa.RecordBatch, pa.RecordBatchReader)):
        return data.schema
    kind = frame_kind(data)
    if kind == "arrow-stream" and hasattr(data, "__arrow_c_stream__"):
        return _stream_schema(data)
    table, _ = frame_to_arrow(_head(data) if kind in ("pandas", "polars") else data)
    return table.schema


def _head(frame: Any) -> Any:
    return frame.head(FRAME_SCHEMA_ROWS) if hasattr(frame, "head") else frame


def _stream_schema(stream: Any) -> pa.Schema:
    try:
        schema: pa.Schema = pa.RecordBatchReader.from_stream(stream).schema
    except _STREAM_ERRORS as exc:
        raise DataLoadError(f"cannot read the Arrow stream: {type(exc).__name__}") from None
    return schema


__all__ = ["FRAME_SCHEMA_ROWS", "schema_of"]
