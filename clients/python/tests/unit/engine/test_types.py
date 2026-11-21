"""Type map: logical acceptance, physical type names, the inverse map and JDK date patterns."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from contractum.engine.types import (
    FLOAT32_MAX,
    INTEGER_FORMAT_RANGES,
    jdk_to_strptime,
    logical_accepts,
    parse_physical_type,
    physical_type_matches,
    real_number,
    to_arrow_type,
    whole_number,
)
from contractum.errors import UsageError
from contractum.odcs import Property


@pytest.mark.parametrize(
    ("logical", "dtype", "strict", "expected"),
    [
        ("string", pa.string(), False, True),
        ("string", pa.large_string(), False, True),
        ("string", pa.string_view(), False, True),
        ("string", pa.dictionary(pa.int8(), pa.string()), False, True),
        ("string", pa.int64(), False, False),
        ("integer", pa.uint8(), False, True),
        ("integer", pa.float64(), False, False),
        ("number", pa.float32(), False, True),
        ("number", pa.decimal128(10, 2), False, True),
        ("number", pa.int32(), False, True),
        ("number", pa.int32(), True, False),
        ("number", pa.float64(), True, True),
        ("boolean", pa.bool_(), False, True),
        ("date", pa.date32(), False, True),
        ("timestamp", pa.timestamp("ms"), False, True),
        ("time", pa.time64("us"), False, True),
        ("array", pa.list_(pa.int8()), False, True),
        ("array", pa.large_list(pa.int8()), False, True),
        ("array", pa.list_(pa.int8(), 3), False, True),
        ("object", pa.struct([("a", pa.int8())]), False, True),
        ("map", pa.map_(pa.string(), pa.int8()), False, True),
        ("vector", pa.list_(pa.float32(), 3), False, True),
        ("vector", pa.list_(pa.int8(), 3), False, False),
        ("vector", pa.list_(pa.float32()), False, False),
        ("unknown", pa.int8(), False, False),
    ],
)
def test_logical_type_acceptance(logical: str, dtype: Any, strict: bool, expected: bool) -> None:
    assert logical_accepts(logical, dtype, strict_types=strict) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("int8", pa.int8()),
        ("INT16", pa.int16()),
        ("Int32", pa.int32()),
        ("Int64", pa.int64()),
        ("uint8", pa.uint8()),
        ("UInt64", pa.uint64()),
        ("float16", pa.float16()),
        ("float32", pa.float32()),
        ("Float64", pa.float64()),
        ("float", pa.float32()),
        ("double", pa.float64()),
        ("bool", pa.bool_()),
        ("Boolean", pa.bool_()),
        ("string", pa.string()),
        ("String", pa.string()),
        ("Utf8", pa.string()),
        ("large_string", pa.large_string()),
        ("binary", pa.binary()),
        ("large_binary", pa.large_binary()),
        ("date32", pa.date32()),
        ("date64", pa.date64()),
        ("date32[day]", pa.date32()),
        ("timestamp[us]", pa.timestamp("us")),
        ("timestamp[ns, tz=UTC]", pa.timestamp("ns", tz="UTC")),
        ("Timestamp[ms, tz=Europe/Oslo]", pa.timestamp("ms", tz="Europe/Oslo")),
        ("timestamp[s, UTC]", pa.timestamp("s", tz="UTC")),
        ("time32[ms]", pa.time32("ms")),
        ("time64[ns]", pa.time64("ns")),
        ("decimal128(10, 2)", pa.decimal128(10, 2)),
        ("DECIMAL256(40,5)", pa.decimal256(40, 5)),
    ],
)
def test_physical_type_names_that_arrow_parquet_pandas_and_polars_use(
    text: str, expected: pa.DataType
) -> None:
    assert parse_physical_type(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "VARCHAR(255)",
        "NUMBER(10,2)",
        "TIMESTAMP_NTZ",
        "int",
        "",
        "timestamp[xs]",
        "decimal128(99, 2)",
        "decimal128(0, 0)",
        "time32[us]",
    ],
)
def test_other_physical_type_names_are_not_comparable(text: str) -> None:
    assert parse_physical_type(text) is None


@pytest.mark.parametrize(
    ("expected", "actual", "matches"),
    [
        (pa.string(), pa.large_string(), True),
        (pa.large_string(), pa.string_view(), True),
        (pa.binary(), pa.binary_view(), True),
        (pa.int32(), pa.int64(), False),
        (pa.string(), pa.dictionary(pa.int8(), pa.string()), True),
        (pa.timestamp("us"), pa.timestamp("us", tz="UTC"), True),
        (pa.timestamp("us", tz="UTC"), pa.timestamp("us"), False),
        (pa.timestamp("us", tz="UTC"), pa.timestamp("us", tz="Europe/Oslo"), False),
        (pa.timestamp("us"), pa.timestamp("ns"), False),
        (pa.decimal128(10, 2), pa.decimal128(10, 3), False),
    ],
)
def test_physical_type_matching_ignores_spelling_but_not_width(
    expected: pa.DataType, actual: pa.DataType, matches: bool
) -> None:
    assert physical_type_matches(expected, actual) is matches


def _prop(logical: str | None, physical: str | None = None, **fields: Any) -> Property:
    options = fields.pop("options", {})
    return Property(
        name="p", logical_type=logical, physical_type=physical, options=options, **fields
    )


@pytest.mark.parametrize(
    ("prop", "expected"),
    [
        (_prop("string"), pa.string()),
        (_prop("integer"), pa.int64()),
        (_prop("integer", options={"format": "i8"}), pa.int8()),
        (_prop("integer", options={"format": "u32"}), pa.uint32()),
        (_prop("integer", options={"format": "u128"}), pa.decimal256(39, 0)),
        (_prop("number"), pa.float64()),
        (_prop("number", options={"format": "f32"}), pa.float32()),
        (_prop("boolean"), pa.bool_()),
        (_prop("date"), pa.date32()),
        (_prop("timestamp"), pa.timestamp("us")),
        (_prop("timestamp", options={"timezone": True}), pa.timestamp("us", tz="UTC")),
        (_prop("time"), pa.time64("us")),
        (_prop("array", items=_prop("integer")), pa.list_(pa.int64())),
        (_prop("array"), pa.list_(pa.null())),
        (
            _prop(
                "object",
                nested=(
                    Property(name="a", logical_type="string"),
                    Property(name="b", physical_name="B", logical_type="integer"),
                ),
            ),
            pa.struct([("a", pa.string()), ("B", pa.int64())]),
        ),
        (_prop("map"), pa.map_(pa.string(), pa.string())),
        (_prop("vector", options={"dimensions": 3}), pa.list_(pa.float32(), 3)),
        (
            _prop("vector", options={"dimensions": 2, "elementType": "float64"}),
            pa.list_(pa.float64(), 2),
        ),
        (_prop("vector"), pa.list_(pa.float32())),
        (_prop("string", "int16"), pa.int16()),
        (_prop("string", "VARCHAR(5)"), pa.string()),
        (_prop(None, "double"), pa.float64()),
    ],
)
def test_to_arrow_type_is_the_inverse_of_the_type_map(
    prop: Property, expected: pa.DataType
) -> None:
    assert to_arrow_type(prop) == expected


def test_to_arrow_type_of_a_property_without_a_type_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="no logicalType"):
        to_arrow_type(_prop(None))
    with pytest.raises(UsageError):
        to_arrow_type(_prop("quaternion"))


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("yyyy-MM-dd", "%Y-%m-%d"),
        ("yy/MM/dd", "%y/%m/%d"),
        ("yyyy-MM-dd'T'HH:mm:ss", "%Y-%m-%dT%H:%M:%S"),
        ("HH:mm:ss.SSS", "%H:%M:%S.%f"),
        ("dd.MM.yyyy HH:mm", "%d.%m.%Y %H:%M"),
        ("yyyy-MM-dd 'at' HH", "%Y-%m-%d at %H"),
        ("100% yyyy", "100%% %Y"),
        ("'it''s' yyyy", "it's %Y"),
        ("'50%' yyyy", "50%% %Y"),
        ("yyyy''MM", "%Y'%m"),
    ],
)
def test_jdk_patterns_translate_to_strptime(pattern: str, expected: str) -> None:
    assert jdk_to_strptime(pattern) == expected


@pytest.mark.parametrize(
    "pattern", ["MMM d, yyyy", "EEEE", "yyyy-MM-dd'T", "", "yyyy-MM-ddXXX", "ddd", "a", "hh:mm"]
)
def test_other_jdk_patterns_are_not_translated(pattern: str) -> None:
    assert jdk_to_strptime(pattern) is None


def test_a_translated_pattern_parses_with_arrow_and_python() -> None:
    fmt = jdk_to_strptime("yyyy-MM-dd'T'HH:mm:ss")
    assert fmt is not None

    parsed = pc.strptime(
        pa.array(["2026-11-21T01:02:03", "nope"]), format=fmt, unit="us", error_is_null=True
    )

    assert parsed.to_pylist() == [dt.datetime(2026, 11, 21, 1, 2, 3), None]
    assert dt.datetime.strptime("2026-11-21T01:02:03", fmt) == dt.datetime(2026, 11, 21, 1, 2, 3)


def test_integer_format_ranges_cover_every_width() -> None:
    assert INTEGER_FORMAT_RANGES["i8"] == (-128, 127)
    assert INTEGER_FORMAT_RANGES["u8"] == (0, 255)
    assert INTEGER_FORMAT_RANGES["i32"] == (-(2**31), 2**31 - 1)
    assert INTEGER_FORMAT_RANGES["u64"] == (0, 2**64 - 1)
    assert INTEGER_FORMAT_RANGES["i128"] == (-(2**127), 2**127 - 1)
    assert INTEGER_FORMAT_RANGES["u128"] == (0, 2**128 - 1)
    assert sorted(INTEGER_FORMAT_RANGES) == sorted(
        [f"i{w}" for w in (8, 16, 32, 64, 128)] + [f"u{w}" for w in (8, 16, 32, 64, 128)]
    )


def test_the_float32_format_range_is_the_largest_finite_float32() -> None:
    assert FLOAT32_MAX == pytest.approx(3.4028234663852886e38)
    assert pa.scalar(FLOAT32_MAX, pa.float32()).as_py() == FLOAT32_MAX


@pytest.mark.parametrize(
    ("raw", "real", "whole"),
    [
        (5, 5, 5),
        (5.0, 5.0, 5),
        (5.5, 5.5, None),
        (-3.0, -3.0, -3),
        (True, None, None),
        ("5", None, None),
        (None, None, None),
        (float("nan"), None, None),
        (float("inf"), None, None),
    ],
)
def test_contract_number_helpers(raw: Any, real: Any, whole: Any) -> None:
    assert real_number(raw) == real
    assert whole_number(raw) == whole
