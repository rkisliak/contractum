"""One small kernel per value check, evaluated on pyarrow columns.

A kernel takes a column (or the key columns) plus the contract's parameters and returns a
``KernelResult``: a boolean failure mask (true where a row breaks the rule, never null), the counts
and a message. Nulls and NaN are ignored by every value check; they only fail ``required``. A
kernel that cannot judge a column type returns a skipped result with the reason, never a pass.
Regular expressions run on RE2 through pyarrow only (see ``regex``).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.types as pat

from contractum.engine.regex import UNSUPPORTED_PATTERN, re2_supported
from contractum.engine.types import (
    is_binary,
    is_list,
    is_string,
    jdk_to_strptime,
    real_number,
    whole_number,
)

_MAX_LISTED_VALUES = 20
_WIDE = Context(prec=200)
_NO_VALUE: Any = object()


@dataclass(frozen=True)
class KernelResult:
    """The verdict of one kernel.

    ``failed_mask`` is a boolean array with one entry per row (true = the row fails), or None for
    a skipped result. ``skipped_reason`` is set when the kernel could not judge the column.
    """

    failed_mask: Any = None
    failed_rows: int = 0
    total_rows: int = 0
    message: str | None = None
    observed: Any = None
    threshold: Any = None
    skipped_reason: str | None = None


def skipped(reason: str) -> KernelResult:
    """A result for a check the kernel cannot judge on this column."""
    return KernelResult(skipped_reason=reason)


def _count(mask: Any) -> int:
    return int(pc.sum(mask).as_py() or 0)


def _result(mask: Any, what: str, *, threshold: Any = None, observed: Any = None) -> KernelResult:
    failed = _count(mask)
    total = len(mask)
    message = f"{failed} of {total} {what}" if failed else None
    return KernelResult(mask, failed, total, message, observed, threshold)


def _positions(count: int) -> Any:
    """The row positions 0..count-1 as an unsigned integer array."""
    return pc.indices_nonzero(pa.repeat(True, count))


def _valid(col: Any) -> Any:
    """Rows that hold a value: not null and, for floats, not NaN."""
    valid = pc.is_valid(col)
    if pat.is_floating(col.type):
        valid = pc.and_(valid, pc.invert(pc.fill_null(pc.is_nan(col), False)))
    return valid


def _failing(col: Any, bad: Any) -> Any:
    """A failure mask from a possibly null badness mask: null and NaN rows never fail."""
    return pc.and_(_valid(col), pc.fill_null(bad, False))


def _none(col: Any) -> Any:
    return pa.repeat(False, len(col))


def _any(masks: Sequence[Any], col: Any) -> Any:
    """The union of badness masks (all false when there are none)."""
    result = _none(col)
    for mask in masks:
        result = pc.or_kleene(result, mask)
    return result


def _bad_distinct(col: Any, is_bad: Callable[[Any], Any]) -> Any:
    """Judge the distinct values once and map the verdict back to the rows."""
    distinct = pc.drop_null(pc.unique(col))
    bad = distinct.filter(pc.fill_null(is_bad(distinct), False))
    return _failing(col, pc.is_in(col, value_set=bad))


def _python_bad(good: Callable[[Any], bool]) -> Callable[[Any], Any]:
    def is_bad(distinct: Any) -> Any:
        return pa.array([not good(value) for value in distinct.to_pylist()], pa.bool_())

    return is_bad


# --- required ---------------------------------------------------------------------------------


def not_null(col: Any, present: Any = None) -> KernelResult:
    """Values are neither null nor NaN; ``present`` limits the rows that count (nested values).

    A child of an absent parent is null by construction and never fails: only elements whose
    ``present`` flag is true are judged and counted in the total.
    """
    mask = pc.is_null(col, nan_is_null=True)
    total = len(col)
    if present is not None:
        mask = pc.and_(mask, present)
        total = _count(present)
    failed = _count(mask)
    message = f"{failed} of {total} values are null" if failed else None
    return KernelResult(mask, failed, total, message)


# --- unique and primary key -------------------------------------------------------------------


def _all_valid(columns: Sequence[Any]) -> Any:
    mask = _valid(columns[0])
    for column in columns[1:]:
        mask = pc.and_(mask, _valid(column))
    return mask


def _duplicate_rows(columns: Sequence[Any]) -> tuple[Any, int]:
    """The rows whose complete key repeats another row's key, and how many rows are surplus.

    Rows with any null key are excluded. The surplus is the complete-key rows minus the distinct
    keys, so a key seen three times counts twice.
    """
    total = len(columns[0])
    keys = [f"k{index}" for index in range(len(columns))]
    table = pa.table([*columns, _positions(total)], names=[*keys, "pos"])
    complete = table.filter(_all_valid(columns))
    groups = complete.group_by(keys).aggregate([("pos", "list"), ([], "count_all")])
    repeated = groups.filter(pc.greater(groups["count_all"], 1))
    rows = pc.list_flatten(repeated["pos_list"].combine_chunks())
    surplus = int(pc.sum(pc.subtract(repeated["count_all"], 1)).as_py() or 0)
    return pc.is_in(_positions(total), value_set=rows), surplus


def _duplicate_mask(columns: Sequence[Any]) -> Any:
    """Rows whose complete key repeats another row's key; rows with any null key are excluded."""
    return _duplicate_rows(columns)[0]


def _nested(columns: Sequence[Any]) -> Any:
    return next((column.type for column in columns if pat.is_nested(column.type)), None)


def unique(col: Any) -> KernelResult:
    """Non-null values are unique; every row of a duplicate group fails."""
    nested = _nested([col])
    if nested is not None:
        return skipped(f"unique is not evaluated on {nested} columns")
    return _result(_duplicate_mask([col]), "values repeat another row's value")


def duplicate_values(columns: Sequence[Any]) -> KernelResult:
    """Duplicate rows of the key columns: ``failed_rows`` is the surplus, the mask every repeat.

    This is the ``duplicateValues`` quality metric: complete-key rows minus distinct keys. Nulls
    and NaN never count.
    """
    nested = _nested(columns)
    if nested is not None:
        return skipped(f"duplicateValues is not evaluated on {nested} columns")
    mask, surplus = _duplicate_rows(columns)
    return KernelResult(mask, surplus, len(mask))


def primary_key(columns: Sequence[Any]) -> KernelResult:
    """No key column is null and the combined key is unique (columns in key order)."""
    nested = _nested(columns)
    if nested is not None:
        return skipped(f"primary key is not evaluated on {nested} columns")
    null_rows = pc.invert(_all_valid(columns))
    repeated = _duplicate_mask(columns)
    nulls, repeats = _count(null_rows), _count(repeated)
    detail = f"{nulls} null keys, {repeats} repeated keys"
    return _result(pc.or_(null_rows, repeated), f"rows break the primary key ({detail})")


# --- value comparison -------------------------------------------------------------------------


def _prepare(col: Any) -> Any:
    """Widen a column so contract values compare without losing precision."""
    dtype = col.type
    if pat.is_timestamp(dtype) and dtype.unit in ("s", "ms"):
        return col.cast(pa.timestamp("us", tz=dtype.tz))
    if pat.is_time32(dtype):
        return col.cast(pa.time64("us"))
    if pat.is_floating(dtype) and dtype.bit_width < 64:
        return col.cast(pa.float64())
    return col


def _text_or_none(raw: Any) -> Any:
    return raw if isinstance(raw, str) else _NO_VALUE


def _bool_or_none(raw: Any) -> Any:
    return raw if isinstance(raw, bool) else _NO_VALUE


def _to_int(raw: Any, _dtype: Any) -> Any:
    whole = whole_number(raw)
    return _NO_VALUE if whole is None else whole


def _to_float(raw: Any, _dtype: Any) -> Any:
    number = real_number(raw)
    if number is None:
        return _NO_VALUE
    try:
        return float(number)
    except OverflowError:
        return _NO_VALUE


def _to_decimal(raw: Any, _dtype: Any = None) -> Any:
    number = real_number(raw)
    if number is None:
        return _NO_VALUE
    value = Decimal(number) if isinstance(number, int) else Decimal(repr(number))
    if value.as_tuple().exponent > 0:  # type: ignore[operator]
        value = value.quantize(Decimal(1), context=_WIDE)
    return value


def _to_date(raw: Any, _dtype: Any) -> Any:
    if not isinstance(raw, str):
        return _NO_VALUE
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return _NO_VALUE


def _to_time(raw: Any, _dtype: Any) -> Any:
    if not isinstance(raw, str):
        return _NO_VALUE
    try:
        value = dt.time.fromisoformat(raw)
    except ValueError:
        return _NO_VALUE
    return _NO_VALUE if value.tzinfo is not None else value


def _to_timestamp(raw: Any, dtype: Any) -> Any:
    """A datetime for the column: a naive value is UTC, a zone-less column holds UTC instants."""
    if not isinstance(raw, str):
        return _NO_VALUE
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        value = dt.datetime.fromisoformat(text)
    except ValueError:
        return _NO_VALUE
    if dtype.tz is not None:
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value
    if value.tzinfo is not None:
        return value.astimezone(dt.UTC).replace(tzinfo=None)
    return value


_CONVERTERS: tuple[tuple[Callable[[Any], Any], Callable[[Any, Any], Any]], ...] = (
    (is_string, lambda raw, _dtype: _text_or_none(raw)),
    (pat.is_boolean, lambda raw, _dtype: _bool_or_none(raw)),
    (pat.is_integer, _to_int),
    (pat.is_floating, _to_float),
    (pat.is_decimal, _to_decimal),
    (pat.is_date, _to_date),
    (pat.is_timestamp, _to_timestamp),
    (pat.is_time, _to_time),
)


def _converter(dtype: Any) -> Callable[[Any, Any], Any]:
    """How to turn a contract value into a Python value for a column type."""
    return next(
        (convert for matches, convert in _CONVERTERS if matches(dtype)),
        lambda raw, dtype: _NO_VALUE,
    )


def _to_py(raw: Any, dtype: Any) -> Any:
    """A contract value as the Python value that compares with a column type, or ``_NO_VALUE``."""
    return _converter(dtype)(raw, dtype)


def _comparable(dtype: Any) -> bool:
    return any(matches(dtype) for matches, _ in _CONVERTERS)


def _value_set(dtype: Any, values: Sequence[Any]) -> Any:
    """The contract values that a column of this type can hold, as one array."""
    items = []
    for raw in values:
        value = _to_py(raw, dtype)
        if value is _NO_VALUE:
            continue
        try:
            items.append(pa.array([value], type=dtype))
        except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):
            continue
    return pa.concat_arrays(items) if items else pa.array([], type=dtype)


def member_mask(col: Any, values: Sequence[Any]) -> Any | None:
    """True where a non-null value equals one of the listed values; None for nested columns."""
    prepared = _prepare(col)
    if not _comparable(prepared.type):
        return None
    return pc.is_in(prepared, value_set=_value_set(prepared.type, values))


def in_set(col: Any, values: Sequence[Any]) -> KernelResult:
    """Non-null values belong to the allowed values; a value of another type never matches."""
    prepared = _prepare(col)
    if not _comparable(prepared.type):
        return skipped(f"enum is not evaluated on {col.type} columns")
    allowed = _value_set(prepared.type, values)
    bad = pc.invert(pc.is_in(prepared, value_set=allowed))
    return _result(
        _failing(prepared, bad),
        "values are not in the allowed set",
        threshold=list(values)[:_MAX_LISTED_VALUES],
    )


# --- pattern, length, format ------------------------------------------------------------------


def pattern(col: Any, regex: str) -> KernelResult:
    """String values contain a match of the pattern (unanchored search, RE2 on distinct values)."""
    if not is_string(col.type):
        return skipped(f"pattern applies to string columns, not {col.type}")
    if not re2_supported(regex):
        return skipped(UNSUPPORTED_PATTERN)

    def is_bad(distinct: Any) -> Any:
        return pc.invert(pc.match_substring_regex(distinct, regex))

    return _result(_bad_distinct(col, is_bad), "values do not match the pattern", threshold=regex)


def _describe(*parts: tuple[str, Any]) -> str:
    return ", ".join(f"{label} {value}" for label, value in parts if value is not None)


def length(col: Any, *, min_length: Any = None, max_length: Any = None) -> KernelResult:
    """String length in code points (binary length in bytes) is within the bounds."""
    if is_string(col.type):
        lengths = pc.utf8_length(col)
    elif is_binary(col.type):
        lengths = pc.binary_length(col)
    else:
        return skipped(f"length applies to string and binary columns, not {col.type}")
    lower = None if min_length is None else whole_number(min_length)
    upper = None if max_length is None else whole_number(max_length)
    if (min_length is not None and lower is None) or (max_length is not None and upper is None):
        return skipped("minLength and maxLength must be whole numbers")
    masks = []
    if lower is not None:
        masks.append(pc.less(lengths, pa.scalar(lower)))
    if upper is not None:
        masks.append(pc.greater(lengths, pa.scalar(upper)))
    return _result(
        _failing(col, _any(masks, col)),
        "values have a length outside the allowed bounds",
        threshold=_describe((">=", lower), ("<=", upper)),
    )


_FORMAT_REGEX = {
    "uuid": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    "email": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
}


def _is_ip(version: int) -> Callable[[Any], bool]:
    parse = ipaddress.IPv4Address if version == 4 else ipaddress.IPv6Address

    def good(value: str) -> bool:
        if "%" in value:
            return False
        try:
            parse(value)
        except ValueError:
            return False
        return True

    return good


def _parses_as(strptime: str) -> Callable[[Any], bool]:
    def good(value: str) -> bool:
        try:
            dt.datetime.strptime(value, strptime)  # noqa: DTZ007 - only the shape is checked
        except ValueError:
            return False
        return True

    return good


def _format_judge(fmt: str) -> Callable[[Any], Any] | None:
    """How to find the values of a text column that break a format, or None when unsupported."""
    regex = _FORMAT_REGEX.get(fmt)
    if regex is not None:
        return lambda distinct: pc.invert(pc.match_substring_regex(distinct, regex))
    if fmt in ("ipv4", "ipv6"):
        return _python_bad(_is_ip(4 if fmt == "ipv4" else 6))
    strptime = jdk_to_strptime(fmt)
    return None if strptime is None else _python_bad(_parses_as(strptime))


def format_is(col: Any, fmt: str) -> KernelResult:
    """Text values follow a named format (uuid, email, ipv4, ipv6) or a date pattern."""
    if not is_string(col.type):
        return skipped(f"format {fmt!r} is not applicable to a {col.type} column")
    judge = _format_judge(fmt)
    if judge is None:
        return skipped(f"format {fmt!r} is not evaluated by this engine")
    return _result(
        _bad_distinct(col, judge), f"values do not follow the {fmt} format", threshold=fmt
    )


# --- range ------------------------------------------------------------------------------------


class _UnusableBound(Exception):
    """A contract bound that cannot be compared with the column."""


_BOUND_NAMES = {
    "minimum": "minimum",
    "exclusive_minimum": "exclusiveMinimum",
    "maximum": "maximum",
    "exclusive_maximum": "exclusiveMaximum",
}
# column <op> bound is a failure; the exclusive lower bound fails at equality too.
_FAILS_WHEN = {
    "minimum": pc.less,
    "exclusive_minimum": pc.less_equal,
    "maximum": pc.greater,
    "exclusive_maximum": pc.greater_equal,
}


_ORDERED: tuple[Callable[[Any], bool], ...] = (
    pat.is_floating,
    pat.is_decimal,
    pat.is_date,
    pat.is_timestamp,
    pat.is_time,
)


def _int_limits(dtype: Any) -> tuple[int, int]:
    bits = dtype.bit_width
    if pat.is_signed_integer(dtype):
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return 0, (1 << bits) - 1


def _integer_limits(bounds: dict[str, Any]) -> tuple[int | None, int | None]:
    """The tightest inclusive integer bounds: bounds round toward the inside of the range."""
    lowers: list[int] = []
    uppers: list[int] = []
    for key, raw in bounds.items():
        number = real_number(raw)
        if number is None:
            raise _UnusableBound(f"{_BOUND_NAMES[key]} {raw!r} is not a number")
        if key == "minimum":
            lowers.append(math.ceil(number))
        elif key == "exclusive_minimum":
            lowers.append(math.floor(number) + 1)
        elif key == "maximum":
            uppers.append(math.floor(number))
        else:
            uppers.append(math.ceil(number) - 1)
    return max(lowers, default=None), min(uppers, default=None)


def _integer_range_mask(col: Any, bounds: dict[str, Any]) -> Any:
    lower, upper = _integer_limits(bounds)
    low, high = _int_limits(col.type)
    masks = []
    if lower is not None and lower > low:
        masks.append(pc.is_valid(col) if lower > high else pc.less(col, pa.scalar(lower, col.type)))
    if upper is not None and upper < high:
        masks.append(
            pc.is_valid(col) if upper < low else pc.greater(col, pa.scalar(upper, col.type))
        )
    return _failing(col, _any(masks, col))


def _bound_scalar(key: str, raw: Any, dtype: Any) -> Any:
    value = _to_py(raw, dtype)
    if value is _NO_VALUE:
        raise _UnusableBound(
            f"{_BOUND_NAMES[key]} {raw!r} cannot be compared with a {dtype} column"
        )
    try:
        return pa.scalar(value) if pat.is_decimal(dtype) else pa.scalar(value, type=dtype)
    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as exc:
        raise _UnusableBound(
            f"{_BOUND_NAMES[key]} {raw!r} is out of range for a {dtype} column"
        ) from exc


def _ordered_range_mask(col: Any, bounds: dict[str, Any]) -> Any:
    masks = [
        _FAILS_WHEN[key](col, _bound_scalar(key, raw, col.type)) for key, raw in bounds.items()
    ]
    return _failing(col, _any(masks, col))


def range_check(
    col: Any,
    *,
    minimum: Any = None,
    maximum: Any = None,
    exclusive_minimum: Any = None,
    exclusive_maximum: Any = None,
) -> KernelResult:
    """Values are within the bounds: integers exact, floats as float64, decimals exact."""
    given = {
        "minimum": minimum,
        "exclusive_minimum": exclusive_minimum,
        "maximum": maximum,
        "exclusive_maximum": exclusive_maximum,
    }
    bounds = {key: raw for key, raw in given.items() if raw is not None}
    prepared = _prepare(col)
    dtype = prepared.type
    if pat.is_integer(dtype):
        build = _integer_range_mask
    elif any(matches(dtype) for matches in _ORDERED):
        build = _ordered_range_mask
    else:
        return skipped(f"range applies to numeric and date/time columns, not {col.type}")
    try:
        mask = build(prepared, bounds)
    except _UnusableBound as exc:
        return skipped(str(exc))
    parts = ((">=", minimum), (">", exclusive_minimum), ("<=", maximum), ("<", exclusive_maximum))
    return _result(mask, "values are outside the allowed range", threshold=_describe(*parts))


# --- multipleOf -------------------------------------------------------------------------------

_FLOAT_TOLERANCE = 1e-9


def _integer_multiple_mask(col: Any, factor: int) -> Any:
    _, high = _int_limits(col.type)
    zero = pa.scalar(0, col.type)
    if factor > high:
        return _failing(col, pc.not_equal(col, zero))
    scalar = pa.scalar(factor, col.type)
    remainder = pc.subtract(col, pc.multiply(pc.divide(col, scalar), scalar))
    return _failing(col, pc.not_equal(remainder, zero))


def _float_multiple_mask(col: Any, factor: float) -> Any:
    values = col if pat.is_float64(col.type) else col.cast(pa.float64())
    ratio = pc.divide(values, pa.scalar(factor))
    offset = pc.abs(pc.subtract(ratio, pc.round(ratio)))
    tolerance = pc.multiply(
        pa.scalar(_FLOAT_TOLERANCE), pc.max_element_wise(pa.scalar(1.0), pc.abs(ratio))
    )
    return _failing(col, pc.or_(pc.greater(offset, tolerance), pc.is_inf(ratio)))


def _decimal_multiple_mask(col: Any, factor: Decimal) -> Any:
    def good(value: Decimal) -> bool:
        return bool(_WIDE.remainder(value, factor) == 0)

    return _bad_distinct(col, _python_bad(good))


def multiple_of(col: Any, factor: Any) -> KernelResult:
    """Values are multiples of the factor.

    Integers are exact, decimals exact, floats pass when ``|x/m - round(x/m)| <= 1e-9 * max(1,
    |x/m|)``.
    """
    number = real_number(factor)
    if number is None or number <= 0:
        return skipped("multipleOf must be a number greater than 0")
    dtype = col.type
    if pat.is_integer(dtype) and whole_number(number) is not None:
        mask = _integer_multiple_mask(col, int(number))
    elif pat.is_decimal(dtype):
        mask = _decimal_multiple_mask(col, _to_decimal(number))
    elif pat.is_integer(dtype) or pat.is_floating(dtype):
        mask = _float_multiple_mask(col, float(number))
    else:
        return skipped(f"multipleOf applies to numeric columns, not {dtype}")
    return _result(mask, "values are not a multiple of the factor", threshold=number)


# --- arrays -----------------------------------------------------------------------------------


def _lists_with_duplicates(col: Any) -> Any:
    lists = col.combine_chunks() if isinstance(col, pa.ChunkedArray) else col
    if pat.is_fixed_size_list(lists.type):
        lists = lists.cast(pa.list_(lists.type.value_type))
    pairs = pa.table(
        {"parent": pc.list_parent_indices(lists).cast(pa.uint64()), "item": pc.list_flatten(lists)}
    )
    groups = pairs.group_by(["parent", "item"]).aggregate([([], "count_all")])
    repeated = groups.filter(pc.greater(groups["count_all"], 1))["parent"].combine_chunks()
    return pc.is_in(_positions(len(lists)), value_set=repeated)


def array_bounds(
    col: Any, *, min_items: Any = None, max_items: Any = None, unique_items: bool = False
) -> KernelResult:
    """List length is within the bounds and, when asked, no list repeats an element."""
    if not is_list(col.type):
        return skipped(f"array bounds apply to list columns, not {col.type}")
    lower = None if min_items is None else whole_number(min_items)
    upper = None if max_items is None else whole_number(max_items)
    if (min_items is not None and lower is None) or (max_items is not None and upper is None):
        return skipped("minItems and maxItems must be whole numbers")
    if unique_items and pat.is_nested(col.type.value_type):
        return skipped(f"uniqueItems is not evaluated for {col.type.value_type} elements")
    sizes = pc.list_value_length(col)
    masks = []
    if lower is not None:
        masks.append(pc.less(sizes, pa.scalar(lower)))
    if upper is not None:
        masks.append(pc.greater(sizes, pa.scalar(upper)))
    if unique_items:
        masks.append(_lists_with_duplicates(col))
    unique_note = "unique" if unique_items else None
    return _result(
        _failing(col, _any(masks, col)),
        "lists break the item rules",
        threshold=_describe(("items >=", lower), ("items <=", upper), ("items", unique_note)),
    )
