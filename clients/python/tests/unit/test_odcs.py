"""The read-only ODCS accessors: loading, tolerant shapes and object lookup."""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml
from support.odcs import contract_doc, load_fixture_contract

from contractum import odcs
from contractum.errors import UsageError
from contractum.odcs import find_object, load_document, schema_objects

YAML_DATES = """
id: x
version: 1.0.0
contractCreatedTs: 2026-11-21T09:30:00Z
effective: 2026-11-21
schema: []
"""


def test_yaml_dates_stay_strings() -> None:
    document = load_document(YAML_DATES)

    assert document["effective"] == "2026-11-21"
    assert document["contractCreatedTs"] == "2026-11-21T09:30:00Z"
    assert yaml.safe_load(YAML_DATES)["effective"] != "2026-11-21"


def test_yaml_and_json_renderings_give_equal_documents() -> None:
    from_yaml = load_fixture_contract("3.2.0")
    as_json = json.dumps(from_yaml)

    assert load_document(as_json) == from_yaml
    assert load_document(as_json.encode(), media_type="application/json") == from_yaml
    assert load_document("  \n" + as_json) == from_yaml


def test_bytes_with_a_bom_load() -> None:
    assert load_document(b"\xef\xbb\xbfid: x\n") == {"id": "x"}


@pytest.mark.parametrize(
    "source",
    [
        "[1, 2]",
        "just text",
        "",
        "- a\n- b\n",
        "{1: 2}",
        '{"a": NaN}',
        "a: .inf\n",
        "a: !!python/tuple [1]\n",
        "a: !!timestamp 2026-11-21\n",
        "a: {1: 2}\n",
        "a: [unclosed\n",
        '{"a": ',
        "[" * 100000,
    ],
)
def test_documents_that_are_not_json_objects_are_refused(source: str) -> None:
    with pytest.raises(UsageError):
        load_document(source)


def test_non_utf8_input_is_refused() -> None:
    with pytest.raises(UsageError, match="UTF-8"):
        load_document(b"\xff\xfe\x00")


def test_oversized_input_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(odcs, "MAX_DOCUMENT_BYTES", 16)

    with pytest.raises(UsageError, match="larger than"):
        load_document("a: " + "b" * 32)


def test_an_alias_bomb_is_stopped_by_the_node_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(odcs, "MAX_DOCUMENT_NODES", 50)
    bomb = "a: &a [x, x, x, x]\nb: &b [*a, *a, *a, *a]\nc: [*b, *b, *b, *b]\nd: [*b, *b, *b, *b]\n"

    with pytest.raises(UsageError):
        load_document(bomb)


def test_fixture_objects_read_with_every_accessor() -> None:
    (orders,) = schema_objects(contract_doc())
    props = {prop.name: prop for prop in orders.properties}

    assert orders.name == "orders"
    assert list(props)[:3] == ["order_id", "customer_email", "amount"]
    order_id = props["order_id"]
    assert (order_id.required, order_id.unique, order_id.primary_key) == (True, True, True)
    assert order_id.primary_key_position == 1
    assert order_id.options["pattern"] == "^ORD-[0-9]{6}$"
    assert props["customer_email"].classification == "restricted"
    assert props["status"].enum_values == ("new", "paid", "shipped")
    assert props["tags"].items is not None
    assert props["tags"].items.logical_type == "string"
    assert [nested.name for nested in props["shipping"].nested] == ["city", "zip"]
    assert props["amount"].quality[0].metric == "nullValues"
    assert props["amount"].quality[0].operator == "mustBe"
    assert props["amount"].quality[0].threshold == 0
    assert props["amount"].quality[0].severity == "warning"
    assert [rule.type for rule in orders.quality] == ["library", "library", "sql", "text"]
    assert orders.quality[1].arguments == {"properties": ["order_id"]}
    assert order_id.source_name == "order_id"


def test_older_shapes_read_like_v3_2_0() -> None:
    document: dict[str, Any] = {
        "schema": [
            {
                "name": "t",
                "physicalName": "T_TABLE",
                "properties": [
                    {
                        "name": "state",
                        "physicalName": "STATE",
                        "logicalType": "string",
                        "enum": ["a", "b"],
                        "classification": 3,
                        "quality": [
                            {
                                "rule": "invalidValues",
                                "mustBeLessThan": 5,
                                "validValues": ["a"],
                                "arguments": {"pattern": "x"},
                                "unit": "percent",
                                "dimension": "conformity",
                                "engine": "soda",
                                "id": "q1",
                                "name": "n",
                            }
                        ],
                    },
                    {"physicalName": "ONLY_PHYSICAL"},
                    {"logicalType": "string"},
                ],
            },
            {"properties": []},
            "not an object",
        ]
    }

    first, second = schema_objects(document)
    state, only_physical, nameless = first.properties
    rule = state.quality[0]

    assert state.enum_values == ("a", "b")
    assert state.classification == "3"
    assert state.source_name == "STATE"
    assert rule.metric == "invalidValues"
    assert rule.operator == "mustBeLessThan"
    assert rule.threshold == 5
    assert rule.arguments == {"validValues": ["a"], "pattern": "x"}
    assert (rule.unit, rule.dimension, rule.engine, rule.id, rule.name) == (
        "percent",
        "conformity",
        "soda",
        "q1",
        "n",
    )
    assert only_physical.name == "ONLY_PHYSICAL"
    assert nameless.name == "properties[2]"
    assert second.name == "schema[1]"


def test_readers_ignore_values_of_the_wrong_shape() -> None:
    document = {
        "schema": [
            {
                "name": "t",
                "properties": [
                    {"name": "p", "enum": "x", "quality": "x", "required": "yes", "items": 3},
                    {"name": "q", "primaryKeyPosition": True, "classification": True},
                ],
                "quality": [{"type": "text"}, 5],
            }
        ],
    }

    (obj,) = schema_objects(document)
    p, q = obj.properties

    assert p.enum_values is None
    assert p.quality == ()
    assert p.required is False
    assert p.items is None
    assert q.primary_key_position is None
    assert q.classification is None
    assert len(obj.quality) == 1
    assert schema_objects({"schema": "nope"}) == ()


def test_find_object_by_name_physical_name_or_the_only_one() -> None:
    document = {
        "schema": [
            {"name": "a", "physicalName": "A_PHYS"},
            {"name": "b"},
        ]
    }

    assert find_object(document, "a").name == "a"
    assert find_object(document, "A_PHYS").name == "a"
    assert find_object({"schema": [{"name": "only"}]}).name == "only"


def test_find_object_errors_name_the_choices() -> None:
    document = {"schema": [{"name": "a"}, {"name": "b"}]}

    with pytest.raises(UsageError, match="a, b") as several:
        find_object(document)
    assert several.value.hint is not None
    assert "--object" in several.value.hint
    with pytest.raises(UsageError, match="no schema object 'zzz'"):
        find_object(document, "zzz")
    with pytest.raises(UsageError, match="no schema objects"):
        find_object({})
