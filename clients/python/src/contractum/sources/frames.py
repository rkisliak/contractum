"""In-memory datasets as Arrow tables: pandas, polars, pyarrow and Arrow C-stream objects.

pandas and polars are never imported here. An object is recognised by the top-level package of
its type, so the user's own installed version is the one that converts its data (D-20, D-35).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from contractum.errors import DataLoadError, UnsupportedDatasetError

_CONVERSION_ERRORS = (pa.ArrowException, TypeError, ValueError, OverflowError)


def _origin(obj: object) -> str:
    return type(obj).__module__.split(".")[0]


def type_name(obj: object) -> str:
    """The package and class of an object (``pandas.DataFrame``), never its content."""
    return f"{_origin(obj)}.{type(obj).__qualname__}"


def frame_kind(obj: object) -> str:
    """A short name of the dataset type for reports: pandas, polars, arrow or arrow-stream."""
    origin = _origin(obj)
    if origin in ("pandas", "polars"):
        return origin
    return "arrow" if origin == "pyarrow" else "arrow-stream"


def frame_to_arrow(obj: object) -> tuple[pa.Table, frozenset[str]]:
    """Convert a dataset object to an Arrow table plus the names of columns held as text.

    A pandas column pyarrow cannot convert (mixed Python objects) becomes a text column of
    ``str(value)`` so the contract decides what it is; polars lazy frames are refused.
    """
    origin = _origin(obj)
    if origin == "pandas":
        return _from_pandas(obj)
    if origin == "polars":
        return _from_polars(obj), frozenset()
    if origin == "pyarrow":
        return _from_pyarrow(obj), frozenset()
    if hasattr(obj, "__arrow_c_stream__"):
        return _from_stream(obj), frozenset()
    raise UnsupportedDatasetError(
        f"cannot test a {type_name(obj)}",
        hint="pass a pandas, polars or pyarrow object, an Arrow stream, or a file path",
    )


def _from_pandas(frame: Any) -> tuple[pa.Table, frozenset[str]]:
    if not hasattr(frame, "columns") or not hasattr(frame, "iloc"):
        raise UnsupportedDatasetError(
            f"cannot test a {type_name(frame)}", hint="pass a pandas DataFrame"
        )
    try:
        return pa.Table.from_pandas(frame, preserve_index=False), frozenset()
    except (pa.ArrowInvalid, pa.ArrowTypeError):
        return _pandas_by_column(frame)


def _pandas_by_column(frame: Any) -> tuple[pa.Table, frozenset[str]]:
    names = [str(name) for name in frame.columns]
    arrays: list[pa.Array] = []
    text: set[str] = set()
    for position, name in enumerate(names):
        series = frame.iloc[:, position]
        try:
            arrays.append(pa.Array.from_pandas(series))
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            arrays.append(_text_array(series))
            text.add(name)
    return pa.Table.from_arrays(arrays, names=names), frozenset(text)


def _text_array(series: Any) -> pa.Array:
    """``str(value)`` for every value of the series, nulls kept."""
    nulls = series.isna().tolist()
    values = series.tolist()
    return pa.array(
        [None if null else str(value) for value, null in zip(values, nulls, strict=True)],
        pa.large_string(),
    )


def _from_polars(frame: Any) -> pa.Table:
    if type(frame).__name__ == "LazyFrame":
        raise UnsupportedDatasetError(
            "polars LazyFrame cannot be tested directly",
            hint="collect the LazyFrame first: lf.collect()",
        )
    if type(frame).__name__ != "DataFrame":
        raise UnsupportedDatasetError(
            f"cannot test a {type_name(frame)}", hint="pass a polars DataFrame"
        )
    try:
        table: pa.Table = frame.to_arrow()
    except _CONVERSION_ERRORS as exc:
        raise DataLoadError(f"cannot convert the polars DataFrame: {type(exc).__name__}") from None
    return table


def _from_pyarrow(obj: Any) -> pa.Table:
    if isinstance(obj, pa.Table):
        return obj
    if isinstance(obj, pa.RecordBatch):
        return pa.Table.from_batches([obj])
    if isinstance(obj, pa.RecordBatchReader):
        return _read_all(obj)
    raise UnsupportedDatasetError(
        f"cannot test a {type_name(obj)}", hint="pass a pyarrow Table, RecordBatch or reader"
    )


def _from_stream(obj: Any) -> pa.Table:
    try:
        return pa.table(obj)
    except _CONVERSION_ERRORS as exc:
        raise DataLoadError(f"cannot read the Arrow stream: {type(exc).__name__}") from None


def _read_all(reader: pa.RecordBatchReader) -> pa.Table:
    try:
        return reader.read_all()
    except _CONVERSION_ERRORS as exc:
        raise DataLoadError(f"cannot read the Arrow stream: {type(exc).__name__}") from None
