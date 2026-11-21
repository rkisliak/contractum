"""The platform `dc.*` helper functions available to cel-2 conditions (D-154).

Pure by construction (T-6-05): a function sees only its arguments, performs no I/O, reads no
module state beyond the constant tables below, and bounds its string inputs. This module imports
no CEL runtime and no other `dc_*` package; `dc_rules.cel_engine` turns each `DcFunction` into an
engine declaration, so the engine stays the only module that imports `cel_expr_python`.

An exception inside a function is reported by the engine as an evaluation error whose text maps to
the fixed `engine_error` code; the text never reaches a result (T-6-07).

To add a function: write a pure, input-bounded implementation here, append a `DcFunction` to
`FUNCTIONS`, and add a conformance case in `tests/unit/test_cel2_surface.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

MAX_ARGUMENT_CHARS = 256


@dataclass(frozen=True)
class DcFunction:
    """A `(string, string) -> bool` CEL function: its dotted name, overload id and pure impl."""

    name: str
    overload_id: str
    impl: Callable[[str, str], bool]


# Conservative: only the pairings that are clearly incompatible are refused. A wider physical type
# than the logical type strictly needs (an integer held in a decimal column) is coherent.
_INTEGER = {"int", "integer", "bigint", "smallint", "tinyint", "long", "short", "byte"}
_INTEGER |= {
    "int2",
    "int4",
    "int8",
    "int16",
    "int32",
    "int64",
    "serial",
    "bigserial",
    "smallserial",
}
_NUMBER = _INTEGER | {"decimal", "numeric", "number", "float", "double", "real", "money"}
_NUMBER |= {"float4", "float8", "float16", "float32", "float64", "double precision", "dec"}
_STRING = {"string", "varchar", "char", "text", "nvarchar", "nchar", "clob", "nclob", "varchar2"}
_STRING |= {"character", "character varying", "tinytext", "mediumtext", "longtext", "citext"}
_STRING |= {"uuid"}
_BOOLEAN = {"boolean", "bool", "bit"}
_DATE = {"date", "date32", "datetime", "datetime2", "timestamp", "timestamptz", "timestamp_ntz"}
_DATE |= {"timestamp_ltz", "timestamp_tz", "datetimeoffset", "timestamp with time zone"}
_DATE |= {"timestamp without time zone"}
_TIMESTAMP = {"timestamp", "timestamptz", "datetime", "datetime2", "timestamp_ntz", "timestamp_ltz"}
_TIMESTAMP |= {"timestamp_tz", "datetimeoffset", "timestamp with time zone"}
_TIMESTAMP |= {"timestamp without time zone", "datetime64"}
_TIME = {"time", "timetz", "time with time zone", "time without time zone"}
_OBJECT = {"struct", "object", "record", "row", "map", "json", "jsonb", "variant", "dict"}
_ARRAY = {"array", "list", "json", "jsonb", "variant", "repeated"}

_ALLOWED: dict[str, frozenset[str]] = {
    "string": frozenset(_STRING),
    "integer": frozenset(_INTEGER | {"decimal", "numeric", "number", "dec"}),
    "number": frozenset(_NUMBER),
    "boolean": frozenset(_BOOLEAN),
    "date": frozenset(_DATE),
    "timestamp": frozenset(_TIMESTAMP),
    "time": frozenset(_TIME),
    "object": frozenset(_OBJECT),
    "array": frozenset(_ARRAY),
}
_KNOWN_PHYSICAL: frozenset[str] = frozenset().union(*_ALLOWED.values())


def _base_type(physical: str) -> str:
    """Lower-cased physical type name without any parenthesised or angle-bracket suffix."""
    base = physical.strip().lower()
    for opener in ("(", "<"):
        base = base.split(opener, 1)[0]
    return " ".join(base.split())


def type_coherent(logical_type: str, physical_type: str) -> bool:
    """True unless the ODCS `logicalType` and the declared `physicalType` clearly disagree.

    Anything the table does not know (an unknown physical type, an unknown logical type, an empty
    value, an input over 256 characters) is coherent: no finding without evidence.
    """
    if len(logical_type) > MAX_ARGUMENT_CHARS or len(physical_type) > MAX_ARGUMENT_CHARS:
        return True
    allowed = _ALLOWED.get(logical_type.strip().lower())
    base = _base_type(physical_type)
    if allowed is None or base not in _KNOWN_PHYSICAL:
        return True
    return base in allowed


def _type_coherent_impl(logical_type: str, physical_type: str) -> bool:
    return type_coherent(logical_type, physical_type)


FUNCTIONS: tuple[DcFunction, ...] = (
    DcFunction("dc.type_coherent", "dc_type_coherent", _type_coherent_impl),
)
