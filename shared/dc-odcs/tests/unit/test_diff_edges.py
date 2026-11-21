"""The diff on odd but accepted shapes: no baseline, junk values, id-only and keyless items."""

from __future__ import annotations

from typing import Any

import pytest
from dc_odcs.diff import CLASSIFIER_VERSION, compare
from dc_odcs.diff.model import capped_value
from dc_odcs.diff.normalise import canon, same
from dc_odcs.diff.table import UNCLASSIFIED_CODE, classify_element

pytestmark = pytest.mark.unit


def _contract(**extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": "orders-contract",
        "version": "1.0.0",
    }
    document.update(extra)
    return document


def _object(**extra: Any) -> dict[str, Any]:
    return _contract(schema=[{"name": "orders", **extra}])


def _summary(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(c.code, c.cls, c.path) for c in compare(before, after).changes]


# --- the result itself ---------------------------------------------------------------------------


def test_no_baseline_compares_to_an_empty_result() -> None:
    result = compare(None, _contract(status="active"))
    assert result.changes == ()
    assert result.counts == {"breaking": 0, "warning": 0, "info": 0}
    assert result.changes_total == 0
    assert result.truncated is False
    assert result.classifier_version == CLASSIFIER_VERSION


def test_the_cel_binding_is_one_json_object_per_change() -> None:
    before = _object(properties=[{"name": "id", "logicalType": "string"}])
    after = _object(properties=[{"name": "id", "logicalType": "integer"}])
    assert compare(before, after).as_binding() == [
        {
            "code": "logical-type-changed",
            "class": "breaking",
            "element_kind": "property",
            "path": "schema.orders.properties.id",
            "pointer_before": "/schema/0/properties/0/logicalType",
            "pointer_after": "/schema/0/properties/0/logicalType",
            "attribute": "logicalType",
            "before": "string",
            "after": "integer",
        }
    ]


# --- recorded values -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"nested": 1}, None),
        ([{"nested": 1}], None),
        (["a", 1, 1.5, True, None], ["a", 1, 1.5, True, None]),
        ("x" * 500, "x" * 200),
    ],
)
def test_only_scalars_and_scalar_lists_are_recorded(value: Any, expected: Any) -> None:
    assert capped_value(value) == expected


def test_an_object_valued_type_is_classified_but_its_value_is_not_recorded() -> None:
    before = _object(properties=[{"name": "id", "logicalType": "string"}])
    after = _object(properties=[{"name": "id", "logicalType": {"nested": "x"}}])
    (change,) = compare(before, after).changes
    assert change.code == "logical-type-changed"
    assert change.before == "string"
    assert change.after is None


# --- canonical equality --------------------------------------------------------------------------


def test_canonical_form_keeps_booleans_apart_from_numbers_and_folds_whole_floats() -> None:
    assert canon({"b": True, "a": 10.0, "c": False}) == '{"a":10,"b":true,"c":false}'
    assert same(10, 10.0) is True
    assert same(True, 1) is False


def test_a_whole_float_and_its_integer_are_not_a_change() -> None:
    before = _object(properties=[{"name": "n", "logicalTypeOptions": {"flag": True, "x": 10}}])
    after = _object(properties=[{"name": "n", "logicalTypeOptions": {"flag": True, "x": 10.0}}])
    assert compare(before, after).changes == ()


# --- pairing of odd items ------------------------------------------------------------------------


def test_a_numeric_id_still_pairs_and_a_new_name_on_it_is_a_rename() -> None:
    before = _contract(schema=[{"name": "orders", "id": 7}])
    after = _contract(schema=[{"name": "sales", "id": 7}])
    assert _summary(before, after) == [("object-renamed", "breaking", "schema.sales")]


def test_a_one_element_target_list_keys_like_the_plain_target() -> None:
    before = _object(relationships=[{"from": "orders.id", "to": ["customers.id"]}])
    after = _object(relationships=[{"from": "orders.id", "to": "customers.id"}])
    changes = compare(before, after).changes
    assert [c.path for c in changes] == ['schema.orders.relationships["orders.id>customers.id"]']
    assert [c.code for c in changes] == ["relationship-retargeted"]


def test_a_relationship_without_a_target_pairs_by_content() -> None:
    before = _object(relationships=[{"type": "foreignKey"}])
    after = _object(relationships=[{"type": "foreignKey"}, {"type": "other"}])
    (change,) = compare(before, after).changes
    assert (change.code, change.cls) == ("relationship-added", "warning")
    assert change.path.startswith("schema.orders.relationships.#")
    assert change.pointer_after == "/schema/0/relationships/1"


def test_scalar_items_of_a_keyed_collection_pair_by_value_and_never_recurse() -> None:
    assert compare(_contract(servers=["prod"]), _contract(servers=["prod"])).changes == ()
    assert _summary(_contract(servers=["prod"]), _contract(servers=["dev"])) == [
        ("server-added", "info", "servers.dev"),
        ("server-removed", "breaking", "servers.prod"),
    ]


def test_an_item_with_only_an_id_is_keyed_by_that_id() -> None:
    before = _contract(schema=[{"id": "o1", "description": "old"}])
    after = _contract(schema=[{"id": "o1", "description": "new"}])
    assert _summary(before, after) == [("metadata-changed", "info", "schema.id=o1")]


def test_quality_rules_without_check_fields_are_removed_and_added() -> None:
    before = _object(quality=[{"description": "a"}])
    after = _object(quality=[{"description": "b"}])
    assert sorted(code for code, _, _ in _summary(before, after)) == [
        "quality-added",
        "quality-removed",
    ]


def test_scalar_quality_entries_are_keyed_by_their_text() -> None:
    assert _summary(_object(quality=["x"]), _object(quality=["y"])) == [
        ("quality-removed", "warning", "schema.orders.quality.x"),
        ("quality-added", "info", "schema.orders.quality.y"),
    ]


# --- absent and null values ----------------------------------------------------------------------


def test_null_on_both_sides_is_no_change() -> None:
    assert compare(_contract(domain=None), _contract(domain=None)).changes == ()


def test_a_null_team_and_no_team_are_the_same() -> None:
    assert compare(_contract(team=None), _contract()).changes == ()


def test_an_added_map_is_classified_as_an_added_property() -> None:
    before = _object(properties=[{"name": "attrs"}])
    after = _object(properties=[{"name": "attrs", "map": {"key": {"logicalType": "string"}}}])
    assert _summary(before, after) == [
        ("property-added", "info", "schema.orders.properties.attrs.map")
    ]


def test_an_element_kind_without_a_rule_is_an_unclassified_warning() -> None:
    rule = classify_element("not-a-kind", "added", {})
    assert (rule.code, rule.cls) == (UNCLASSIFIED_CODE, "warning")
