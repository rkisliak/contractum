"""Type mapping between ODCS, Arrow and the type names of common dataframe libraries and SQL.

``logical_accepts`` decides whether an Arrow type holds an ODCS logical type,
``parse_physical_type`` reads Arrow, Parquet, pandas and polars type names,
``vendor_type_family`` and ``vendor_to_arrow`` read common SQL names (``VARCHAR(20)``,
``DECIMAL(12,2)``, ``TIMESTAMP_NTZ``; D-47), ``to_arrow_type`` is the inverse map from a contract
property, and ``jdk_to_strptime`` translates the date patterns ODCS borrows from Java.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import pyarrow as pa
import pyarrow.types as pat

from contractum.errors import UsageError
from contractum.odcs import Property

# Inclusive value ranges of the ODCS integer ``format`` widths.
INTEGER_FORMAT_RANGES: dict[str, tuple[int, int]] = {
    **{f"i{bits}": (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) for bits in (8, 16, 32, 64, 128)},
    **{f"u{bits}": (0, (1 << bits) - 1) for bits in (8, 16, 32, 64, 128)},
}

# Largest finite float32; the ODCS number ``format`` f32 is a value range of this size.
FLOAT32_MAX = 3.4028234663852886e38

_INTEGER_ARROW_TYPES: dict[str, Callable[[], pa.DataType]] = {
    "i8": pa.int8,
    "i16": pa.int16,
    "i32": pa.int32,
    "i64": pa.int64,
    "u8": pa.uint8,
    "u16": pa.uint16,
    "u32": pa.uint32,
    "u64": pa.uint64,
}


def real_number(raw: Any) -> int | float | None:
    """A contract number (a bool is not one, nor is a non-finite float), else None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return raw if not isinstance(raw, float) or math.isfinite(raw) else None


def whole_number(raw: Any) -> int | None:
    """A contract integer, accepting an integral float such as ``5.0``; else None."""
    number = real_number(raw)
    if number is None or isinstance(number, int):
        return number
    return int(number) if number.is_integer() else None


def _unwrap(dtype: Any) -> Any:
    while pat.is_dictionary(dtype):
        dtype = dtype.value_type
    return dtype


def is_string(dtype: Any) -> bool:
    """Whether the type is any Arrow string type."""
    return bool(pat.is_string(dtype) or pat.is_large_string(dtype) or pat.is_string_view(dtype))


def is_binary(dtype: Any) -> bool:
    """Whether the type is any Arrow binary type."""
    return bool(
        pat.is_binary(dtype)
        or pat.is_large_binary(dtype)
        or pat.is_binary_view(dtype)
        or pat.is_fixed_size_binary(dtype)
    )


def is_list(dtype: Any) -> bool:
    """Whether the type is a list, large list or fixed-size list."""
    return bool(pat.is_list(dtype) or pat.is_large_list(dtype) or pat.is_fixed_size_list(dtype))


def _is_number(dtype: Any) -> bool:
    return bool(pat.is_floating(dtype) or pat.is_decimal(dtype) or pat.is_integer(dtype))


def _is_vector(dtype: Any) -> bool:
    return bool(pat.is_fixed_size_list(dtype) and pat.is_floating(dtype.value_type))


_TYPE_PREDICATES: dict[str, Callable[[Any], bool]] = {
    "string": is_string,
    "integer": pat.is_integer,
    "number": _is_number,
    "boolean": pat.is_boolean,
    "date": pat.is_date,
    "timestamp": pat.is_timestamp,
    "time": pat.is_time,
    "array": is_list,
    "object": pat.is_struct,
    "map": pat.is_map,
    "vector": _is_vector,
}


def logical_accepts(logical_type: str, arrow_type: Any, *, strict_types: bool = False) -> bool:
    """Whether an Arrow type holds values of an ODCS logical type.

    A whole number is a number unless ``strict_types`` asks for float or decimal columns.
    """
    dtype = _unwrap(arrow_type)
    if logical_type == "number" and strict_types:
        return bool(pat.is_floating(dtype) or pat.is_decimal(dtype))
    predicate = _TYPE_PREDICATES.get(logical_type)
    return predicate is not None and bool(predicate(dtype))


_PHYSICAL_NAMES: dict[str, pa.DataType] = {
    **{name: getattr(pa, name)() for name in ("int8", "int16", "int32", "int64")},
    **{name: getattr(pa, name)() for name in ("uint8", "uint16", "uint32", "uint64")},
    "float16": pa.float16(),
    "halffloat": pa.float16(),
    "float32": pa.float32(),
    "float": pa.float32(),
    "float64": pa.float64(),
    "double": pa.float64(),
    "bool": pa.bool_(),
    "boolean": pa.bool_(),
    "string": pa.string(),
    "utf8": pa.string(),
    "large_string": pa.large_string(),
    "large_utf8": pa.large_string(),
    "binary": pa.binary(),
    "large_binary": pa.large_binary(),
    "date32": pa.date32(),
    "date32[day]": pa.date32(),
    "date64": pa.date64(),
    "date64[ms]": pa.date64(),
}

_TIMESTAMP = re.compile(r"^timestamp\[(s|ms|us|ns)(?:, (?:tz=)?([^,\]]+))?\]$", re.IGNORECASE)
_TIME = re.compile(r"^time(32|64)\[(s|ms|us|ns)\]$", re.IGNORECASE)
_DECIMAL = re.compile(r"^decimal(128|256)\((\d{1,2}), ?(-?\d{1,2})\)$", re.IGNORECASE)


def _parse_timestamp(text: str) -> pa.DataType | None:
    match = _TIMESTAMP.match(text)
    if match is None:
        return None
    zone = match.group(2)
    return pa.timestamp(match.group(1).lower(), tz=zone.strip() if zone else None)


def _parse_time(text: str) -> pa.DataType | None:
    match = _TIME.match(text)
    if match is None:
        return None
    unit = match.group(2).lower()
    return pa.time32(unit) if match.group(1) == "32" else pa.time64(unit)


def _parse_decimal(text: str) -> pa.DataType | None:
    match = _DECIMAL.match(text)
    if match is None:
        return None
    build = pa.decimal128 if match.group(1) == "128" else pa.decimal256
    return build(int(match.group(2)), int(match.group(3)))


def parse_physical_type(text: str) -> pa.DataType | None:
    """The Arrow type a physicalType names, or None when it is not an Arrow-comparable name.

    Reads Arrow and Parquet spellings (``int64``, ``double``, ``timestamp[us, tz=UTC]``,
    ``decimal128(10, 2)``) and the pandas and polars ones (``Int64``, ``Utf8``, ``Boolean``),
    case-insensitively.
    """
    name = text.strip()
    known = _PHYSICAL_NAMES.get(name.lower())
    if known is not None:
        return known
    for parse in (_parse_timestamp, _parse_time, _parse_decimal):
        try:
            parsed = parse(name)
        except (ValueError, pa.ArrowException):
            return None
        if parsed is not None:
            return parsed
    return None


def _canonical(dtype: Any) -> Any:
    """Collapse the Arrow spellings of one physical type (string views, large offsets)."""
    dtype = _unwrap(dtype)
    if is_string(dtype):
        return pa.string()
    if pat.is_binary(dtype) or pat.is_large_binary(dtype) or pat.is_binary_view(dtype):
        return pa.binary()
    return dtype


def physical_type_matches(expected: pa.DataType, actual: Any) -> bool:
    """Whether a column type is the named physical type.

    String and binary spellings are interchangeable, dictionary encoding is ignored, and a
    timestamp without a zone in the name matches the unit whatever the column's zone is.
    """
    want, have = _canonical(expected), _canonical(actual)
    if pat.is_timestamp(want) and pat.is_timestamp(have):
        return bool(want.unit == have.unit and (want.tz is None or want.tz == have.tz))
    return bool(want == have)


# --- vendor (SQL) physical types ----------------------------------------------------------------

FamilyKind = Literal[
    "string", "number", "decimal", "integer", "float", "bool", "date", "timestamp", "time", "binary"
]


@dataclass(frozen=True)
class TypeFamily:
    """The Arrow types a vendor physical type accepts.

    ``bits`` is the exact width of an integer or float (None: float32 or float64), ``precision``
    and ``scale`` describe a decimal (``number`` is any numeric type), ``tz`` is None for any
    timestamp, True when a time zone is required and False when it is forbidden. ``preferred`` is
    the float width ``vendor_to_arrow`` builds.
    """

    kind: FamilyKind
    bits: int | None = None
    precision: int | None = None
    scale: int | None = None
    tz: bool | None = None
    preferred: int | None = None


_STRING_NAMES = (
    "VARCHAR",
    "VARCHAR2",
    "NVARCHAR",
    "NVARCHAR2",
    "CHAR",
    "NCHAR",
    "CHARACTER",
    "CHARACTER VARYING",
    "CHAR VARYING",
    "TEXT",
    "STRING",
    "CLOB",
)
_DECIMAL_NAMES = frozenset({"DECIMAL", "DEC", "NUMERIC", "NUMBER"})
_INTEGER_BITS = {"TINYINT": 8, "SMALLINT": 16, "INT": 32, "INTEGER": 32, "BIGINT": 64}
_SIMPLE_FAMILIES: dict[str, TypeFamily] = {
    **{name: TypeFamily("string") for name in _STRING_NAMES},
    **{name: TypeFamily("integer", bits=bits) for name, bits in _INTEGER_BITS.items()},
    "FLOAT": TypeFamily("float", preferred=64),
    "REAL": TypeFamily("float", preferred=32),
    "DOUBLE": TypeFamily("float", bits=64),
    "DOUBLE PRECISION": TypeFamily("float", bits=64),
    "BOOLEAN": TypeFamily("bool"),
    "BOOL": TypeFamily("bool"),
    "DATE": TypeFamily("date"),
    "TIMESTAMP": TypeFamily("timestamp"),
    "DATETIME": TypeFamily("timestamp"),
    "TIMESTAMP_NTZ": TypeFamily("timestamp", tz=False),
    "TIMESTAMP WITHOUT TIME ZONE": TypeFamily("timestamp", tz=False),
    "TIMESTAMP_TZ": TypeFamily("timestamp", tz=True),
    "TIMESTAMP_LTZ": TypeFamily("timestamp", tz=True),
    "TIMESTAMPTZ": TypeFamily("timestamp", tz=True),
    "TIMESTAMP WITH TIME ZONE": TypeFamily("timestamp", tz=True),
    "TIMESTAMP WITH LOCAL TIME ZONE": TypeFamily("timestamp", tz=True),
    "TIME": TypeFamily("time"),
    "TIME WITHOUT TIME ZONE": TypeFamily("time"),
    "BINARY": TypeFamily("binary"),
    "VARBINARY": TypeFamily("binary"),
    "BINARY VARYING": TypeFamily("binary"),
    "BYTEA": TypeFamily("binary"),
    "BLOB": TypeFamily("binary"),
}
# SQL FLOAT has no fixed width, while the Arrow name ``float`` is 32 bits: only the SQL spelling
# in capitals reads as a vendor type, the lower-case Arrow spelling stays exact (D-47).
_SQL_BEFORE_ARROW = frozenset({"FLOAT"})
_MAX_DECIMAL128 = 38
_MAX_DECIMAL256 = 76


def _split_vendor(text: str) -> tuple[str, tuple[int, ...]] | None:
    """The upper-cased name and the numeric arguments of ``NAME(p[,s])``; None if malformed."""
    name = text.strip()
    opened = name.find("(")
    if opened < 0:
        return " ".join(name.upper().split()), ()
    if not name.endswith(")"):
        return None
    parts = [part.strip() for part in name[opened + 1 : -1].split(",")]
    if not 1 <= len(parts) <= 2 or not all(part.isascii() and part.isdigit() for part in parts):
        return None
    return " ".join(name[:opened].upper().split()), tuple(int(part) for part in parts)


def _decimal_family(arguments: tuple[int, ...]) -> TypeFamily | None:
    if not arguments:
        return TypeFamily("number")
    precision = arguments[0]
    scale = arguments[1] if len(arguments) > 1 else 0
    if not 1 <= precision <= _MAX_DECIMAL256 or scale > precision:
        return None
    return TypeFamily("decimal", precision=precision, scale=scale)


def vendor_type_family(text: str) -> TypeFamily | None:
    """The family of a common SQL physical type, or None when it has no Arrow mapping.

    Case-insensitive, with optional ``(p)`` or ``(p,s)`` arguments: ``VARCHAR(20)``, ``CHAR``,
    ``TEXT``, ``NUMBER(12,2)``, ``DECIMAL``, ``INT``, ``BIGINT``, ``FLOAT``, ``DOUBLE``,
    ``BOOLEAN``, ``DATE``, ``TIMESTAMP``, ``TIMESTAMP_NTZ``, ``TIMESTAMP_TZ``, ``TIME``,
    ``BINARY`` and their synonyms. Other names (``GEOGRAPHY``, ``VARIANT``) return None.
    """
    split = _split_vendor(text)
    if split is None:
        return None
    name, arguments = split
    if name in _DECIMAL_NAMES:
        return _decimal_family(arguments)
    return _SIMPLE_FAMILIES.get(name)


def _decimal_accepts(family: TypeFamily, dtype: Any) -> bool:
    return bool(
        pat.is_decimal(dtype)
        and dtype.scale == family.scale
        and dtype.precision >= (family.precision or 0)
    )


def _timestamp_accepts(family: TypeFamily, dtype: Any) -> bool:
    if not pat.is_timestamp(dtype):
        return False
    return family.tz is None or family.tz == (dtype.tz is not None)


_FAMILY_ACCEPTS: dict[str, Callable[[TypeFamily, Any], bool]] = {
    "string": lambda family, dtype: is_string(dtype),
    "number": lambda family, dtype: _is_number(dtype),
    "decimal": _decimal_accepts,
    "integer": lambda family, dtype: bool(
        pat.is_signed_integer(dtype) and dtype.bit_width == family.bits
    ),
    "float": lambda family, dtype: bool(
        pat.is_floating(dtype) and dtype.bit_width in ((family.bits,) if family.bits else (32, 64))
    ),
    "bool": lambda family, dtype: bool(pat.is_boolean(dtype)),
    "date": lambda family, dtype: bool(pat.is_date(dtype)),
    "timestamp": _timestamp_accepts,
    "time": lambda family, dtype: bool(pat.is_time(dtype)),
    "binary": lambda family, dtype: is_binary(dtype),
}


def family_accepts(family: TypeFamily, arrow_type: Any) -> bool:
    """Whether a column type belongs to the vendor family (dictionary encoding is ignored)."""
    return _FAMILY_ACCEPTS[family.kind](family, _unwrap(arrow_type))


def _decimal_type(family: TypeFamily) -> pa.DataType | None:
    precision, scale = family.precision or _MAX_DECIMAL128, family.scale or 0
    if precision <= _MAX_DECIMAL128:
        return pa.decimal128(precision, scale)
    return pa.decimal256(precision, scale) if precision <= _MAX_DECIMAL256 else None


_FAMILY_TYPES: dict[str, Callable[[TypeFamily], pa.DataType | None]] = {
    "string": lambda family: pa.string(),
    "number": lambda family: pa.decimal128(_MAX_DECIMAL128, 0),
    "decimal": _decimal_type,
    "integer": lambda family: {8: pa.int8, 16: pa.int16, 32: pa.int32, 64: pa.int64}[
        family.bits or 64
    ](),
    "float": lambda family: (
        pa.float32() if (family.bits or family.preferred) == 32 else pa.float64()
    ),
    "bool": lambda family: pa.bool_(),
    "date": lambda family: pa.date32(),
    "timestamp": lambda family: pa.timestamp("us", tz="UTC" if family.tz else None),
    "time": lambda family: pa.time64("us"),
    "binary": lambda family: pa.binary(),
}


def vendor_to_arrow(text: str) -> pa.DataType | None:
    """A concrete Arrow type for a vendor physical type, or None when it has no mapping.

    ``VARCHAR(n)`` and friends become ``string``, ``NUMBER(p,s)`` a decimal, ``INT`` ``int32``,
    ``FLOAT`` ``float64``, ``REAL`` ``float32``, ``TIMESTAMP_TZ`` a UTC timestamp, a bare
    ``NUMBER`` ``decimal128(38, 0)``.
    """
    family = vendor_type_family(text)
    return None if family is None else _FAMILY_TYPES[family.kind](family)


def physical_type_known(text: str) -> bool:
    """Whether a physical type name maps to Arrow, as an Arrow name or a vendor family."""
    return parse_physical_type(text) is not None or vendor_type_family(text) is not None


def physical_type_accepts(text: str, arrow_type: Any) -> bool | None:
    """Whether a column type is the named physical type; None when the name has no mapping.

    Arrow, Parquet, pandas and polars names are read first (exactly), then the vendor families.
    """
    vendor = vendor_type_family(text)
    exact = parse_physical_type(text)
    if vendor is not None and (exact is None or text.strip() in _SQL_BEFORE_ARROW):
        return family_accepts(vendor, arrow_type)
    if exact is not None:
        return physical_type_matches(exact, arrow_type)
    return None


def _integer_type(prop: Property) -> pa.DataType:
    fmt = prop.options.get("format")
    builder = _INTEGER_ARROW_TYPES.get(fmt) if isinstance(fmt, str) else None
    if builder is not None:
        return builder()
    if fmt in ("i128", "u128"):
        return pa.decimal256(39, 0)
    return pa.int64()


def _number_type(prop: Property) -> pa.DataType:
    return pa.float32() if prop.options.get("format") == "f32" else pa.float64()


def _timestamp_type(prop: Property) -> pa.DataType:
    return pa.timestamp("us", tz="UTC" if prop.options.get("timezone") is True else None)


def _array_type(prop: Property) -> pa.DataType:
    return pa.list_(to_arrow_type(prop.items) if prop.items is not None else pa.null())


def _object_type(prop: Property) -> pa.DataType:
    return pa.struct([pa.field(item.source_name, to_arrow_type(item)) for item in prop.nested])


def _vector_type(prop: Property) -> pa.DataType:
    dimensions = prop.options.get("dimensions")
    element = pa.float64() if prop.options.get("elementType") == "float64" else pa.float32()
    if isinstance(dimensions, int) and not isinstance(dimensions, bool) and dimensions > 0:
        return pa.list_(element, dimensions)
    return pa.list_(element)


_LOGICAL_BUILDERS: dict[str, Callable[[Property], pa.DataType]] = {
    "string": lambda _: pa.string(),
    "integer": _integer_type,
    "number": _number_type,
    "boolean": lambda _: pa.bool_(),
    "date": lambda _: pa.date32(),
    "timestamp": _timestamp_type,
    "time": lambda _: pa.time64("us"),
    "array": _array_type,
    "object": _object_type,
    "map": lambda _: pa.map_(pa.string(), pa.string()),
    "vector": _vector_type,
}


def to_arrow_type(prop: Property) -> pa.DataType:
    """The Arrow type a contract property describes (the inverse of the type map).

    A comparable physicalType wins over the logicalType; otherwise the logicalType and its
    options choose the type. A property that names neither raises a UsageError.
    """
    if prop.physical_type is not None:
        physical = parse_physical_type(prop.physical_type) or vendor_to_arrow(prop.physical_type)
        if physical is not None:
            return physical
    builder = _LOGICAL_BUILDERS.get((prop.logical_type or "").lower())
    if builder is None:
        raise UsageError(
            f"property {prop.name!r} has no logicalType that maps to an Arrow type "
            f"(logicalType {prop.logical_type!r})"
        )
    return builder(prop)


# JDK DateTimeFormatter letters this engine translates, longest token first.
_JDK_TOKENS = (
    ("yyyy", "%Y"),
    ("SSS", "%f"),
    ("yy", "%y"),
    ("MM", "%m"),
    ("dd", "%d"),
    ("HH", "%H"),
    ("mm", "%M"),
    ("ss", "%S"),
)


def _jdk_quoted(pattern: str, start: int) -> tuple[str, int] | None:
    """The literal text of a quoted run starting at ``start``, and the index after it.

    ``''`` is an apostrophe, both on its own and inside a quoted run.
    """
    if pattern.startswith("''", start):
        return "'", start + 2
    text: list[str] = []
    index = start + 1
    while index < len(pattern):
        if pattern.startswith("''", index):
            text.append("'")
            index += 2
        elif pattern[index] == "'":
            return "".join(text).replace("%", "%%"), index + 1
        else:
            text.append(pattern[index])
            index += 1
    return None


def _jdk_token(pattern: str, start: int) -> tuple[str, int] | None:
    for token, strptime in _JDK_TOKENS:
        if pattern.startswith(token, start):
            return strptime, start + len(token)
    return None


def jdk_to_strptime(pattern: str) -> str | None:
    """Translate a JDK date pattern to a strptime format, or None when it cannot be translated.

    Handles ``yyyy yy MM dd HH mm ss SSS``, separators and single-quoted text. Any other letter
    (month names, zones, ``a``) makes the pattern untranslatable.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        step = (
            _jdk_quoted(pattern, index)
            if char == "'"
            else _jdk_token(pattern, index)
            if char.isalpha()
            else (char.replace("%", "%%"), index + 1)
        )
        if step is None:
            return None
        text, index = step
        out.append(text)
    return "".join(out) or None
