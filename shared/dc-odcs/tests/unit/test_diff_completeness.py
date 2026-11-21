"""VAL-02: every attribute of both vendored ODCS schemas is classified on purpose (D-145).

The test walks ``odcs-json-schema-v3.1.0.json`` and ``odcs-json-schema-v3.2.0.json`` (element
``$defs`` properties, every ``logicalTypeOptions`` branch, every Server variant) and fails when an
``(element kind, attribute)`` has no explicit table row, is not a structural attribute the walk
pairs itself, is not in ``METADATA_ONLY`` and, for servers, is not covered by the connection
wildcard. A new attribute in a future vendored schema therefore forces a conscious decision.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import dc_odcs
import pytest
from dc_odcs.diff import table
from dc_odcs.diff.bounds import BOUND_KEYWORDS
from dc_odcs.diff.normalise import (
    KEYED_COLLECTIONS,
    SCALAR_SETS,
    single_child_kind,
    spec_for,
)

pytestmark = pytest.mark.unit

SCHEMA_DIR = Path(dc_odcs.__file__).resolve().parent / "schemas"
VERSIONS = ("3.1.0", "3.2.0")

# element kind (the walk's name) -> the schema ``$defs`` whose properties describe it
KIND_DEFS: dict[str, tuple[str, ...]] = {
    "object": ("SchemaElement", "SchemaObject"),
    "property": ("SchemaElement", "SchemaBaseProperty", "SchemaItemProperty"),
    "relationship": ("RelationshipBase",),
    "quality": (
        "DataQuality",
        "DataQualityLibrary",
        "DataQualitySql",
        "DataQualityCustom",
        "DataQualityOperators",
    ),
    "sla": ("ServiceLevelAgreementProperty",),
    "server": ("Server",),
    "team": ("Team",),
    "team-member": ("TeamMember",),
    "support": ("SupportItem",),
    "role": ("Role",),
    "custom-property": ("CustomProperty",),
    "enum-value": ("EnumValue",),
    "synonym": ("Synonym",),
}
_COMBINATORS = ("allOf", "anyOf", "oneOf", "then", "else")


def _properties(node: Any, out: dict[str, Any] | None = None) -> dict[str, Any]:
    """Property names declared by a schema node, through its allOf/oneOf/then branches."""
    found: dict[str, Any] = {} if out is None else out
    if isinstance(node, list):
        for item in node:
            _properties(item, found)
    elif isinstance(node, dict):
        declared = node.get("properties")
        if isinstance(declared, dict):
            for name, sub in declared.items():
                found.setdefault(name, sub)
        for key in _COMBINATORS:
            if key in node:
                _properties(node[key], found)
    return found


def schema_attributes(schema: dict[str, Any]) -> set[tuple[str, str]]:
    """Every ``(element kind, attribute)`` the schema declares."""
    defs = schema["$defs"]
    found: set[tuple[str, str]] = {("contract", name) for name in schema["properties"]}
    for kind, names in KIND_DEFS.items():
        for name in names:
            if name in defs:
                found.update((kind, attribute) for attribute in _properties(defs[name]))
    base = _properties(defs["SchemaBaseProperty"])
    # every logicalTypeOptions if/then branch (string, date, timestamp, numbers, object, array,
    # vector); their keys surface as attributes of a property in the walk
    for branch in defs["SchemaBaseProperty"]["allOf"]:
        options = _properties(branch.get("then", {})).get("logicalTypeOptions")
        if options is not None:
            found.update(("property", attribute) for attribute in _properties(options))
    # the 3.2 map type: ``key`` and ``value`` of the ``map`` single child
    if "map" in base:
        found.update(("map", attribute) for attribute in _properties(base["map"]))
    # every Server variant adds its own connection attributes
    for variant in defs["ServerSource"].values():
        found.update(("server", attribute) for attribute in _properties(variant))
    context = defs.get("Context")
    if context is not None:
        found.update(("context", attribute) for attribute in _properties(context))
    return found


def _structural(kind: str, attribute: str) -> bool:
    """Attributes the walk pairs, descends into or compares as intervals (not table rows)."""
    if attribute == "logicalTypeOptions" or attribute in BOUND_KEYWORDS:
        return True
    if (kind, attribute) in table.EXCLUDED:
        return True
    if attribute in SCALAR_SETS:
        return True
    if spec_for(kind, attribute) is not None or single_child_kind(kind, attribute) is not None:
        return True
    return any(
        spec.child_kind == kind and attribute in spec.key_fields
        for spec in KEYED_COLLECTIONS.values()
    )


def gaps(schema: dict[str, Any]) -> set[tuple[str, str]]:
    """The attributes of a schema that nothing in the classifier accounts for."""
    missing: set[tuple[str, str]] = set()
    for kind, attribute in schema_attributes(schema):
        if _structural(kind, attribute) or table.is_explicit(kind, attribute):
            continue
        if (
            kind == "server"
            and table.lookup(kind, attribute, "changed") == table.RULES[table.CONNECTION_WILDCARD]
        ):
            continue  # a connection attribute: the server wildcard row is the decision
        missing.add((kind, attribute))
    return missing


def _load(version: str) -> dict[str, Any]:
    loaded = json.loads((SCHEMA_DIR / f"odcs-json-schema-v{version}.json").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize("version", VERSIONS)
def test_every_schema_attribute_is_classified(version: str) -> None:
    assert gaps(_load(version)) == set()


@pytest.mark.parametrize("version", VERSIONS)
def test_the_walk_sees_the_expected_breadth(version: str) -> None:
    attributes = schema_attributes(_load(version))
    assert {("property", "maxLength"), ("property", "multipleOf"), ("property", "pattern")} <= (
        attributes
    )
    assert ("server", "host") in attributes and ("server", "catalog") in attributes
    assert ("quality", "mustBeBetween") in attributes
    assert len(attributes) > 150
    if version == "3.2.0":
        assert {("property", "dimensions"), ("property", "enum"), ("map", "key")} <= attributes
        assert ("server", "catalogUrl") in attributes


def test_an_injected_attribute_fails_the_gate() -> None:
    schema = copy.deepcopy(_load("3.2.0"))
    defs = schema["$defs"]
    defs["SchemaBaseProperty"]["allOf"][0]["properties"] = {"fakePropertyAttr": {}}
    first_branch = next(
        branch
        for branch in defs["SchemaBaseProperty"]["allOf"]
        if "logicalTypeOptions" in branch.get("then", {}).get("properties", {})
    )
    first_branch["then"]["properties"]["logicalTypeOptions"]["properties"]["fakeOption"] = {}
    defs["DataQuality"]["properties"]["fakeQualityAttr"] = {}
    defs["ServerSource"]["PostgresServer"]["properties"]["fakeConnectionAttr"] = {}
    defs["Team"]["properties"]["fakeTeamAttr"] = {}
    schema["properties"]["fakeTopLevel"] = {}
    missing = gaps(schema)
    assert ("property", "fakePropertyAttr") in missing
    assert ("property", "fakeOption") in missing
    assert ("quality", "fakeQualityAttr") in missing
    assert ("contract", "fakeTopLevel") in missing
    # a server connection attribute is covered by the wildcard, a metadata-only kind by its kind
    assert ("server", "fakeConnectionAttr") not in missing
    assert ("team", "fakeTeamAttr") not in missing


def test_the_connection_wildcard_row_is_the_server_warning() -> None:
    rule = table.RULES[table.CONNECTION_WILDCARD]
    assert (rule.code, rule.cls) == ("server-changed", "warning")
    for attribute in ("host", "port", "database", "catalog", "endpointUrl", "schema"):
        assert table.lookup("server", attribute, "changed") == rule


def test_metadata_only_entries_classify_as_info() -> None:
    assert table.METADATA_ONLY, "METADATA_ONLY must not be empty"
    for kind, attributes in table.METADATA_ONLY.items():
        for attribute in attributes:
            rule = table.lookup(kind if kind != "*" else "property", attribute, "changed")
            assert (rule.code, rule.cls) == (table.METADATA_CODE, "info"), (kind, attribute)


def test_excluded_attributes_are_not_compared_as_content() -> None:
    assert ("contract", "version") in table.EXCLUDED
