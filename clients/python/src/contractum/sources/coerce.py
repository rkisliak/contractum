"""Contract-driven coercion of text columns (CSV, TSV, JSON) to the contract's logical types.

A text column is read as text so that one bad value cannot stop the whole file from loading. Each
value is first matched by a linear RE2 pattern so Arrow only ever casts values that look right
(``pyarrow.compute.cast`` fails the whole array on the first bad value), then cast; a value that
does not parse becomes null in the typed column and is counted in a ``ParseFailure``. The raw
failing text stays inside the engine, which masks it by the column's classification.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from contractum.engine.backend import ParseFailure
from contractum.engine.types import jdk_to_strptime
from contractum.odcs import Property, SchemaObject

_INTEGER = r"^[+-]?[0-9]+$"
_NUMBER = r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$"
_ISO_DATE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
_ISO_TIMESTAMP = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?"
    r"(Z|[+-][0-9]{2}(:?[0-9]{2})?)?$"
)
_ZONE = r"(Z|[+-][0-9]{2}(:?[0-9]{2})?)$"
_CLOCK = r"^[0-9]{2}:[0-9]{2}:[0-9]{2}$"
_FRACTION_BEYOND_MICROSECONDS = r"(\.[0-9]{6})[0-9]+"

_TRUE_WORDS = ("true", "1", "yes")
_FALSE_WORDS = ("false", "0", "no")

DEFAULT_MAX_SAMPLES = 20

Converter = Callable[[pa.Array, Property], pa.Array]


def _only_matching(values: pa.Array, regex: str) -> pa.Array:
    """The values that match ``regex``; every other value becomes null."""
    return pc.if_else(pc.match_substring_regex(values, regex), values, pa.scalar(None, values.type))


def _cast_one(text: str, target: pa.DataType) -> pa.Array:
    try:
        return pc.cast(pa.array([text], pa.large_string()), target)
    except pa.ArrowInvalid:
        return pa.nulls(1, target)


def _cast_distinct(candidate: pa.Array, target: pa.DataType) -> pa.Array:
    """Cast each distinct value alone (the array held one that cannot be cast) and map back."""
    distinct = pc.unique(candidate.drop_null())
    pieces = [_cast_one(text, target) for text in distinct.to_pylist()]
    lookup = pa.concat_arrays(pieces) if pieces else pa.nulls(0, target)
    return pc.take(lookup, pc.index_in(candidate, value_set=distinct))


def _safe_cast(candidate: pa.Array, target: pa.DataType) -> pa.Array:
    """Cast text to ``target``; a value Arrow cannot cast becomes null instead of an error."""
    try:
        return pc.cast(candidate, target)
    except pa.ArrowInvalid:
        return _cast_distinct(candidate, target)


def _integer(values: pa.Array, prop: Property) -> pa.Array:
    candidate = pc.replace_substring_regex(_only_matching(values, _INTEGER), r"^\+", "")
    return _safe_cast(candidate, pa.int64())


def _number(values: pa.Array, prop: Property) -> pa.Array:
    parsed = _safe_cast(_only_matching(values, _NUMBER), pa.float64())
    return pc.if_else(pc.is_inf(parsed), pa.scalar(None, pa.float64()), parsed)


def _boolean(values: pa.Array, prop: Property) -> pa.Array:
    lowered = pc.utf8_lower(values)
    known = pc.is_in(lowered, value_set=pa.array(_TRUE_WORDS + _FALSE_WORDS, values.type))
    truth = pc.is_in(lowered, value_set=pa.array(_TRUE_WORDS, values.type))
    return pc.if_else(known, truth, pa.scalar(None, pa.bool_()))


def _parse_day(text: str, pattern: str) -> date | None:
    try:
        return datetime.strptime(text, pattern).date()
    except ValueError:
        return None


def _dates_by_pattern(values: pa.Array, pattern: str) -> pa.Array:
    """Parse each distinct text with the contract's pattern, so an impossible day such as
    31/02 is a failure (Arrow's own strptime rolls it over into the next month).

    A column has at most a few tens of thousands of distinct calendar days, so the per-value
    Python parse stays cheap.
    """
    distinct = pc.unique(values.drop_null())
    lookup = pa.array([_parse_day(text, pattern) for text in distinct.to_pylist()], pa.date32())
    return pc.take(lookup, pc.index_in(values, value_set=distinct))


def _date(values: pa.Array, prop: Property) -> pa.Array:
    declared = prop.options.get("format")
    pattern = jdk_to_strptime(declared) if isinstance(declared, str) else None
    if pattern is not None:
        return _dates_by_pattern(values, pattern)
    return _safe_cast(_only_matching(values, _ISO_DATE), pa.date32())


def _timestamp(values: pa.Array, prop: Property) -> pa.Array:
    candidate = _only_matching(values, _ISO_TIMESTAMP)
    trimmed = pc.replace_substring_regex(candidate, _FRACTION_BEYOND_MICROSECONDS, r"\1")
    utc = pc.binary_join_element_wise(
        trimmed, pa.scalar("Z", trimmed.type), pa.scalar("", trimmed.type)
    )
    zoned = pc.if_else(pc.match_substring_regex(trimmed, _ZONE), trimmed, utc)
    return _safe_cast(zoned, pa.timestamp("us", tz="UTC"))


def _time(values: pa.Array, prop: Property) -> pa.Array:
    parsed = pc.strptime(_only_matching(values, _CLOCK), "%H:%M:%S", "us", error_is_null=True)
    return pc.cast(parsed, pa.time64("us"))


_CONVERTERS: dict[str, Converter] = {
    "integer": _integer,
    "number": _number,
    "boolean": _boolean,
    "date": _date,
    "timestamp": _timestamp,
    "time": _time,
}


def _lookup(obj: SchemaObject, ignore_case: bool) -> dict[str, Property]:
    fold = str.lower if ignore_case else str
    found: dict[str, Property] = {}
    for prop in obj.properties:
        found.setdefault(fold(prop.source_name), prop)
    return found


def _failure(text: pa.Array, typed: pa.Array, max_samples: int) -> ParseFailure:
    """The values that were text but did not parse (valid text, null result)."""
    failed = pc.and_(pc.is_valid(text), pc.is_null(typed))
    positions = pc.indices_nonzero(failed)
    head = positions[:max_samples]
    return ParseFailure(
        len(positions), tuple(head.to_pylist()), tuple(pc.take(text, head).to_pylist())
    )


def _target(
    name: str, props: Mapping[str, Property], fold: Callable[[str], str]
) -> tuple[Property, Converter] | None:
    prop = props.get(fold(name))
    convert = _CONVERTERS.get(prop.logical_type or "") if prop is not None else None
    return (prop, convert) if prop is not None and convert is not None else None


def coerce_to_contract(
    table: pa.Table,
    obj: SchemaObject,
    *,
    text_columns: Iterable[str],
    ignore_case: bool = False,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> tuple[pa.Table, dict[str, ParseFailure]]:
    """Coerce the text columns the contract types as integer, number, boolean, date, timestamp
    or time; returns the table and one ParseFailure per coerced column (count may be 0).

    A column the contract types as string, array or object, and a column the contract does not
    know, stays text. ``max_samples`` bounds the raw failing values kept per column.
    """
    wanted = frozenset(text_columns)
    props = _lookup(obj, ignore_case)
    fold = str.lower if ignore_case else str
    failures: dict[str, ParseFailure] = {}
    for position, name in enumerate(table.column_names):
        target = _target(name, props, fold) if name in wanted else None
        if target is None:
            continue
        prop, convert = target
        text = _as_text(table.column(position))
        typed = convert(text, prop)
        failures[name] = _failure(text, typed, max_samples)
        table = table.set_column(position, name, pa.chunked_array([typed]))
    return table, failures


def _as_text(column: Any) -> pa.Array:
    """The column as one large_string array (string, large_string and dictionary text)."""
    return pc.cast(column, pa.large_string()).combine_chunks()
