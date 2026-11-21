"""Delta Lake tables (the optional ``delta`` extra)."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

import pyarrow as pa

from contractum.errors import DataLoadError, ExtraNotInstalledError
from contractum.sources.files import FileRead, PathLike, project_names


def is_delta_table(path: PathLike) -> bool:
    """Whether the directory holds a Delta transaction log."""
    return os.path.isdir(os.path.join(os.fspath(path), "_delta_log"))


def read_delta(
    path: PathLike,
    *,
    columns: Iterable[str] | None = None,
    limit: int | None = None,
    ignore_case: bool = False,
) -> FileRead:
    """Read the current version of a Delta table, only the wanted columns and the first rows.

    Needs ``deltalake`` (``pip install 'contractum[delta]'``); it is imported here and nowhere
    else, so a plain install never loads it.
    """
    table_class = _delta_table_class()
    try:
        dataset: Any = table_class(os.fspath(path)).to_pyarrow_dataset()
        schema: pa.Schema = dataset.schema
        names = project_names(schema.names, columns, ignore_case)
        total = int(dataset.count_rows())
        if limit is None:
            table = dataset.to_table(columns=names)
        else:
            table = dataset.head(limit, columns=names)
    except Exception as exc:  # deltalake raises its own error types; only the type name is kept
        raise DataLoadError(f"cannot read {os.fspath(path)}: {type(exc).__name__}") from None
    return FileRead(table, schema, total)


def delta_schema(path: PathLike) -> pa.Schema:
    """The Arrow schema of a Delta table from its log, without reading any data file."""
    table_class = _delta_table_class()
    try:
        schema = pa.schema(table_class(os.fspath(path)).schema().to_arrow())
    except Exception as exc:  # deltalake raises its own error types; only the type name is kept
        raise DataLoadError(f"cannot read {os.fspath(path)}: {type(exc).__name__}") from None
    return schema


def _delta_table_class() -> Any:
    try:
        from deltalake import DeltaTable
    except ImportError:
        raise ExtraNotInstalledError("delta", "reading a Delta table") from None
    return DeltaTable
