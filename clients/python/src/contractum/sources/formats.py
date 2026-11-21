"""Which format a dataset path holds: the declared format, a Delta directory or the suffix."""

from __future__ import annotations

import os
from pathlib import Path

from contractum.errors import UnsupportedDatasetError, UsageError
from contractum.sources.delta import is_delta_table

FILE_FORMATS = ("parquet", "csv", "tsv", "json", "ndjson", "delta", "ddl")

_SUFFIX_FORMATS = {
    ".parquet": "parquet",
    ".pq": "parquet",
    ".csv": "csv",
    ".tsv": "tsv",
    ".tab": "tsv",
    ".json": "json",
    ".ndjson": "ndjson",
    ".jsonl": "ndjson",
    ".sql": "ddl",
    ".ddl": "ddl",
}


def format_of(path: str | os.PathLike[str], declared: str | None) -> str:
    """The format of a dataset path; ``declared`` wins, then a Delta log, then the suffix."""
    if declared is not None:
        if declared not in FILE_FORMATS:
            raise UsageError(f"unknown format {declared!r}", hint="use " + ", ".join(FILE_FORMATS))
        return declared
    where = Path(path)
    if where.is_dir():
        if is_delta_table(where):
            return "delta"
        raise UnsupportedDatasetError(f"{os.fspath(path)} is a directory but not a Delta table")
    kind = _SUFFIX_FORMATS.get(where.suffix.lower())
    if kind is None:
        raise UnsupportedDatasetError(
            f"cannot tell the format of {os.fspath(path)}",
            hint="pass --format " + "|".join(FILE_FORMATS),
        )
    return kind
