"""Contract link extraction (CONT-04, D-54): the two recognised forms, at any depth, bounded."""

from __future__ import annotations

from typing import Any

import pytest
from dc_odcs import MAX_REFS, ContractRef, extract_contract_refs

pytestmark = pytest.mark.unit

UUID = "0198AAAA-BBBB-7CCC-8DDD-EEEEEEEEEEEE"
LOWER = UUID.lower()
URL = f"https://dc.example/api/v1/contracts/contracts/{UUID}"


def test_schema_level_relationship_file_reference_is_an_odcs_id_ref() -> None:
    document = {"schema": [{"relationships": [{"to": "orders.yaml#/schema/orders"}]}]}
    assert extract_contract_refs(document) == [
        ContractRef("odcs_id", "orders", "/schema/0/relationships/0/to")
    ]


def test_property_level_relationship_to_list_gives_one_ref_per_element() -> None:
    document = {
        "schema": [
            {
                "name": "t",
                "properties": [
                    {"relationships": [{"to": ["a.yaml#/x", "b.yml#/y", "not a link"]}]},
                ],
            }
        ]
    }
    assert extract_contract_refs(document) == [
        ContractRef("odcs_id", "a", "/schema/0/properties/0/relationships/0/to/0"),
        ContractRef("odcs_id", "b", "/schema/0/properties/0/relationships/0/to/1"),
    ]


def test_authoritative_definition_platform_url_is_a_lowercased_uuid_ref() -> None:
    document = {"authoritativeDefinitions": [{"type": "x", "url": URL}]}
    assert extract_contract_refs(document) == [
        ContractRef("contract_uuid", LOWER, "/authoritativeDefinitions/0/url")
    ]


def test_platform_url_with_a_version_suffix_is_the_same_uuid_ref() -> None:
    document = {"authoritativeDefinitions": [{"url": f"{URL}/versions/1.0.0"}]}
    assert [r.value for r in extract_contract_refs(document)] == [LOWER]


def test_relationship_to_may_be_a_platform_url() -> None:
    document = {"relationships": [{"to": URL}]}
    assert extract_contract_refs(document) == [
        ContractRef("contract_uuid", LOWER, "/relationships/0/to")
    ]


def test_url_file_name_and_urn_style_ids_are_odcs_id_refs() -> None:
    document = {
        "authoritativeDefinitions": [
            {"url": "https://host/x/orders.yml#a"},
            {"url": "urn:dc:orders:42.yaml#/schema/o"},
        ]
    }
    assert [(r.kind, r.value) for r in extract_contract_refs(document)] == [
        ("odcs_id", "orders"),
        ("odcs_id", "urn:dc:orders:42"),
    ]


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/docs",
        "orders.yaml",
        ".yaml#x",
        "dir/.yaml#x",
        "plain text",
        "/api/v1/contracts/other",
    ],
)
def test_values_matching_neither_rule_are_ignored(value: str) -> None:
    document = {
        "relationships": [{"to": value}],
        "authoritativeDefinitions": [{"url": value}],
    }
    assert extract_contract_refs(document) == []


def test_links_outside_the_two_field_families_are_ignored() -> None:
    document = {
        "description": {"purpose": URL},
        "customProperties": [{"property": "x", "value": URL}],
    }
    assert extract_contract_refs(document) == []


def test_refs_are_found_at_any_depth() -> None:
    document = {
        "schema": [{"properties": [{"items": {"authoritativeDefinitions": [{"url": URL}]}}]}]
    }
    assert extract_contract_refs(document) == [
        ContractRef(
            "contract_uuid", LOWER, "/schema/0/properties/0/items/authoritativeDefinitions/0/url"
        )
    ]


def test_duplicates_by_kind_and_value_collapse_keeping_the_first_pointer() -> None:
    document = {
        "relationships": [{"to": "a.yaml#/x"}, {"to": "a.yaml#/y"}],
        "authoritativeDefinitions": [{"url": URL}, {"url": URL.upper().replace("HTTPS", "https")}],
    }
    refs = extract_contract_refs(document)
    assert [(r.kind, r.value, r.pointer) for r in refs] == [
        ("odcs_id", "a", "/relationships/0/to"),
        ("contract_uuid", LOWER, "/authoritativeDefinitions/0/url"),
    ]


def test_raw_distinct_keeps_every_link_string_to_one_target() -> None:
    """IN-04: link facts key by the link as written, so a second pin is never dropped."""
    first, second = f"{URL}/versions/1.0.0", f"{URL}/versions/9.9.9"
    document = {
        "authoritativeDefinitions": [{"url": first}, {"url": second}, {"url": first}],
    }
    by_target = extract_contract_refs(document)
    by_raw = extract_contract_refs(document, distinct="raw")
    assert [r.pointer for r in by_target] == ["/authoritativeDefinitions/0/url"]
    assert [(r.value, r.pointer) for r in by_raw] == [
        (LOWER, "/authoritativeDefinitions/0/url"),
        (LOWER, "/authoritativeDefinitions/1/url"),
    ]


def test_the_cap_counts_link_strings_in_raw_mode() -> None:
    document = {"authoritativeDefinitions": [{"url": f"{URL}/v{i}"} for i in range(5)]}
    assert len(extract_contract_refs(document, max_refs=3, distinct="raw")) == 3
    assert len(extract_contract_refs(document, max_refs=3)) == 1


def test_pointer_tokens_are_escaped_per_rfc_6901() -> None:
    document = {"a/b": {"c~d": [{"relationships": [{"to": "x.yaml#/y"}]}]}}
    assert extract_contract_refs(document)[0].pointer == "/a~1b/c~0d/0/relationships/0/to"


def test_the_cap_returns_exactly_max_refs() -> None:
    document = {"relationships": [{"to": f"c{i}.yaml#/x"} for i in range(1500)]}
    refs = extract_contract_refs(document)
    assert len(refs) == MAX_REFS == 1000
    assert refs[0].value == "c0" and refs[-1].value == "c999"
    assert len(extract_contract_refs(document, max_refs=3)) == 3


def test_a_document_nested_5000_levels_deep_does_not_recurse() -> None:
    node: Any = {"relationships": [{"to": "deep.yaml#/x"}]}
    for _ in range(5000):
        node = {"n": node}
    refs = extract_contract_refs(node)
    assert [r.value for r in refs] == ["deep"]
    assert refs[0].pointer == "/n" * 5000 + "/relationships/0/to"


@pytest.mark.parametrize("document", [None, 1, "x", [], [1, {"relationships": 3}]])
def test_odd_shapes_are_accepted(document: Any) -> None:
    assert extract_contract_refs(document) == []


def test_junk_link_entries_are_skipped_around_a_real_link() -> None:
    document = {
        "relationships": ["junk", {"to": 5}, {"to": "orders.yaml#/schema/o"}],
        "authoritativeDefinitions": ["junk", {"url": 5}, {"type": "doc"}],
    }
    assert extract_contract_refs(document) == [
        ContractRef("odcs_id", "orders", "/relationships/2/to")
    ]
