"""Element and server reference extraction and resolution (VAL-04, VAL-05, D-153)."""

from __future__ import annotations

from typing import Any

import pytest
from dc_odcs import reference_inputs, resolve_internal

pytestmark = pytest.mark.unit


def contract(**extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": [
            {
                "name": "orders",
                "id": "o1",
                "properties": [
                    {"name": "id", "id": "p0"},
                    {"name": "amount", "id": "p1"},
                    {
                        "name": "address",
                        "id": "p2",
                        "properties": [{"name": "city", "id": "p3"}],
                    },
                ],
            },
            {"name": "customers", "properties": [{"name": "id"}, {"name": "region"}]},
        ],
        "servers": [{"server": "prod", "type": "postgres"}],
    }
    document.update(extra)
    return document


def elements(document: dict[str, Any]) -> dict[str, str]:
    return resolve_internal(document, reference_inputs(document))["elements"]


def servers(document: dict[str, Any]) -> dict[str, str]:
    return resolve_internal(document, reference_inputs(document))["servers"]


def test_an_sla_element_is_found_by_table_and_column_name() -> None:
    document = contract(slaProperties=[{"property": "freshness", "element": "orders.amount"}])
    assert elements(document) == {"orders.amount": "found"}


def test_a_missing_column_is_not_found() -> None:
    document = contract(slaProperties=[{"property": "freshness", "element": "orders.nope"}])
    assert elements(document) == {"orders.nope": "not_found"}


def test_comma_separated_elements_become_one_key_each() -> None:
    document = contract(slaProperties=[{"property": "x", "element": "orders.amount, orders.id"}])
    assert elements(document) == {"orders.amount": "found", "orders.id": "found"}


def test_the_deprecated_default_element_is_extracted() -> None:
    document = contract(slaDefaultElement="orders.gone")
    assert elements(document) == {"orders.gone": "not_found"}


def test_a_nested_property_resolves_through_its_parents() -> None:
    document = contract(
        slaProperties=[{"property": "x", "element": "orders.address.city"}],
        relationships=[{"from": "orders.address.zip", "to": "customers.id"}],
    )
    assert elements(document) == {
        "orders.address.city": "found",
        "orders.address.zip": "not_found",
        "customers.id": "found",
    }


def test_an_external_file_prefix_is_unverifiable() -> None:
    document = contract(slaProperties=[{"property": "x", "element": "https://x/y.yaml#/schema/a"}])
    assert elements(document) == {"https://x/y.yaml#/schema/a": "unverifiable"}


def test_fully_qualified_references_resolve_by_id() -> None:
    document = contract(
        slaProperties=[
            {"property": "x", "element": "schema/o1/properties/p1"},
            {"property": "y", "element": "schema/o1/properties/zzz"},
            {"property": "z", "element": "schema/nope/properties/p1"},
            {"property": "w", "element": "schema/o1/properties/p2/properties/p3"},
        ]
    )
    assert elements(document) == {
        "schema/o1/properties/p1": "found",
        "schema/o1/properties/zzz": "not_found",
        "schema/nope/properties/p1": "not_found",
        "schema/o1/properties/p2/properties/p3": "found",
    }


def test_text_that_is_no_element_at_all_is_unverifiable_never_not_found() -> None:
    document = contract(slaProperties=[{"property": "x", "element": "SELECT 1 FROM x"}])
    assert elements(document) == {"SELECT 1 FROM x": "unverifiable"}


def test_a_relationship_to_a_list_gives_a_key_per_entry() -> None:
    document = contract()
    document["schema"][0]["relationships"] = [
        {"from": "orders.id", "to": ["customers.id", "customers.region"]}
    ]
    assert elements(document) == {
        "orders.id": "found",
        "customers.id": "found",
        "customers.region": "found",
    }


def test_a_property_level_relationship_is_extracted() -> None:
    document = contract()
    document["schema"][0]["properties"][0]["relationships"] = [{"to": "customers.id"}]
    assert elements(document) == {"customers.id": "found"}


def test_quality_properties_are_names_inside_the_checked_object() -> None:
    document = contract()
    document["schema"][0]["quality"] = [
        {"type": "library", "arguments": {"properties": ["id", "missing"]}}
    ]
    assert elements(document) == {"id": "found", "missing": "not_found"}


def test_a_server_custom_property_must_match_a_declared_server() -> None:
    document = contract()
    document["schema"][0]["customProperties"] = [{"property": "server", "value": "prod"}]
    document["schema"][1]["customProperties"] = [
        {"property": "server", "value": ["staging", "prod"]}
    ]
    assert servers(document) == {"prod": "found", "staging": "not_found"}


def test_a_contract_without_the_convention_has_nothing_to_check() -> None:
    document = contract()
    inputs = reference_inputs(document)
    assert inputs.server_refs == ()
    assert resolve_internal(document, inputs) == {"elements": {}, "servers": {}}


def test_the_team_name_and_members_of_both_forms_are_collected() -> None:
    as_object = contract(
        team={"name": "payments", "members": [{"username": "alice"}, {"role": "x"}]}
    )
    as_list = contract(team=[{"username": "bob"}, {"username": "alice"}])
    assert reference_inputs(as_object).team_name == "payments"
    assert reference_inputs(as_object).user_names == ("alice",)
    assert reference_inputs(as_list).team_name is None
    assert reference_inputs(as_list).user_names == ("bob", "alice")


def test_every_reference_is_a_key_and_the_overflow_is_unverifiable() -> None:
    document = contract(
        slaProperties=[{"property": "x", "element": f"orders.c{i}"} for i in range(250)]
    )
    result = resolve_internal(document, reference_inputs(document), max_refs=200)["elements"]
    assert len(result) == 250
    assert sum(1 for state in result.values() if state == "not_found") == 200
    assert sum(1 for state in result.values() if state == "unverifiable") == 50


@pytest.mark.parametrize("junk", [None, 1, "x", [], {"schema": "x", "slaProperties": [1, None]}])
def test_any_shape_of_document_is_accepted(junk: Any) -> None:
    inputs = reference_inputs(junk)
    assert resolve_internal(junk, inputs) == {"elements": {}, "servers": {}}


def test_the_values_are_only_the_tri_state() -> None:
    document = contract(
        slaProperties=[{"property": "x", "element": "orders.id,a.b,https://q/r.yaml#/s"}]
    )
    assert set(elements(document).values()) <= {"found", "not_found", "unverifiable"}


def test_blank_parts_and_non_string_elements_are_skipped() -> None:
    document = contract(
        slaProperties=[
            {"property": "x", "element": "orders.id, ,orders.amount"},
            {"property": "y", "element": 5},
        ]
    )
    assert elements(document) == {"orders.id": "found", "orders.amount": "found"}


def test_junk_server_conventions_and_quality_arguments_are_ignored() -> None:
    document = contract()
    document["schema"][0]["customProperties"] = [
        "junk",
        {"property": "owner", "value": "prod"},
        {"property": "server", "value": ["", 5, " prod "]},
    ]
    document["schema"][0]["quality"] = [{"type": "library", "arguments": ["id"]}]
    assert servers(document) == {"prod": "found"}
    assert elements(document) == {}


def test_server_references_past_the_cap_are_unverifiable() -> None:
    document = contract()
    document["schema"][0]["customProperties"] = [
        {"property": "server", "value": ["prod", "staging", "dev"]}
    ]
    result = resolve_internal(document, reference_inputs(document), max_refs=1)["servers"]
    assert result == {"prod": "found", "staging": "unverifiable", "dev": "unverifiable"}


def test_array_items_are_walked_for_references_and_resolution() -> None:
    document = contract(slaProperties=[{"property": "x", "element": "orders.lines.sku"}])
    document["schema"][0]["properties"].append(
        {
            "name": "lines",
            "items": {
                "relationships": [{"to": "customers.region"}],
                "properties": [{"name": "sku", "relationships": [{"to": "customers.nope"}]}],
            },
        }
    )
    assert elements(document) == {
        "orders.lines.sku": "found",
        "customers.region": "found",
        "customers.nope": "not_found",
    }


def test_non_mapping_objects_and_unnamed_objects_are_skipped() -> None:
    document = contract(relationships=[{"from": "orders.id", "to": "customers.id"}])
    document["schema"].extend(["junk", {"id": "o9", "name": 7}])
    assert elements(document) == {"orders.id": "found", "customers.id": "found"}


def test_a_qualified_reference_to_an_object_alone_is_found() -> None:
    document = contract(
        slaProperties=[
            {"property": "x", "element": "schema/o1"},
            {"property": "y", "element": "/schema/o9"},
        ]
    )
    assert elements(document) == {"schema/o1": "found", "/schema/o9": "not_found"}


def test_a_bare_name_outside_a_quality_check_names_a_schema_object() -> None:
    document = contract(relationships=[{"from": "orders", "to": "suppliers"}])
    assert elements(document) == {"orders": "found", "suppliers": "not_found"}


def test_a_bare_name_checked_in_two_objects_keeps_its_worst_state() -> None:
    document = contract()
    document["schema"][0]["quality"] = [{"arguments": {"properties": ["region"]}}]
    document["schema"][1]["quality"] = [{"arguments": {"properties": ["region"]}}]
    assert elements(document) == {"region": "not_found"}


def test_a_blank_team_name_is_not_collected() -> None:
    inputs = reference_inputs(contract(team={"name": "  ", "members": [{"username": " a "}]}))
    assert inputs.team_name is None
    assert inputs.user_names == ("a",)
