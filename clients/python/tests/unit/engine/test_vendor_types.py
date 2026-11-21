"""Vendor physical types (D-47): families, Arrow construction and the physical_type check."""

from __future__ import annotations

import time

import pyarrow as pa
import pytest
from support.engine import plan_properties, prop, run_properties

from contractum.engine.types import (
    TypeFamily,
    family_accepts,
    physical_type_accepts,
    physical_type_known,
    to_arrow_type,
    vendor_to_arrow,
    vendor_type_family,
)
from contractum.odcs import Property

_STRINGS = [pa.string(), pa.large_string(), pa.string_view()]
_UTC = "UTC"

# (vendor type, column types that satisfy it, column types that do not)
_ACCEPTS = [
    ("VARCHAR(20)", _STRINGS, [pa.int64(), pa.binary()]),
    ("varchar(20)", _STRINGS, [pa.date32()]),
    ("CHAR(3)", _STRINGS, [pa.int32()]),
    ("TEXT", _STRINGS, [pa.bool_()]),
    ("STRING", _STRINGS, [pa.binary()]),
    (
        "DECIMAL(12,2)",
        [pa.decimal128(12, 2), pa.decimal128(18, 2)],
        [pa.decimal128(12, 3), pa.float64(), pa.decimal128(10, 2), pa.int64()],
    ),
    ("NUMBER(12, 2)", [pa.decimal128(12, 2)], [pa.decimal128(12, 0)]),
    ("NUMBER(10)", [pa.decimal128(10, 0), pa.decimal128(38, 0)], [pa.decimal128(10, 1)]),
    ("NUMERIC", [pa.decimal128(5, 1), pa.int64(), pa.float64()], [pa.string(), pa.bool_()]),
    ("NUMBER", [pa.decimal128(38, 9), pa.int32(), pa.float32()], [pa.date32()]),
    ("INT", [pa.int32()], [pa.int64(), pa.int16(), pa.uint32(), pa.float64()]),
    ("INTEGER", [pa.int32()], [pa.int64()]),
    ("BIGINT", [pa.int64()], [pa.int32(), pa.uint64()]),
    ("SMALLINT", [pa.int16()], [pa.int32(), pa.int8()]),
    ("TINYINT", [pa.int8()], [pa.int16()]),
    ("FLOAT", [pa.float32(), pa.float64()], [pa.float16(), pa.int32(), pa.decimal128(5, 2)]),
    ("REAL", [pa.float32(), pa.float64()], [pa.int64()]),
    ("DOUBLE", [pa.float64()], [pa.float32()]),
    ("DOUBLE PRECISION", [pa.float64()], [pa.float32()]),
    ("BOOLEAN", [pa.bool_()], [pa.int8()]),
    ("DATE", [pa.date32(), pa.date64()], [pa.timestamp("us")]),
    ("TIMESTAMP", [pa.timestamp("us"), pa.timestamp("ns", tz=_UTC)], [pa.date32(), pa.int64()]),
    ("TIMESTAMP(3)", [pa.timestamp("ms")], [pa.time32("ms")]),
    ("TIMESTAMP_NTZ", [pa.timestamp("us")], [pa.timestamp("us", tz=_UTC)]),
    ("TIMESTAMP WITHOUT TIME ZONE", [pa.timestamp("ns")], [pa.timestamp("ns", tz=_UTC)]),
    ("TIMESTAMP_TZ", [pa.timestamp("us", tz=_UTC)], [pa.timestamp("us")]),
    ("timestamp with time zone", [pa.timestamp("us", tz="Europe/Oslo")], [pa.timestamp("us")]),
    ("TIME", [pa.time32("s"), pa.time64("us")], [pa.timestamp("us"), pa.date32()]),
    ("BINARY", [pa.binary(), pa.large_binary(), pa.binary(4)], [pa.string()]),
    ("VARBINARY(16)", [pa.binary()], [pa.int64()]),
]


@pytest.mark.parametrize(
    ("text", "accepted", "rejected"), _ACCEPTS, ids=[row[0] for row in _ACCEPTS]
)
def test_a_vendor_family_accepts_its_arrow_types_and_rejects_the_rest(
    text: str, accepted: list[pa.DataType], rejected: list[pa.DataType]
) -> None:
    family = vendor_type_family(text)

    assert family is not None
    assert [family_accepts(family, dtype) for dtype in accepted] == [True] * len(accepted)
    assert [family_accepts(family, dtype) for dtype in rejected] == [False] * len(rejected)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("VARCHAR(20)", pa.string()),
        ("CHAR(3)", pa.string()),
        ("TEXT", pa.string()),
        ("DECIMAL(12,2)", pa.decimal128(12, 2)),
        ("NUMBER(40,2)", pa.decimal256(40, 2)),
        ("NUMBER", pa.decimal128(38, 0)),
        ("NUMERIC(10)", pa.decimal128(10, 0)),
        ("INT", pa.int32()),
        ("BIGINT", pa.int64()),
        ("SMALLINT", pa.int16()),
        ("FLOAT", pa.float64()),
        ("REAL", pa.float32()),
        ("DOUBLE", pa.float64()),
        ("BOOLEAN", pa.bool_()),
        ("DATE", pa.date32()),
        ("TIMESTAMP", pa.timestamp("us")),
        ("TIMESTAMP_NTZ", pa.timestamp("us")),
        ("TIMESTAMP_TZ", pa.timestamp("us", tz=_UTC)),
        ("TIME", pa.time64("us")),
        ("BINARY", pa.binary()),
        ("VARBINARY(8)", pa.binary()),
    ],
)
def test_vendor_to_arrow_builds_a_concrete_type_that_its_own_family_accepts(
    text: str, expected: pa.DataType
) -> None:
    built = vendor_to_arrow(text)

    assert built == expected
    family = vendor_type_family(text)
    assert family is not None
    assert family_accepts(family, built)


@pytest.mark.parametrize(
    "text",
    [
        "GEOGRAPHY",
        "VARIANT",
        "",
        "VARCHAR(",
        "VARCHAR(a)",
        "VARCHAR(1,2,3)",
        "DECIMAL(5,10)",
        "DECIMAL(0)",
        "NUMBER(80)",
        "VARCHAR(12)x",
        "DECIMAL(1_0)",
    ],
)
def test_unmappable_or_malformed_types_have_no_arrow_mapping(text: str) -> None:
    assert vendor_to_arrow(text) is None
    assert physical_type_known(text) is False
    assert physical_type_accepts(text, pa.string()) is None


def test_the_family_value_describes_the_declared_type() -> None:
    assert vendor_type_family("DECIMAL(12,2)") == TypeFamily("decimal", precision=12, scale=2)
    assert vendor_type_family("  timestamp_tz ") == TypeFamily("timestamp", tz=True)
    assert vendor_type_family("DECIMAL") == TypeFamily("number")


def test_parsing_a_very_long_name_is_linear() -> None:
    hostile = "A " * 50_000 + "(" + "1," * 50_000

    started = time.perf_counter()
    result = vendor_type_family(hostile)

    assert result is None
    assert time.perf_counter() - started < 1.0


def test_arrow_names_win_and_float_in_capitals_reads_as_the_sql_type() -> None:
    assert physical_type_accepts("float", pa.float64()) is False
    assert physical_type_accepts("float", pa.float32()) is True
    assert physical_type_accepts("FLOAT", pa.float64()) is True
    assert physical_type_accepts("double", pa.float64()) is True
    assert physical_type_accepts("int64", pa.int64()) is True
    assert physical_type_accepts("VARCHAR(5)", pa.large_string()) is True
    assert physical_type_known("int64") is True
    assert physical_type_known("DECIMAL(5,2)") is True


def test_to_arrow_type_uses_a_vendor_physical_type_before_the_logical_type() -> None:
    declared = Property(name="p", logical_type="number", physical_type="DECIMAL(12,2)")
    plain = Property(name="p", logical_type="string", physical_type="GEOGRAPHY")

    assert to_arrow_type(declared) == pa.decimal128(12, 2)
    assert to_arrow_type(plain) == pa.string()


# --- through plan and backend -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("physical", "column", "status"),
    [
        ("VARCHAR(20)", pa.array(["a"], pa.string()), "passed"),
        ("VARCHAR(20)", pa.array(["a"], pa.large_string()), "passed"),
        ("VARCHAR(20)", pa.array([1], pa.int64()), "failed"),
        ("DECIMAL(12,2)", pa.array([1], pa.decimal128(12, 2)), "passed"),
        ("DECIMAL(12,2)", pa.array([1], pa.decimal128(18, 2)), "passed"),
        ("DECIMAL(12,2)", pa.array([1], pa.decimal128(12, 3)), "failed"),
        ("DECIMAL(12,2)", pa.array([1.0], pa.float64()), "failed"),
        ("INT", pa.array([1], pa.int32()), "passed"),
        ("INT", pa.array([1], pa.int64()), "failed"),
        ("BIGINT", pa.array([1], pa.int64()), "passed"),
        ("TIMESTAMP", pa.array([1], pa.timestamp("us", tz=_UTC)), "passed"),
        ("TIMESTAMP_NTZ", pa.array([1], pa.timestamp("us")), "passed"),
        ("TIMESTAMP_NTZ", pa.array([1], pa.timestamp("us", tz=_UTC)), "failed"),
        ("TIMESTAMP_TZ", pa.array([1], pa.timestamp("us", tz=_UTC)), "passed"),
        ("TIMESTAMP_TZ", pa.array([1], pa.timestamp("us")), "failed"),
        ("BOOLEAN", pa.array([True]), "passed"),
        ("BINARY", pa.array([b"x"]), "passed"),
    ],
)
def test_the_physical_type_check_judges_vendor_types(
    physical: str, column: pa.Array, status: str
) -> None:
    outcome = run_properties([prop("c", None, physicalType=physical)], pa.table({"c": column}))[
        "t.c.physical_type"
    ]

    assert outcome.status == status
    assert outcome.threshold == physical or outcome.status == "failed"


@pytest.mark.parametrize("physical", ["GEOGRAPHY", "VARIANT"])
def test_only_unmappable_types_are_skipped_with_a_reason(physical: str) -> None:
    the_plan = plan_properties([prop("c", None, physicalType=physical)])

    (entry,) = [s for s in the_plan.skipped if s.rule == "physical_type"]
    assert entry.reason == f"physicalType {physical!r} has no Arrow mapping"
    assert not any(check.rule == "physical_type" for check in the_plan.checks)
