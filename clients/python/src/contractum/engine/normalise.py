"""Normalise a table before any kernel runs.

Polars hands over ``string_view`` and ``binary_view`` columns and pandas can hand over
dictionary-encoded ones; pyarrow compute has no kernel for the views and the checks would raise
``ArrowNotImplementedError``. Every string-like column becomes ``large_string``, binary views
become ``large_binary`` and dictionaries are decoded, including inside list and struct columns.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.types as pat


def _target(dtype: Any) -> Any:
    """The type a column is cast to; the type itself when it needs no change."""
    if pat.is_dictionary(dtype):
        return _target(dtype.value_type)
    if pat.is_string(dtype) or pat.is_large_string(dtype) or pat.is_string_view(dtype):
        return pa.large_string()
    if pat.is_binary_view(dtype):
        return pa.large_binary()
    if pat.is_list(dtype):
        return pa.list_(_target(dtype.value_type))
    if pat.is_large_list(dtype):
        return pa.large_list(_target(dtype.value_type))
    if pat.is_fixed_size_list(dtype):
        return pa.list_(_target(dtype.value_type), dtype.list_size)
    if pat.is_struct(dtype):
        return pa.struct(
            [
                dtype.field(index).with_type(_target(dtype.field(index).type))
                for index in range(dtype.num_fields)
            ]
        )
    return dtype


def normalise(table: pa.Table) -> pa.Table:
    """The same table with kernel-safe column types; unchanged columns are not copied."""
    columns = []
    changed = False
    for field, column in zip(table.schema, table.columns, strict=True):
        target = _target(field.type)
        if target != field.type:
            column = column.cast(target)
            changed = True
        columns.append(column)
    if not changed:
        return table
    return pa.table(columns, names=table.column_names)
