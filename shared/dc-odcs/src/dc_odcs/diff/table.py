"""The classification rule table (D-144, D-145): ``(element kind, attribute, direction)`` rows.

Every leaf difference found by the walk is classified here and nowhere else. A difference no row
matches becomes code ``unclassified``, class ``warning`` (D-145). Editing any row is a change to
public behaviour that is stored in reports: bump ``CLASSIFIER_VERSION`` in ``dc_odcs.diff``.

Directions: ``added`` (absent -> present), ``removed`` (present -> absent), ``changed`` (both
present and different), ``renamed`` (an equal id with another name), ``to-true`` / ``to-false``
for boolean attributes (absent counts as false) and ``*`` as a row wildcard. Lookup order for an
attribute difference: the exact row, the attribute wildcard, the same two with kind ``*``, then
the kind wildcards. Element rows (an element on one side only) use the empty attribute.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from dc_odcs.diff.model import ChangeClass

UNCLASSIFIED_CODE = "unclassified"
METADATA_CODE = "metadata-changed"
PHYSICAL_TYPE_COSMETIC = "physical-type-cosmetic"

BOOLEAN_ATTRIBUTES = frozenset({"required", "unique", "primaryKey", "uniqueItems", "partitioned"})


class Rule(NamedTuple):
    code: str
    cls: ChangeClass


_ENTITIES = ("object", "property")

# Never compared as content: the declared version token is the thing being judged (RESEARCH
# table), and an entity's name is its identity (the pairing reports renames).
EXCLUDED: frozenset[tuple[str, str]] = frozenset(
    {("contract", "version"), ("object", "name"), ("property", "name")}
)

# The server connection wildcard: every Server variant adds its own connection attributes (host,
# port, database, catalog, endpointUrl, ...); a change to any of them is one warning.
CONNECTION_WILDCARD = ("server", "*", "*")

RULES: dict[tuple[str, str, str], Rule] = {}


def _row(
    kinds: tuple[str, ...], attribute: str, direction: str, code: str, cls: ChangeClass
) -> None:
    for kind in kinds:
        RULES[(kind, attribute, direction)] = Rule(code, cls)


# --- identity ---
_row(("object",), "name", "renamed", "object-renamed", "breaking")
_row(("property",), "name", "renamed", "property-renamed", "breaking")
_row(("*",), "id", "changed", "id-changed", "warning")

# --- objects and properties: types, guarantees, constraints ---
_row(_ENTITIES, "logicalType", "*", "logical-type-changed", "breaking")
_row(("property",), "physicalType", "*", "physical-type-changed", "breaking")
_row(("object",), "physicalType", "*", "object-kind-changed", "warning")
_row(_ENTITIES, "physicalName", "*", "physical-name-changed", "breaking")
_row(_ENTITIES, "required", "to-true", "required-added", "breaking")
_row(_ENTITIES, "required", "to-false", "required-removed", "breaking")
_row(_ENTITIES, "unique", "to-true", "unique-added", "breaking")
_row(_ENTITIES, "unique", "to-false", "unique-removed", "breaking")
_row(_ENTITIES, "primaryKey", "*", "primary-key-changed", "breaking")
_row(_ENTITIES, "primaryKeyPosition", "*", "primary-key-position-changed", "breaking")
_row(_ENTITIES, "classification", "*", "classification-changed", "warning")
_row(_ENTITIES, "partitioned", "*", "partitioning-changed", "warning")
_row(_ENTITIES, "partitionKeyPosition", "*", "partitioning-changed", "warning")
_row(_ENTITIES, "pattern", "added", "pattern-added", "breaking")
_row(_ENTITIES, "pattern", "removed", "pattern-removed", "warning")
_row(_ENTITIES, "pattern", "changed", "pattern-changed", "warning")
for _attribute, _code in (("format", "format-changed"), ("multipleOf", "multiple-of-changed")):
    _row(_ENTITIES, _attribute, "added", _code, "breaking")
    _row(_ENTITIES, _attribute, "changed", _code, "breaking")
    _row(_ENTITIES, _attribute, "removed", _code, "warning")
_row(_ENTITIES, "uniqueItems", "*", "unique-items-changed", "breaking")
_row(_ENTITIES, "dimensions", "*", "vector-shape-changed", "breaking")
_row(_ENTITIES, "elementType", "*", "vector-shape-changed", "breaking")
for _attribute in ("distanceMetric", "embeddingModel", "embeddingModelVersion", "normalized"):
    _row(_ENTITIES, _attribute, "*", "vector-shape-changed", "breaking")
_row(_ENTITIES, "encryptedName", "*", "physical-name-changed", "breaking")
_row(_ENTITIES, "timezone", "*", "timezone-changed", "warning")
_row(_ENTITIES, "defaultTimezone", "*", "timezone-changed", "warning")
_row(("property",), "enum", "added", "enum-added", "breaking")
_row(("property",), "enum", "removed", "enum-removed", "warning")
_row(("*",), "logicalTypeOptions.required", "added", "required-added", "breaking")
_row(("*",), "logicalTypeOptions.required", "removed", "required-removed", "breaking")
_row(("*",), "bounds", "tightened", "constraint-tightened", "breaking")
_row(("*",), "bounds", "both", "constraint-tightened", "breaking")
_row(("*",), "bounds", "widened", "constraint-widened", "warning")

# --- relationships, quality, SLA, servers, access roles ---
_row(("relationship",), "from", "*", "relationship-retargeted", "breaking")
_row(("relationship",), "to", "*", "relationship-retargeted", "breaking")
_row(("relationship",), "type", "*", "relationship-changed", "warning")
_row(("relationship",), "*", "*", "relationship-changed", "warning")  # fallback, not a decision
_row(("quality",), "name", "*", METADATA_CODE, "info")
for _attribute in (
    "type",
    "metric",
    "rule",
    "arguments",
    "query",
    "engine",
    "implementation",
    "unit",
    "severity",
    "dimension",
    "method",
    "schedule",
    "scheduler",
    "businessImpact",
    "mustBe",
    "mustNotBe",
    "mustBeGreaterThan",
    "mustBeGreaterOrEqualTo",
    "mustBeLessThan",
    "mustBeLessOrEqualTo",
    "mustBeBetween",
    "mustNotBeBetween",
):
    _row(("quality",), _attribute, "*", "quality-changed", "warning")
_row(("quality",), "*", "*", "quality-changed", "warning")  # fallback, not a decision
for _attribute in ("value", "valueExt", "unit", "driver", "schedule", "scheduler"):
    _row(("sla",), _attribute, "*", "sla-changed", "warning")
_row(("sla",), "*", "*", "sla-changed", "warning")  # fallback, not a decision
RULES[CONNECTION_WILDCARD] = Rule("server-changed", "warning")
_row(("role",), "access", "*", "access-role-changed", "warning")

# --- the contract itself ---
_row(("contract",), "status", "*", "status-changed", "warning")
_row(("contract",), "apiVersion", "*", "api-version-changed", "info")
_row(("contract",), "slaDefaultElement", "*", "sla-changed", "warning")

# --- documentation-only attributes (explicit decisions, one entry each) --------------------------
# Any difference is informational: it never changes what a consumer can read or write. Keyed by
# element kind; ``*`` applies to every kind. The completeness test requires every attribute of the
# vendored ODCS schemas to be here, in a row above, structural or covered by the server wildcard.
METADATA_ONLY: dict[str, frozenset[str]] = {
    "*": frozenset(
        {
            "description",
            "businessName",
            "tags",
            "customProperties",
            "authoritativeDefinitions",
            "synonyms",
            "examples",
            "deprecated",
            "criticalDataElement",
            "context",
            "transformSourceObjects",
            "transformLogic",
            "transformDescription",
            "semanticType",
            "dataGranularityDescription",
        }
    ),
    "contract": frozenset(
        {"kind", "name", "tenant", "domain", "dataProduct", "price", "contractCreatedTs"}
    ),
    "enum-value": frozenset({"label"}),
}
for _kind, _attributes in METADATA_ONLY.items():
    for _attribute in sorted(_attributes):
        _row((_kind,), _attribute, "*", METADATA_CODE, "info")

# Elements that only carry documentation: any difference is metadata.
METADATA_KINDS = frozenset(
    {
        "team",
        "team-member",
        "support",
        "custom-property",
        "authoritative-definition",
        "synonym",
        "context",
        "context-statement",
        "context-constraint",
        "server-role",
        "role",
        "map",
    }
)
for _kind in METADATA_KINDS:
    _row((_kind,), "*", "*", METADATA_CODE, "info")

# --- an element on one side only ---
_row(("object",), "", "added", "object-added", "info")
_row(("object",), "", "removed", "object-removed", "breaking")
_row(("property",), "", "added", "property-added", "info")
_row(("property",), "", "removed", "property-removed", "breaking")
_row(("relationship",), "", "added", "relationship-added", "warning")
_row(("relationship",), "", "removed", "relationship-removed", "breaking")
_row(("quality",), "", "added", "quality-added", "info")
_row(("quality",), "", "removed", "quality-removed", "warning")
_row(("enum-value",), "", "added", "enum-value-added", "warning")
_row(("enum-value",), "", "removed", "enum-value-removed", "breaking")
_row(("sla",), "", "added", "sla-added", "info")
_row(("sla",), "", "removed", "sla-removed", "warning")
_row(("server",), "", "added", "server-added", "info")
_row(("server",), "", "removed", "server-removed", "breaking")
_row(("role",), "", "removed", "access-role-removed", "warning")

_UNCLASSIFIED = Rule(UNCLASSIFIED_CODE, "warning")


def lookup(kind: str, attribute: str, direction: str) -> Rule:
    """The rule for an attribute difference, or the ``unclassified`` warning."""
    for key in (
        (kind, attribute, direction),
        (kind, attribute, "*"),
        ("*", attribute, direction),
        ("*", attribute, "*"),
        (kind, "*", direction),
        (kind, "*", "*"),
    ):
        rule = RULES.get(key)
        if rule is not None:
            return rule
    return _UNCLASSIFIED


def is_explicit(kind: str, attribute: str) -> bool:
    """Whether an attribute has its own decision: a row naming it, ``METADATA_ONLY`` or a
    documentation-only kind. The per-kind fallback rows (``quality``, ``sla``, ``relationship``)
    and the server connection wildcard are not decisions about a named attribute."""
    if kind in METADATA_KINDS or (kind, attribute) in EXCLUDED:
        return True
    return any(
        key[1] == attribute and key[0] in (kind, "*") for key in RULES if key[1] not in ("", "*")
    )


def _squash(value: Any) -> str | None:
    return "".join(value.split()).casefold() if isinstance(value, str) else None


def classify_attribute(kind: str, attribute: str, direction: str, before: Any, after: Any) -> Rule:
    """Classify an attribute difference (``before``/``after`` are the raw document values)."""
    if (
        attribute == "physicalType"
        and kind == "property"
        and direction == "changed"
        and _squash(before) is not None
        and _squash(before) == _squash(after)
    ):
        return Rule(PHYSICAL_TYPE_COSMETIC, "info")  # case or whitespace only (D-145)
    return lookup(kind, attribute, direction)


def classify_bound(verdict: str) -> Rule:
    """Classify a constraint-bound change (``tightened``, ``widened`` or ``both``)."""
    return RULES[("*", "bounds", verdict)]


def classify_element(kind: str, action: str, node: Any) -> Rule:
    """Classify an element that exists on one side only (``action`` is added or removed)."""
    if kind == "map":
        kind = "property"
    if (
        kind == "property"
        and action == "added"
        and isinstance(node, dict)
        and node.get("required") is True
    ):
        return Rule("required-property-added", "breaking")
    rule = RULES.get((kind, "", action))
    if rule is not None:
        return rule
    if kind in METADATA_KINDS:
        return Rule(METADATA_CODE, "info")
    return _UNCLASSIFIED


__all__ = [
    "BOOLEAN_ATTRIBUTES",
    "CONNECTION_WILDCARD",
    "EXCLUDED",
    "METADATA_CODE",
    "METADATA_KINDS",
    "METADATA_ONLY",
    "PHYSICAL_TYPE_COSMETIC",
    "RULES",
    "UNCLASSIFIED_CODE",
    "Rule",
    "classify_attribute",
    "classify_bound",
    "classify_element",
    "is_explicit",
    "lookup",
]
