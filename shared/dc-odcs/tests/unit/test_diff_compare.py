"""VAL-02: the contract diff classifies removed, retyped and tightened elements (D-144, D-145)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from dc_odcs.diff import CLASSIFIER_VERSION, Change, compare

pytestmark = pytest.mark.unit


def _contract(*objects: dict[str, Any], **extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": "orders-contract",
        "version": "1.0.0",
        "schema": list(objects),
    }
    document.update(extra)
    return document


def _orders(*properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"name": "orders", "properties": list(properties), **extra}


def _one(changes: tuple[Change, ...]) -> Change:
    assert len(changes) == 1, changes
    return changes[0]


# --- Task 1 (tracer): a removed property is one breaking change ---------------------------------


def test_removed_property_is_one_breaking_change_with_name_keyed_path() -> None:
    before = _contract(_orders({"name": "id"}, {"name": "amount"}))
    after = _contract(_orders({"name": "id"}))
    result = compare(before, after)
    change = _one(result.changes)
    assert change.code == "property-removed"
    assert change.cls == "breaking"
    assert change.path == "schema.orders.properties.amount"
    assert change.pointer_before == "/schema/0/properties/1"
    assert change.pointer_after is None
    assert result.counts == {"breaking": 1, "warning": 0, "info": 0}
    assert result.classifier_version == CLASSIFIER_VERSION == "odcs-compat-1"


def test_removed_object_is_one_change_at_the_object_root() -> None:
    before = _contract(_orders({"name": "id"}, {"name": "amount"}), {"name": "customers"})
    after = _contract({"name": "customers"})
    change = _one(compare(before, after).changes)
    assert (change.code, change.cls, change.path) == ("object-removed", "breaking", "schema.orders")


def test_optional_property_added_is_info_and_required_one_is_breaking() -> None:
    before = _contract(_orders({"name": "id"}))
    optional = _contract(_orders({"name": "id"}, {"name": "note"}))
    required = _contract(_orders({"name": "id"}, {"name": "note", "required": True}))
    added = _one(compare(before, optional).changes)
    assert (added.code, added.cls) == ("property-added", "info")
    assert added.pointer_after == "/schema/0/properties/1"
    hard = _one(compare(before, required).changes)
    assert (hard.code, hard.cls) == ("required-property-added", "breaking")


def test_logical_type_change_is_breaking_and_carries_allow_listed_values() -> None:
    before = _contract(_orders({"name": "amount", "logicalType": "string"}))
    after = _contract(_orders({"name": "amount", "logicalType": "integer"}))
    change = _one(compare(before, after).changes)
    assert (change.code, change.cls, change.attribute) == (
        "logical-type-changed",
        "breaking",
        "logicalType",
    )
    assert (change.before, change.after) == ("string", "integer")
    assert change.path == "schema.orders.properties.amount"


def test_description_change_is_info_and_leaks_no_text() -> None:
    before = _contract(_orders({"name": "id", "description": "secret old text"}))
    after = _contract(_orders({"name": "id", "description": "secret new text"}))
    change = _one(compare(before, after).changes)
    assert (change.code, change.cls) == ("metadata-changed", "info")
    assert change.before is None
    assert change.after is None


def test_documents_equal_except_the_declared_version_have_no_changes() -> None:
    before = _contract(_orders({"name": "id"}))
    after = copy.deepcopy(before)
    after["version"] = "2.0.0"
    assert compare(before, after).changes == ()


def test_unmatched_difference_is_an_unclassified_warning() -> None:
    before = _contract(_orders({"name": "id", "mysteryAttribute": 1}))
    after = _contract(_orders({"name": "id", "mysteryAttribute": 2}))
    change = _one(compare(before, after).changes)
    assert (change.code, change.cls) == ("unclassified", "warning")


# --- Task 2: tiered pairing, every keyed collection, bounds and the semver verdict ---------------


def _codes(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, str]]:
    return [(c.code, c.cls) for c in compare(before, after).changes]


def test_adding_an_id_to_an_existing_property_is_not_a_change() -> None:
    before = _contract(_orders({"name": "amount"}))
    after = _contract(_orders({"name": "amount", "id": "p-amount"}))
    assert compare(before, after).changes == ()


def test_same_id_with_another_name_is_one_breaking_rename() -> None:
    before = _contract(_orders({"name": "amount", "id": "p-1"}))
    after = _contract(_orders({"name": "total", "id": "p-1"}))
    change = _one(compare(before, after).changes)
    assert (change.code, change.cls) == ("property-renamed", "breaking")


def test_object_rename_through_a_stable_id_is_breaking() -> None:
    before = _contract({"name": "orders", "id": "o-1"})
    after = _contract({"name": "purchases", "id": "o-1"})
    change = _one(compare(before, after).changes)
    assert (change.code, change.cls) == ("object-renamed", "breaking")


def test_different_ids_with_the_same_name_are_a_warning() -> None:
    before = _contract(_orders({"name": "amount", "id": "p-1"}))
    after = _contract(_orders({"name": "amount", "id": "p-2"}))
    assert _codes(before, after) == [("id-changed", "warning")]


def test_rename_without_an_id_is_remove_plus_add() -> None:
    before = _contract(_orders({"name": "amount"}))
    after = _contract(_orders({"name": "total"}))
    assert sorted(c for c, _ in _codes(before, after)) == ["property-added", "property-removed"]


def _rich() -> dict[str, Any]:
    return _contract(
        {"name": "orders", "properties": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
        {"name": "customers", "properties": [{"name": "x"}]},
        servers=[
            {"server": "prod", "type": "postgres"},
            {"server": "dev", "type": "postgres"},
        ],
        slaProperties=[
            {"property": "latency", "element": "orders", "value": 5},
            {"property": "latency", "element": "customers", "value": 9},
        ],
        roles=[{"role": "reader"}, {"role": "writer"}],
        team={"members": [{"username": "ann"}, {"username": "bob"}]},
        tags=["a", "b", "c"],
    )


def test_reordering_every_keyed_collection_is_not_a_change() -> None:
    before = _rich()
    after = copy.deepcopy(before)
    after["schema"].reverse()
    after["schema"][0]["properties"].reverse()
    after["servers"].reverse()
    after["slaProperties"].reverse()
    after["roles"].reverse()
    after["team"]["members"].reverse()
    after["tags"].reverse()
    assert compare(before, after).changes == ()


def test_quality_without_id_or_name_pairs_by_content_then_by_check_fields() -> None:
    first = {"type": "library", "metric": "nullValues", "mustBe": 0}
    second = {"type": "library", "metric": "rowCount", "mustBeGreaterThan": 10}
    before = _contract(_orders({"name": "id", "quality": [first, second]}))
    reordered = _contract(_orders({"name": "id", "quality": [second, first]}))
    assert compare(before, reordered).changes == ()
    tuned = _contract(
        _orders({"name": "id", "quality": [{**first, "mustBe": 3}, second]}),
    )
    change = _one(compare(before, tuned).changes)
    assert (change.code, change.cls) == ("quality-changed", "warning")


def test_quality_added_is_info_and_removed_is_a_warning() -> None:
    rule = {"type": "library", "metric": "nullValues", "mustBe": 0}
    bare = _contract(_orders({"name": "id"}))
    checked = _contract(_orders({"name": "id", "quality": [rule]}))
    assert _codes(bare, checked) == [("quality-added", "info")]
    assert _codes(checked, bare) == [("quality-removed", "warning")]


def test_team_array_form_and_object_form_compare_equal() -> None:
    members = [{"username": "ann", "role": "owner"}, {"username": "bob"}]
    before = _contract(_orders({"name": "id"}), team=members)
    after = _contract(_orders({"name": "id"}), team={"name": "core", "members": members})
    changes = compare(before, after).changes
    assert [c.attribute for c in changes] == ["name"]
    swapped = compare(after, before).changes
    assert [c.attribute for c in swapped] == ["name"]
    plain = _contract(_orders({"name": "id"}), team={"members": list(reversed(members))})
    assert compare(before, plain).changes == ()


def test_sla_properties_are_keyed_by_property_and_element() -> None:
    before = _contract(
        _orders({"name": "id"}),
        slaProperties=[
            {"property": "latency", "element": "orders", "value": 5, "unit": "s"},
            {"property": "latency", "element": "customers", "value": 9, "unit": "s"},
        ],
    )
    after = copy.deepcopy(before)
    after["slaProperties"][1]["value"] = 12
    change = _one(compare(before, after).changes)
    assert (change.code, change.cls) == ("sla-changed", "warning")
    assert change.path == "slaProperties.latency@customers"


def test_duplicate_names_never_crash_and_are_suffixed_in_document_order() -> None:
    before = _contract(_orders({"name": "amount"}, {"name": "amount"}))
    after = _contract(_orders({"name": "amount"}))
    change = _one(compare(before, after).changes)
    assert change.code == "property-removed"
    assert change.path == "schema.orders.properties.amount~2"
    triple = _contract(_orders({"name": "amount"}, {"name": "amount"}, {"name": "amount"}))
    assert [c.path for c in compare(before, triple).changes] == [
        "schema.orders.properties.amount~3"
    ]


def test_paths_bracket_quote_a_segment_with_a_dot() -> None:
    before = _contract({"name": "sales.orders", "properties": [{"name": "id"}]})
    after = _contract({"name": "sales.orders", "properties": []})
    change = _one(compare(before, after).changes)
    assert change.path == 'schema["sales.orders"].properties.id'


def test_server_removed_is_breaking_and_connection_change_is_a_warning() -> None:
    before = _contract(_orders({"name": "id"}), servers=[{"server": "prod", "type": "postgres"}])
    gone = _contract(_orders({"name": "id"}))
    assert _codes(before, gone) == [("server-removed", "breaking")]
    moved = _contract(_orders({"name": "id"}), servers=[{"server": "prod", "type": "mysql"}])
    change = _one(compare(before, moved).changes)
    assert (change.code, change.cls, change.before, change.after) == (
        "server-changed",
        "warning",
        "postgres",
        "mysql",
    )


def test_enum_values_removed_breaking_added_warning() -> None:
    def prop(*values: str) -> dict[str, Any]:
        return _contract(_orders({"name": "s", "enum": [{"value": v} for v in values]}))

    assert _codes(prop("a", "b"), prop("a")) == [("enum-value-removed", "breaking")]
    assert _codes(prop("a"), prop("a", "b")) == [("enum-value-added", "warning")]
    none = _contract(_orders({"name": "s"}))
    assert _codes(none, prop("a", "b")) == [("enum-added", "breaking")]
    assert _codes(prop("a", "b"), none) == [("enum-removed", "warning")]


def test_constraint_bounds_are_classified_by_direction() -> None:
    def prop(**options: Any) -> dict[str, Any]:
        return _contract(_orders({"name": "s", "logicalTypeOptions": options}))

    tight = compare(prop(maxLength=50), prop(maxLength=40)).changes
    assert [(c.code, c.cls, c.attribute, c.before, c.after) for c in tight] == [
        ("constraint-tightened", "breaking", "maxLength", 50, 40)
    ]
    assert _codes(prop(maxLength=50), prop(maxLength=60)) == [("constraint-widened", "warning")]
    assert _codes(prop(minimum=1), prop(minimum=0)) == [("constraint-widened", "warning")]
    assert _codes(prop(), prop(minimum=1)) == [("constraint-tightened", "breaking")]
    assert _codes(prop(maximum=10), prop(maximum=10.0)) == []
    assert _codes(prop(pattern="^a"), prop(pattern="^a$")) == [("pattern-changed", "warning")]
    assert _codes(prop(), prop(pattern="^a")) == [("pattern-added", "breaking")]


def test_object_required_list_changes_are_breaking_and_tags_are_sets() -> None:
    def obj(required: list[str], tags: list[str]) -> dict[str, Any]:
        return _contract({"name": "o", "tags": tags, "logicalTypeOptions": {"required": required}})

    assert compare(obj(["a", "b"], ["x", "y"]), obj(["b", "a"], ["y", "x"])).changes == ()
    assert _codes(obj(["a"], []), obj(["a", "b"], [])) == [("required-added", "breaking")]
    assert _codes(obj([], ["x"]), obj([], ["x", "y"])) == [("metadata-changed", "info")]


def test_items_and_map_children_compare_as_properties() -> None:
    def arr(item_type: str) -> dict[str, Any]:
        return _contract(
            _orders({"name": "tags", "logicalType": "array", "items": {"logicalType": item_type}})
        )

    change = _one(compare(arr("string"), arr("integer")).changes)
    assert change.code == "logical-type-changed"
    assert change.path == "schema.orders.properties.tags.items"
    gone = _contract(_orders({"name": "tags", "logicalType": "array"}))
    assert _codes(arr("string"), gone) == [("property-removed", "breaking")]


def test_changes_are_ordered_by_path_code_attribute() -> None:
    before = _contract(_orders({"name": "a", "logicalType": "string"}, {"name": "b"}))
    after = _contract(_orders({"name": "a", "logicalType": "integer"}), status="active")
    first = compare(before, after)
    again = compare(copy.deepcopy(before), copy.deepcopy(after))
    assert first == again
    keys = [(c.path, c.code, c.attribute or "") for c in first.changes]
    assert keys == sorted(keys)


def test_empty_documents_and_self_comparison_have_no_changes() -> None:
    assert compare({}, {}).changes == ()
    document = _rich()
    assert compare(document, document).changes == ()


def _deep(levels: int, leaf: str) -> Any:
    value: Any = leaf
    for _ in range(levels):
        value = {"k": value}
    return value


def _with_value(value: Any) -> dict[str, Any]:
    return _contract(
        _orders({"name": "id", "customProperties": [{"property": "p", "value": value}]})
    )


def test_compare_never_recurses_and_does_not_mutate_its_inputs() -> None:
    # Far deeper than the interpreter recursion limit: ingest caps real documents at 128 levels,
    # but the classifier must not depend on that cap to stay up.
    before = _with_value(_deep(5000, "a"))
    after = _with_value(_deep(5000, "b"))
    unchanged = _with_value(_deep(5000, "a"))
    assert [c.code for c in compare(before, after).changes] == ["metadata-changed"]
    assert compare(before, unchanged).changes == ()
    props = before["schema"][0]["properties"][0]
    assert list(props) == ["name", "customProperties"]
    assert list(before) == ["apiVersion", "kind", "id", "version", "schema"]


def test_top_level_names_are_exported() -> None:
    import dc_odcs

    for name in ("compare", "required_bump", "DiffResult", "Change", "BumpVerdict"):
        assert hasattr(dc_odcs, name), name
    assert dc_odcs.CLASSIFIER_VERSION == "odcs-compat-1"


# --- bounds ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "verdict"),
    [
        ({"maxLength": 50}, {"maxLength": 40}, "tightened"),
        ({"maxLength": 50}, {"maxLength": 60}, "widened"),
        ({}, {"minimum": 1}, "tightened"),
        ({"maximum": 5}, {}, "widened"),
        ({"exclusiveMaximum": 10}, {"maximum": 10}, "widened"),
        ({"maximum": 10}, {"exclusiveMaximum": 10}, "tightened"),
        ({"maximum": 10}, {"maximum": 10.0}, "equal"),
        ({"minimum": 1, "maximum": 9}, {"minimum": 2, "maximum": 20}, "both"),
        ({"minItems": 1}, {"minItems": 3}, "tightened"),
        ({"minimum": "2024-01-01"}, {"minimum": "2025-01-01"}, "both"),
        ({"minimum": "2024-01-01"}, {"minimum": "2024-01-01"}, "equal"),
    ],
)
def test_bounds_interval_comparison(
    before: dict[str, Any], after: dict[str, Any], verdict: str
) -> None:
    from dc_odcs.diff.bounds import compare_bounds

    assert compare_bounds(before, after) == verdict


# --- semver verdict (D-146) --------------------------------------------------------------------


def _breaking() -> Any:
    return compare(_contract(_orders({"name": "a"})), _contract(_orders()))


def test_no_baseline_means_no_check() -> None:
    from dc_odcs.diff import required_bump

    verdict = required_bump(None, declared="1.0.0", stable_major_floor=None)
    assert (verdict.status, verdict.required_bump, verdict.bump_ok) == ("no-baseline", "none", True)
    assert verdict.smallest_passing is None


@pytest.mark.parametrize(
    ("declared", "floor", "ok", "smallest"),
    [
        ("1.1.0", 1, False, "2.0.0"),
        ("2.0.0", 1, True, None),
        ("2.0.0-rc.2", 1, True, None),
        ("1.0.0-rc.1", 1, False, "2.0.0"),
        ("0.4.0", 0, False, "1.0.0"),
        ("1.0.0", 0, True, None),
        ("0.1.0", None, True, None),
        ("not-semver", 1, False, "2.0.0"),
    ],
)
def test_breaking_change_needs_a_major_above_the_stable_floor(
    declared: str, floor: int | None, ok: bool, smallest: str | None
) -> None:
    from dc_odcs.diff import required_bump

    verdict = required_bump(_breaking(), declared=declared, stable_major_floor=floor)
    assert verdict.status == "compared"
    assert verdict.required_bump == "major"
    assert verdict.bump_ok is ok
    assert verdict.smallest_passing == smallest


def test_non_breaking_changes_only_advise_a_bump() -> None:
    from dc_odcs.diff import required_bump

    base = _contract(_orders({"name": "id"}), servers=[{"server": "s", "type": "postgres"}])
    warn = copy.deepcopy(base)
    warn["servers"][0]["type"] = "mysql"
    info = copy.deepcopy(base)
    info["schema"][0]["description"] = "text"
    for after, bump in ((warn, "minor"), (info, "patch"), (copy.deepcopy(base), "none")):
        verdict = required_bump(compare(base, after), declared="1.0.1", stable_major_floor=1)
        assert (verdict.required_bump, verdict.bump_ok) == (bump, True)
