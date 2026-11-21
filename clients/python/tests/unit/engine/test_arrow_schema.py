"""contract_to_arrow_schema: the Arrow schema a contract implies, proven against the type checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from support.odcs import contract_doc

from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.arrow_schema import contract_to_arrow_schema
from contractum.engine.backend import RunOptions
from contractum.engine.plan import PlanOptions, plan
from contractum.errors import UsageError
from contractum.odcs import find_object, load_document
from contractum.testing import TestOptions, run_test

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "contracts"
_FIXTURE_NAMES = ("orders-v3.0.2", "orders-v3.1.0", "orders-v3.2.0", "orders-nested")


def _document(name: str) -> dict[str, Any]:
    return load_document((FIXTURES / f"{name}.yaml").read_bytes())


def _contract(*properties: dict[str, Any]) -> dict[str, Any]:
    return {"schema": [{"name": "t", "properties": list(properties)}]}


def _type(prop: dict[str, Any]) -> pa.DataType:
    return contract_to_arrow_schema(_contract({"name": "p", **prop})).field("p").type


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_a_table_built_with_the_schema_passes_every_type_check(name: str) -> None:
    document = _document(name)
    schema = contract_to_arrow_schema(document)
    obj = find_object(document)
    schema_plan = plan(obj, PlanOptions(schema_only=True))
    empty = schema.empty_table()
    one_row = pa.Table.from_pylist([{}], schema=schema)

    assert schema.names == [prop.source_name for prop in obj.properties]
    assert schema_plan.checks
    for table in (empty, one_row):
        outcomes = ArrowBackend(table).evaluate(schema_plan.checks, RunOptions())
        assert {outcome.status for outcome in outcomes} == {"passed"}


def test_the_v3_2_0_fixture_schema_is_the_expected_one() -> None:
    schema = contract_to_arrow_schema(contract_doc())

    assert schema.field("order_id").type == pa.string()
    assert schema.field("amount").type == pa.float64()
    assert schema.field("quantity").type == pa.int64()
    assert schema.field("created_at").type == pa.timestamp("us")
    assert schema.field("order_date").type == pa.date32()
    assert schema.field("tags").type == pa.list_(pa.string())
    shipping = schema.field("shipping").type
    assert [field.name for field in shipping] == ["city", "zip"]
    assert not shipping.field("city").nullable
    assert shipping.field("zip").nullable


def test_a_field_is_nullable_unless_the_property_is_required() -> None:
    schema = contract_to_arrow_schema(contract_doc())

    assert not schema.field("order_id").nullable
    assert schema.field("customer_email").nullable


def test_nested_arrays_and_objects_become_lists_and_structs() -> None:
    schema = contract_to_arrow_schema(_document("orders-nested"))

    lines = schema.field("lines").type
    assert pa.types.is_list(lines)
    assert [field.name for field in lines.value_type] == ["sku", "qty"]
    assert not lines.value_type.field("sku").nullable
    assert schema.field("shipping").type.field("geo").type == pa.struct(
        [pa.field("lat", pa.float64())]
    )
    assert schema.field("tags").type == pa.list_(pa.string())


@pytest.mark.parametrize(
    ("prop", "expected"),
    [
        ({"logicalType": "integer"}, pa.int64()),
        ({"logicalType": "integer", "logicalTypeOptions": {"format": "i32"}}, pa.int32()),
        ({"logicalType": "integer", "logicalTypeOptions": {"format": "u16"}}, pa.uint16()),
        ({"logicalType": "number"}, pa.float64()),
        ({"logicalType": "number", "logicalTypeOptions": {"format": "f32"}}, pa.float32()),
        ({"logicalType": "string"}, pa.string()),
        ({"logicalType": "boolean"}, pa.bool_()),
        ({"logicalType": "date"}, pa.date32()),
        ({"logicalType": "timestamp"}, pa.timestamp("us")),
        (
            {"logicalType": "timestamp", "logicalTypeOptions": {"timezone": True}},
            pa.timestamp("us", tz="UTC"),
        ),
        ({"logicalType": "time"}, pa.time64("us")),
        ({"logicalType": "array", "items": {"logicalType": "integer"}}, pa.list_(pa.int64())),
        ({"logicalType": "array"}, pa.list_(pa.null())),
        (
            {"logicalType": "object", "properties": [{"name": "a", "logicalType": "boolean"}]},
            pa.struct([pa.field("a", pa.bool_())]),
        ),
    ],
)
def test_the_logical_type_gives_the_default_arrow_type(prop: dict[str, Any], expected: Any) -> None:
    assert _type(prop) == expected


@pytest.mark.parametrize(
    ("physical", "expected"),
    [
        ("int32", pa.int32()),
        ("double", pa.float64()),
        ("timestamp[ms]", pa.timestamp("ms")),
        ("VARCHAR(20)", pa.string()),
        ("DECIMAL(12,2)", pa.decimal128(12, 2)),
        ("TIMESTAMP_TZ", pa.timestamp("us", tz="UTC")),
    ],
)
def test_a_mappable_physical_type_wins_over_the_logical_type(
    physical: str, expected: pa.DataType
) -> None:
    assert _type({"logicalType": "string", "physicalType": physical}) == expected


def test_an_unmappable_physical_type_falls_back_to_the_logical_type() -> None:
    assert _type({"logicalType": "integer", "physicalType": "GEOGRAPHY"}) == pa.int64()


def test_physical_name_names_the_field() -> None:
    document = _contract({"name": "id", "physicalName": "ID", "logicalType": "integer"})

    assert contract_to_arrow_schema(document).names == ["ID"]


def test_a_property_without_a_mappable_type_is_a_usage_error() -> None:
    with pytest.raises(UsageError) as raised:
        contract_to_arrow_schema(_contract({"name": "mystery"}))

    assert "mystery" in str(raised.value)


def test_the_object_is_chosen_by_name() -> None:
    document = {
        "schema": [
            {"name": "a", "properties": [{"name": "x", "logicalType": "integer"}]},
            {
                "name": "b",
                "physicalName": "bee",
                "properties": [{"name": "y", "logicalType": "string"}],
            },
        ]
    }

    assert contract_to_arrow_schema(document, "a").names == ["x"]
    assert contract_to_arrow_schema(document, "bee").names == ["y"]
    with pytest.raises(UsageError):
        contract_to_arrow_schema(document)


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_a_schema_only_run_on_the_contracts_own_schema_passes(name: str) -> None:
    document = _document(name)

    report = run_test(
        document, contract_to_arrow_schema(document), options=TestOptions(schema_only=True)
    )

    assert report.ok
    assert report.summary.failed == 0
    assert report.summary.warning == 0
