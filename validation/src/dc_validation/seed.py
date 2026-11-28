"""The seeded platform rule set ``odcs-structural`` (D-130, VAL-01, RULE-05).

The structural rules of VAL-01 beyond the official ODCS JSON Schema ship as a platform rule set:
scope ``platform`` (no folder, so no permission can be held on it and no API route can edit,
mandate, deprecate or delete it), mandatory, and enforced from its first version. Migration
``0005_seed_platform_rule_set`` inserts ``ODCS_STRUCTURAL_V1`` under the fixed ids below, with the
system actor ``system:validation-seed``; the set is then versioned and reported like any other
(mandatory resolution always includes platform sets, at depth 0).

How D-130's indicative examples were adopted (the examples are indicative, the seed is small and
conservative because it is enforced from version 1):

* "property names unique within an object" -> ``property-names-unique`` (error), plus
  ``schema-names-unique`` (error) for the schema objects of the contract itself.
* "primary-key positions unique and contiguous within an object" ->
  ``primary-key-positions-valid`` (error). ODCS documents ``primaryKeyPosition`` as "Default to
  -1", so a primary key without a position is legal: the rule applies only when at least one
  primary-key property declares a position other than -1. Then every primary-key property must
  declare one, the positions must be unique and within 1..number of primary-key properties.
* "``logicalTypeOptions`` consistent with ``logicalType``" -> ``logical-type-option-bounds``
  (warning), limited to the min/max bound pairs (``minLength <= maxLength``, ``minimum <=
  maximum``, ``minItems <= maxItems``). Which options belong to which logical type is ambiguous in
  v3.2.0 (``logicalTypeOptions`` is an open object), so a cross-option check could block a valid
  contract; a warning cannot.

Every condition is guarded so no document can make it error: an error in an enforced mandatory
rule blocks publish (D-139). Guards use ``type(x) == map|list|string|int|bool`` tests and
``cel.bind``; a field of an unexpected JSON type is simply not part of what the rule judges (the
official ODCS JSON Schema reports the type problem itself). Conditions stay local to the matched
node (``this``) and never touch ``contract``, so each node gets its own iteration budget
(research Pitfalls 6 and 7).

``odcs-publication`` (quick 261119-rpi, migration 0009) holds the checks that apply only when a
version is published. Its one rule, ``published-status-active``, is a warning: the author's ODCS
``status`` belongs to the author and the platform never rewrites it, but a published document that
says ``status: draft`` reads as "not ready" to consumers. It binds the cel-2 ``ctx.operation``
(``save`` or ``publish``) and judges only publish runs, so a draft save or validation never
shows it. A warning-severity finding is never blocking (D-124), in warn grace and after
``enforce_at`` alike. Its condition judges only ``this`` and is fully guarded, so it can never
error.

``ODCS_PUBLICATION_V2`` (quick 261128-rsa, migration 0010) is the second version of that set and
rolls out like any new version of a mandatory set: in warn grace until its ``enforce_at``. A
released version (a SemVer without a prerelease part, so it becomes Latest or an older release)
must declare ``status: active``: ``released-status-active`` is an **error**, so once version 2 is
enforced it blocks such a publish. A Preview published with a prerelease version (``1.2.0-rc.1``)
keeps the ``published-status-active`` warning. Both read the document's own ODCS ``version``:
contracts publishes exactly the SemVer the gated bytes declare, or refuses the publish. Only a
string shaped like a SemVer prerelease counts as a Preview; anything else is judged as a release
(fail closed).

A later platform version ships as a new constant (for example ``ODCS_STRUCTURAL_V2``) and a new
migration; ``ODCS_STRUCTURAL_V1`` is never edited, because its hash is stored by migration 0005.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from dc_core.actor import system_actor
from dc_rules.model import RuleSetContent

PLATFORM_ODCS_STRUCTURAL_ID: Final[UUID] = UUID("05d2384d-a57d-4922-a576-921b2f7113bd")
PLATFORM_ODCS_STRUCTURAL_V1_ID: Final[UUID] = UUID("b1a828f6-8bff-45b3-aaf4-b7240e7dba6a")
PLATFORM_SEED_EVENT_ID: Final[UUID] = UUID("be2e53b7-0479-4072-b248-e12c40b493f7")

ODCS_STRUCTURAL_NAME: Final = "odcs-structural"
ODCS_STRUCTURAL_DESCRIPTION: Final = (
    "Platform structural rules for ODCS contracts, mandatory and enforced for every folder."
)

SEED_ACTOR: Final = system_actor("validation-seed")


def _table(*properties: dict[str, Any]) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": list(properties)}]}


def _prop(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "logicalType": "string", **extra}


_PROPERTY_NAMES_UNIQUE = """\
(type(this) == map && has(this.properties) && type(this.properties) == list)
  ? cel.bind(names,
      this.properties
        .filter(p, type(p) == map && has(p.name) && type(p.name) == string)
        .map(p, p.name),
      names.distinct().size() == names.size())
  : true"""

_SCHEMA_NAMES_UNIQUE = """\
(type(this) == map && has(this.schema) && type(this.schema) == list)
  ? cel.bind(names,
      this.schema
        .filter(s, type(s) == map && has(s.name) && type(s.name) == string)
        .map(s, s.name),
      names.distinct().size() == names.size())
  : true"""

_PRIMARY_KEY_POSITIONS_VALID = """\
(type(this) == map && has(this.properties) && type(this.properties) == list)
  ? cel.bind(pks,
      this.properties.filter(p,
        type(p) == map && has(p.primaryKey) && type(p.primaryKey) == bool && p.primaryKey),
      cel.bind(positioned,
        pks.filter(p,
          has(p.primaryKeyPosition) && type(p.primaryKeyPosition) == int
            && p.primaryKeyPosition != -1),
        positioned.size() == 0
          ? true
          : (positioned.size() == pks.size()
              && positioned.all(p, p.primaryKeyPosition >= 1 && p.primaryKeyPosition <= pks.size())
              && positioned.map(p, p.primaryKeyPosition).distinct().size() == pks.size())))
  : true"""

_LOGICAL_TYPE_OPTION_BOUNDS = """\
(type(this) == map && has(this.logicalTypeOptions) && type(this.logicalTypeOptions) == map)
  ? cel.bind(o, this.logicalTypeOptions,
      [["minLength", "maxLength"], ["minimum", "maximum"], ["minItems", "maxItems"]].all(k,
        !((k[0] in o) && (k[1] in o)
          && (type(o[k[0]]) == int || type(o[k[0]]) == double)
          && (type(o[k[1]]) == int || type(o[k[1]]) == double))
        || double(o[k[0]]) <= double(o[k[1]])))
  : true"""


def _rule(
    key: str,
    description: str,
    target: str,
    condition: str,
    severity: str,
    message: str,
    passing: list[dict[str, Any]],
    failing: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "key": key,
        "description": description,
        "target": target,
        "condition": condition,
        "severity": severity,
        "message": message,
        "fixtures": {
            "pass": [{"document": document} for document in passing],
            "fail": [{"document": document} for document in failing],
        },
    }


# Rules are listed in key order, so the stored ``rule_keys`` are sorted and equal draft order.
ODCS_STRUCTURAL_V1: Final[RuleSetContent] = RuleSetContent.model_validate(
    {
        "dsl_version": "cel-1",
        "rules": [
            _rule(
                "logical-type-option-bounds",
                "A logicalTypeOptions lower bound must not exceed its upper bound.",
                "$.schema[*].properties[*]",
                _LOGICAL_TYPE_OPTION_BOUNDS,
                "warning",
                "A logicalTypeOptions lower bound is greater than its upper bound.",
                [
                    _table(_prop("code", logicalTypeOptions={"minLength": 2, "maxLength": 8})),
                    _table(_prop("code")),
                    _table(_prop("score", logicalTypeOptions={"minimum": 1, "maximum": 1})),
                ],
                [
                    _table(_prop("code", logicalTypeOptions={"minLength": 9, "maxLength": 8})),
                    _table(_prop("score", logicalTypeOptions={"minimum": 10, "maximum": 1.5})),
                    _table(_prop("tags", logicalTypeOptions={"minItems": 3, "maxItems": 2})),
                ],
            ),
            _rule(
                "primary-key-positions-valid",
                "When primary-key positions are declared they are unique and contiguous from 1.",
                "$.schema[*]",
                _PRIMARY_KEY_POSITIONS_VALID,
                "error",
                "The primary-key positions of a schema object must be unique and run from 1 "
                "to the number of primary-key properties.",
                [
                    _table(_prop("id", primaryKey=True), _prop("name")),
                    _table(
                        _prop("id", primaryKey=True, primaryKeyPosition=1),
                        _prop("region", primaryKey=True, primaryKeyPosition=2),
                    ),
                    _table(
                        _prop("id", primaryKey=True, primaryKeyPosition=2),
                        _prop("region", primaryKey=True, primaryKeyPosition=1),
                    ),
                ],
                [
                    _table(
                        _prop("id", primaryKey=True, primaryKeyPosition=1),
                        _prop("region", primaryKey=True, primaryKeyPosition=1),
                    ),
                    _table(
                        _prop("id", primaryKey=True, primaryKeyPosition=1),
                        _prop("region", primaryKey=True, primaryKeyPosition=3),
                    ),
                    _table(
                        _prop("id", primaryKey=True, primaryKeyPosition=1),
                        _prop("region", primaryKey=True),
                    ),
                ],
            ),
            _rule(
                "property-names-unique",
                "Property names are unique within a schema object.",
                "$.schema[*]",
                _PROPERTY_NAMES_UNIQUE,
                "error",
                "Two properties of a schema object share a name.",
                [_table(_prop("id"), _prop("name")), {"schema": [{"name": "empty"}]}],
                [_table(_prop("id"), _prop("name"), _prop("id"))],
            ),
            _rule(
                "schema-names-unique",
                "Schema object names are unique within a contract.",
                "$",
                _SCHEMA_NAMES_UNIQUE,
                "error",
                "Two schema objects of the contract share a name.",
                [
                    {"schema": [{"name": "orders"}, {"name": "customers"}]},
                    {},
                ],
                [{"schema": [{"name": "orders"}, {"name": "customers"}, {"name": "orders"}]}],
            ),
        ],
    }
)


# -- Phase 6: odcs-consistency and odcs-references (plan 06-09; D-150, D-151, VAL-04, VAL-05) -----
#
# Two more platform rule sets, written in ``cel-2`` (they read ``refs``). Unlike odcs-structural
# they are NOT enforced from their first version: migration 0006 stamps ``enforce_at = migration
# time + mandatory_warn_days`` so existing teams see warnings first (D-150), and a Policy Manager
# can switch them to enforce early. Because they can block every publish once enforced, every
# condition is guarded the same way as above (an odd-typed field is simply not judged) and a
# missing reference key passes: only ``not_found`` ever fails a rule, ``unverifiable`` never does
# (D-153). Targets are per node so each node gets its own iteration budget. None of the rules
# reads ``previous``; a future seeded rule that does must start with ``previous == null ||``.
#
# Primary-key positions are judged by the odcs-structural set above and are not repeated here.
# The new ``*_V1`` constants are never edited: their hashes are stored by migration 0006.

PLATFORM_ODCS_CONSISTENCY_ID: Final[UUID] = UUID("841826b2-2418-480d-b34b-b600f504931d")
PLATFORM_ODCS_CONSISTENCY_V1_ID: Final[UUID] = UUID("dae431cc-9633-43e2-9bbe-331ea1f11da9")
PLATFORM_ODCS_REFERENCES_ID: Final[UUID] = UUID("c8c7e8bc-fbe8-4225-a0e3-40e8c06ddb3d")
PLATFORM_ODCS_REFERENCES_V1_ID: Final[UUID] = UUID("c5adf089-d394-46bd-b89a-473bbe06b6d5")
PLATFORM_SEMANTIC_SEED_EVENT_IDS: Final[tuple[UUID, UUID]] = (
    UUID("edb1a073-a10e-442e-ae4a-b825b4ab9362"),
    UUID("0f6f7a43-7d1c-4b8e-9a52-3c1d86e0b7a4"),
)

ODCS_CONSISTENCY_NAME: Final = "odcs-consistency"
ODCS_CONSISTENCY_DESCRIPTION: Final = (
    "Platform consistency rules for ODCS contracts: keys, PII, types and element references."
)
ODCS_REFERENCES_NAME: Final = "odcs-references"
ODCS_REFERENCES_DESCRIPTION: Final = (
    "Platform reference-integrity rules for ODCS contracts: links, teams, members and servers."
)


def _is_pii(node: str) -> str:
    """CEL text: ``node`` (a property map) is PII: tag ``pii`` or customProperties pii true."""
    return (
        f"((has({node}.tags) && type({node}.tags) == list"
        f" && {node}.tags.exists(t, type(t) == string && t.lowerAscii() == 'pii'))"
        f" || (has({node}.customProperties) && type({node}.customProperties) == list"
        f" && {node}.customProperties.exists(c, type(c) == map && has(c.property)"
        f" && c.property == 'pii' && has(c.value)"
        f" && (c.value == true || (type(c.value) == string && c.value.lowerAscii() == 'true')))))"
    )


def _resolves(key: str, kind: str) -> str:
    """CEL text: the reference text ``key`` is not ``not_found`` in ``refs.<kind>``."""
    return (
        f"cel.bind(k, {key}.trim(),"
        f" k == '' || !(k in refs.{kind}) || refs.{kind}[k] != 'not_found')"
    )


_LOGICAL_PHYSICAL_COHERENT = """\
(type(this) == map && has(this.logicalType) && type(this.logicalType) == string
    && has(this.physicalType) && type(this.physicalType) == string)
  ? dc.type_coherent(this.logicalType, this.physicalType)
  : true"""

_PARTITION_KEY_POSITION = """\
(type(this) == map && has(this.partitioned) && type(this.partitioned) == bool && this.partitioned)
  ? (has(this.partitionKeyPosition) && type(this.partitionKeyPosition) == int
      && this.partitionKeyPosition >= 1)
  : true"""

_PII_NEEDS_CLASSIFICATION = f"""\
(type(this) == map && {_is_pii("this")})
  ? (has(this.classification) && type(this.classification) == string
      && this.classification.trim() != '')
  : true"""

_PII_NEEDS_RETENTION = f"""\
(type(this) == map && has(this.properties) && type(this.properties) == list)
  ? cel.bind(oname, (has(this.name) && type(this.name) == string) ? this.name : '',
      cel.bind(retention,
        (has(contract.slaProperties) && type(contract.slaProperties) == list)
          ? contract.slaProperties.filter(s,
              type(s) == map && has(s.property) && s.property == 'retention')
          : [],
        this.properties.filter(p, type(p) == map && {_is_pii("p")}).all(p,
          cel.bind(pname, (has(p.name) && type(p.name) == string) ? p.name : '',
            retention.exists(s,
              cel.bind(el, has(s.element) ? s.element : null,
                el == null
                  || (type(el) == string
                      && (el.trim() == ''
                          || el.split(',').exists(e,
                               e.trim() != ''
                                 && (e.trim() == oname
                                     || e.trim() == oname + '.' + pname
                                     || e.trim() == pname))))))))))
  : true"""

_PRIMARY_KEY_REQUIRED_UNIQUE = """\
(type(this) == map && has(this.properties) && type(this.properties) == list)
  ? cel.bind(pks,
      this.properties.filter(p,
        type(p) == map && has(p.primaryKey) && type(p.primaryKey) == bool && p.primaryKey),
      pks.size() == 0
        ? true
        : pks.all(p, has(p.required) && p.required == true)
          && (pks.size() > 1 || (has(pks[0].unique) && pks[0].unique == true)))
  : true"""

_QUALITY_ELEMENT_RESOLVES = f"""\
(type(this) == map && has(this.arguments) && type(this.arguments) == map
    && has(this.arguments.properties))
  ? cel.bind(v, this.arguments.properties,
      (type(v) == list ? v : [v]).all(x, type(x) != string || {_resolves("x", "elements")}))
  : true"""

_RELATIONSHIP_TARGET_RESOLVES = f"""\
type(this) == map
  ? cel.bind(targets,
      (has(this.from) ? (type(this.from) == list ? this.from : [this.from]) : [])
        + (has(this.to) ? (type(this.to) == list ? this.to : [this.to]) : []),
      targets.all(x, type(x) != string || {_resolves("x", "elements")}))
  : true"""

_SLA_ELEMENT_RESOLVES = f"""\
(type(this) == map && has(this.element) && type(this.element) == string)
  ? this.element.split(',').all(e, {_resolves("e", "elements")})
  : true"""

_LINKED_CONTRACT_PUBLISHED = """\
type(this) == map
  ? cel.bind(candidates,
      (has(this.url) ? [this.url] : [])
        + (has(this.to) ? (type(this.to) == list ? this.to : [this.to]) : []),
      candidates.all(x,
        type(x) != string || !(x in refs.contracts)
          || cel.bind(fact, refs.contracts[x],
               type(fact) != map
                 || (!(has(fact.state)
                         && (fact.state == 'not_found'
                             || fact.state == 'unpublished_or_draft_only'))
                     && !(has(fact.pinned_published) && fact.pinned_published == false)))))
  : true"""

_SERVER_REFERENCE_EXISTS = f"""\
(type(this) == map && has(this.value))
  ? cel.bind(names, type(this.value) == list ? this.value : [this.value],
      names.all(n, type(n) != string || {_resolves("n", "servers")}))
  : true"""

_TEAM_EXISTS = f"""\
(type(this) == map && has(this.name) && type(this.name) == string)
  ? {_resolves("this.name", "teams")}
  : true"""

_TEAM_MEMBER_EXISTS = f"""\
(type(this) == map && has(this.username) && type(this.username) == string)
  ? {_resolves("this.username", "principals")}
  : true"""


def _refs(**kinds: Any) -> dict[str, Any]:
    """A complete ``refs`` binding for a fixture: the kinds given, every other kind empty."""
    refs: dict[str, Any] = {
        "elements": {},
        "servers": {},
        "contracts": {},
        "principals": {},
        "teams": {},
    }
    refs.update(kinds)
    return refs


def _fx(document: dict[str, Any], **kinds: Any) -> dict[str, Any]:
    """A fixture whose ``refs`` are the kinds given (empty for a rule that ignores them)."""
    return {"document": document, "refs": _refs(**kinds)}


def _rule2(
    key: str,
    description: str,
    target: str,
    condition: str,
    severity: str,
    message: str,
    passing: list[dict[str, Any]],
    failing: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "key": key,
        "description": description,
        "target": target,
        "condition": condition,
        "severity": severity,
        "message": message,
        "fixtures": {"pass": passing, "fail": failing},
    }


def _doc(*properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": list(properties)}], **extra}


def _retention(element: str | None = None) -> dict[str, Any]:
    sla: dict[str, Any] = {"property": "retention", "value": 5, "unit": "y"}
    if element is not None:
        sla["element"] = element
    return {"slaProperties": [sla]}


def _pii_tagged(name: str = "email", **extra: Any) -> dict[str, Any]:
    return _prop(name, tags=["PII"], **extra)


def _link(uuid_text: str) -> str:
    return f"/api/v1/contracts/contracts/{uuid_text}"


_LINK_A = _link("11111111-1111-4111-8111-111111111111")
_LINK_B = _link("22222222-2222-4222-8222-222222222222")

ODCS_CONSISTENCY_V1: Final[RuleSetContent] = RuleSetContent.model_validate(
    {
        "dsl_version": "cel-2",
        "rules": [
            _rule2(
                "logical-physical-coherent",
                "The logicalType and the physicalType of a property do not clearly disagree.",
                "$.schema[*].properties[*]",
                _LOGICAL_PHYSICAL_COHERENT,
                "warning",
                "The logicalType and the physicalType of a property do not fit together.",
                [
                    _fx(_doc(_prop("id", logicalType="integer", physicalType="BIGINT"))),
                    _fx(_doc(_prop("name", physicalType="VARCHAR(20)"))),
                    _fx(_doc(_prop("blob", logicalType="string", physicalType="my_custom_type"))),
                    _fx(_doc(_prop("plain"))),
                ],
                [
                    _fx(_doc(_prop("id", logicalType="integer", physicalType="VARCHAR(10)"))),
                    _fx(_doc(_prop("flag", logicalType="boolean", physicalType="timestamp"))),
                ],
            ),
            _rule2(
                "partition-key-position",
                "A partitioned property declares its partitionKeyPosition.",
                "$.schema[*].properties[*]",
                _PARTITION_KEY_POSITION,
                "warning",
                "A partitioned property has no partitionKeyPosition.",
                [
                    _fx(_doc(_prop("day", partitioned=True, partitionKeyPosition=1))),
                    _fx(_doc(_prop("day", partitioned=False))),
                    _fx(_doc(_prop("day"))),
                ],
                [
                    _fx(_doc(_prop("day", partitioned=True))),
                    _fx(_doc(_prop("day", partitioned=True, partitionKeyPosition=-1))),
                ],
            ),
            _rule2(
                "pii-needs-classification",
                "A property tagged pii (or with customProperties pii true) has a classification.",
                "$.schema[*].properties[*]",
                _PII_NEEDS_CLASSIFICATION,
                "warning",
                "A PII property has no classification.",
                [
                    _fx(_doc(_pii_tagged(classification="confidential"))),
                    _fx(_doc(_prop("amount"))),
                    _fx(_doc(_prop("amount", tags=["finance"]))),
                ],
                [
                    _fx(_doc(_pii_tagged())),
                    _fx(
                        _doc(
                            _prop(
                                "email",
                                customProperties=[{"property": "pii", "value": True}],
                                classification="  ",
                            )
                        )
                    ),
                ],
            ),
            _rule2(
                "pii-needs-retention",
                "Every PII property of a schema object is covered by an SLA retention property.",
                "$.schema[*]",
                _PII_NEEDS_RETENTION,
                "warning",
                "A PII property has no retention SLA covering it or the whole contract.",
                [
                    _fx(_doc(_pii_tagged(), **_retention())),
                    _fx(_doc(_pii_tagged(), **_retention("orders"))),
                    _fx(_doc(_pii_tagged(), **_retention("orders.email"))),
                    _fx(_doc(_pii_tagged(), **_retention("orders.id, orders.email"))),
                    _fx(_doc(_prop("amount"))),
                ],
                [
                    _fx(_doc(_pii_tagged())),
                    _fx(_doc(_pii_tagged(), **_retention("orders.id"))),
                    _fx(
                        _doc(
                            _pii_tagged(),
                            slaProperties=[{"property": "latency", "value": 1, "unit": "d"}],
                        )
                    ),
                ],
            ),
            _rule2(
                "primary-key-required-unique",
                "A single-column primary key is required and unique; composite keys are required.",
                "$.schema[*]",
                _PRIMARY_KEY_REQUIRED_UNIQUE,
                "error",
                "A primary-key property must be required, and a single-column key also unique.",
                [
                    _fx(_doc(_prop("id", primaryKey=True, required=True, unique=True))),
                    _fx(
                        _doc(
                            _prop("id", primaryKey=True, required=True),
                            _prop("region", primaryKey=True, required=True),
                        )
                    ),
                    _fx(_doc(_prop("id"))),
                ],
                [
                    _fx(_doc(_prop("id", primaryKey=True, required=True))),
                    _fx(_doc(_prop("id", primaryKey=True, unique=True))),
                    _fx(
                        _doc(
                            _prop("id", primaryKey=True, required=True),
                            _prop("region", primaryKey=True),
                        )
                    ),
                ],
            ),
            _rule2(
                "quality-element-resolves",
                "The properties a quality check names exist in the contract.",
                "$..quality[*]",
                _QUALITY_ELEMENT_RESOLVES,
                "error",
                "A quality check names a property that does not exist.",
                [
                    _fx(
                        _doc(
                            _prop(
                                "id",
                                quality=[{"type": "library", "arguments": {"properties": ["id"]}}],
                            )
                        ),
                        elements={"id": "found"},
                    ),
                    _fx(
                        _doc(_prop("id", quality=[{"type": "text", "description": "no arguments"}]))
                    ),
                    _fx(
                        _doc(
                            _prop(
                                "id",
                                quality=[{"type": "library", "arguments": {"properties": "far#x"}}],
                            )
                        ),
                        elements={"far#x": "unverifiable"},
                    ),
                ],
                [
                    _fx(
                        _doc(
                            _prop(
                                "id",
                                quality=[
                                    {"type": "library", "arguments": {"properties": ["ghost"]}}
                                ],
                            )
                        ),
                        elements={"ghost": "not_found"},
                    ),
                    _fx(
                        {
                            "schema": [
                                {
                                    "name": "orders",
                                    "quality": [
                                        {
                                            "type": "library",
                                            "arguments": {"properties": "ghost"},
                                        }
                                    ],
                                }
                            ]
                        },
                        elements={"ghost": "not_found"},
                    ),
                ],
            ),
            _rule2(
                "relationship-target-resolves",
                "A relationship names elements that exist in the contract.",
                "$..relationships[*]",
                _RELATIONSHIP_TARGET_RESOLVES,
                "error",
                "A relationship names an element that does not exist.",
                [
                    _fx(
                        _doc(
                            _prop(
                                "customer_id",
                                relationships=[{"type": "foreignKey", "to": "customers.id"}],
                            )
                        ),
                        elements={"customers.id": "found"},
                    ),
                    _fx(
                        {
                            "schema": [
                                {
                                    "name": "orders",
                                    "relationships": [
                                        {"type": "foreignKey", "from": ["orders.a"], "to": ["c.b"]}
                                    ],
                                }
                            ]
                        },
                        elements={"orders.a": "found", "c.b": "found"},
                    ),
                    _fx(
                        _doc(
                            relationships=[
                                {"type": "foreignKey", "from": "orders.id", "to": "other.yaml#x"}
                            ]
                        ),
                        elements={"orders.id": "found", "other.yaml#x": "unverifiable"},
                    ),
                ],
                [
                    _fx(
                        _doc(
                            _prop(
                                "customer_id",
                                relationships=[{"type": "foreignKey", "to": "customers.nope"}],
                            )
                        ),
                        elements={"customers.nope": "not_found"},
                    ),
                    _fx(
                        {
                            "schema": [
                                {
                                    "name": "orders",
                                    "relationships": [
                                        {"type": "foreignKey", "from": "orders.id", "to": ["c.zz"]}
                                    ],
                                }
                            ]
                        },
                        elements={"orders.id": "found", "c.zz": "not_found"},
                    ),
                ],
            ),
            _rule2(
                "sla-element-resolves",
                "The elements an SLA property names exist in the contract.",
                "$.slaProperties[*]",
                _SLA_ELEMENT_RESOLVES,
                "error",
                "An SLA property names an element that does not exist.",
                [
                    _fx(
                        _doc(
                            _prop("id"),
                            slaProperties=[
                                {
                                    "property": "latency",
                                    "value": 1,
                                    "unit": "d",
                                    "element": "orders.id",
                                }
                            ],
                        ),
                        elements={"orders.id": "found"},
                    ),
                    _fx(
                        _doc(
                            _prop("id"),
                            slaProperties=[{"property": "latency", "value": 1, "unit": "d"}],
                        )
                    ),
                    _fx(
                        _doc(
                            slaProperties=[
                                {
                                    "property": "latency",
                                    "value": 1,
                                    "unit": "d",
                                    "element": "a.b, c.d",
                                }
                            ]
                        ),
                        elements={"a.b": "found", "c.d": "unverifiable"},
                    ),
                ],
                [
                    _fx(
                        _doc(
                            slaProperties=[
                                {
                                    "property": "latency",
                                    "value": 1,
                                    "unit": "d",
                                    "element": "orders.zz",
                                }
                            ]
                        ),
                        elements={"orders.zz": "not_found"},
                    ),
                    _fx(
                        _doc(
                            slaProperties=[
                                {
                                    "property": "latency",
                                    "value": 1,
                                    "unit": "d",
                                    "element": "a.b, c.zz",
                                }
                            ]
                        ),
                        elements={"a.b": "found", "c.zz": "not_found"},
                    ),
                ],
            ),
        ],
    }
)

ODCS_REFERENCES_V1: Final[RuleSetContent] = RuleSetContent.model_validate(
    {
        "dsl_version": "cel-2",
        "rules": [
            _rule2(
                "linked-contract-published",
                "A contract the document links to exists, is published and its pin is published.",
                "$..['relationships','authoritativeDefinitions'][*]",
                _LINKED_CONTRACT_PUBLISHED,
                "error",
                "A linked contract is missing, not published, or pinned to an unpublished version.",
                [
                    _fx(
                        _doc(authoritativeDefinitions=[{"type": "x", "url": _LINK_A}]),
                        contracts={_LINK_A: {"state": "published", "pinned_published": None}},
                    ),
                    _fx(
                        _doc(authoritativeDefinitions=[{"type": "x", "url": _LINK_A}]),
                        contracts={_LINK_A: {"state": "unverifiable"}},
                    ),
                    _fx(
                        _doc(authoritativeDefinitions=[{"type": "x", "url": _LINK_A}]),
                        contracts={_LINK_A: {"state": "published", "pinned_published": True}},
                    ),
                    _fx(_doc(_prop("id", description="see /api/v1/contracts/contracts/ docs"))),
                ],
                [
                    _fx(
                        _doc(authoritativeDefinitions=[{"type": "x", "url": _LINK_A}]),
                        contracts={_LINK_A: {"state": "not_found"}},
                    ),
                    _fx(
                        _doc(authoritativeDefinitions=[{"type": "x", "url": _LINK_B}]),
                        contracts={_LINK_B: {"state": "unpublished_or_draft_only"}},
                    ),
                    _fx(
                        _doc(authoritativeDefinitions=[{"type": "x", "url": _LINK_A}]),
                        contracts={_LINK_A: {"state": "published", "pinned_published": False}},
                    ),
                ],
            ),
            _rule2(
                "server-reference-exists",
                "A server a customProperties server entry names is declared in the contract.",
                "$..customProperties[?@.property == 'server']",
                _SERVER_REFERENCE_EXISTS,
                "error",
                "A server reference names a server the contract does not declare.",
                [
                    _fx(
                        _doc(
                            _prop("id", customProperties=[{"property": "server", "value": "prod"}]),
                            servers=[{"server": "prod", "type": "postgres"}],
                        ),
                        servers={"prod": "found"},
                    ),
                    _fx(_doc(_prop("id", customProperties=[{"property": "owner", "value": "x"}]))),
                    _fx(
                        _doc(
                            _prop(
                                "id", customProperties=[{"property": "server", "value": ["a", "b"]}]
                            )
                        ),
                        servers={"a": "found", "b": "unverifiable"},
                    ),
                ],
                [
                    _fx(
                        _doc(
                            _prop("id", customProperties=[{"property": "server", "value": "ghost"}])
                        ),
                        servers={"ghost": "not_found"},
                    ),
                    _fx(
                        _doc(
                            _prop(
                                "id",
                                customProperties=[{"property": "server", "value": ["a", "ghost"]}],
                            )
                        ),
                        servers={"a": "found", "ghost": "not_found"},
                    ),
                ],
            ),
            _rule2(
                "team-exists",
                "The team the contract names can be found.",
                "$.team",
                _TEAM_EXISTS,
                "warning",
                "The team the contract names cannot be found.",
                [
                    _fx({"team": {"name": "payments"}}, teams={"payments": "found"}),
                    _fx({"team": {"name": "payments"}}, teams={"payments": "unverifiable"}),
                    _fx({"team": [{"username": "alice"}]}),
                    _fx({}),
                ],
                [_fx({"team": {"name": "ghosts"}}, teams={"ghosts": "not_found"})],
            ),
            _rule2(
                "team-member-exists",
                "Every team member the contract names can be found.",
                "$.team..[?@.username]",
                _TEAM_MEMBER_EXISTS,
                "warning",
                "A team member the contract names cannot be found.",
                [
                    _fx(
                        {"team": {"name": "t", "members": [{"username": "alice"}]}},
                        principals={"alice": "found"},
                    ),
                    _fx(
                        {"team": {"members": [{"username": "bob"}]}},
                        principals={"bob": "unverifiable"},
                    ),
                    _fx({"team": {"name": "t"}}),
                ],
                [
                    _fx(
                        {"team": {"members": [{"username": "alice"}, {"username": "ghost"}]}},
                        principals={"alice": "found", "ghost": "not_found"},
                    ),
                ],
            ),
        ],
    }
)


PLATFORM_ODCS_PUBLICATION_ID: Final[UUID] = UUID("8285c164-893d-4088-bd46-c517186d3d5f")
PLATFORM_ODCS_PUBLICATION_V1_ID: Final[UUID] = UUID("f686e3d6-a026-472c-b64c-49aa2edd49ba")
PLATFORM_PUBLICATION_SEED_EVENT_ID: Final[UUID] = UUID("0bf7e9f7-d257-4959-8da5-602ed8109255")

ODCS_PUBLICATION_NAME: Final = "odcs-publication"
ODCS_PUBLICATION_DESCRIPTION: Final = (
    "Platform publication checks for ODCS contracts: warnings at publish, never blocking."
)

_PUBLISHED_STATUS_ACTIVE = """\
!has(ctx.operation) || ctx.operation != 'publish'
  || (type(this) == map && has(this.status) && type(this.status) == string
      && this.status == 'active')"""


def _pub(document: dict[str, Any], operation: str | None = None) -> dict[str, Any]:
    fixture = _fx(document)
    if operation is not None:
        fixture["ctx"] = {"operation": operation}
    return fixture


ODCS_PUBLICATION_V1: Final[RuleSetContent] = RuleSetContent.model_validate(
    {
        "dsl_version": "cel-2",
        "rules": [
            _rule2(
                "published-status-active",
                "At publish, the document's ODCS status is 'active'.",
                "$",
                _PUBLISHED_STATUS_ACTIVE,
                "warning",
                "This version was published while the document's status is not 'active'. "
                "Consumers usually expect status 'active' for a published version.",
                [
                    _pub({"status": "active"}, "publish"),
                    _pub({"status": "draft"}, "save"),
                    _pub({"status": "draft"}),
                    _pub({"status": "proposed"}, "save"),
                ],
                [
                    _pub({"status": "draft"}, "publish"),
                    _pub({"apiVersion": "v3.2.0"}, "publish"),
                    _pub({"status": "deprecated"}, "publish"),
                    _pub({"status": 1}, "publish"),
                ],
            ),
        ],
    }
)


# -- odcs-publication v2 (quick 261128-rsa) ------------------------------------------------------

PLATFORM_ODCS_PUBLICATION_V2_ID: Final[UUID] = UUID("31ce56ee-98cc-453b-b406-766cbf044bcd")
PLATFORM_PUBLICATION_V2_EVENT_ID: Final[UUID] = UUID("5dd557d9-7891-4a98-a357-2a2db9dcff5a")

ODCS_PUBLICATION_V2_DESCRIPTION: Final = (
    "Platform publication checks for ODCS contracts, run at publish: a released version needs "
    "status active, a Preview gets a warning."
)

# A SemVer prerelease (``1.2.0-rc.1``): a hyphen right after the three core numbers. Build metadata
# alone (``1.0.0+build-1``) is a release. Anything else, a missing or non-string version included,
# is judged as a release (fail closed; contracts refuses such a publish as invalid-version anyway).
_IS_PRERELEASE = (
    "has(this.version) && type(this.version) == string"
    " && this.version.matches('^[0-9]+[.][0-9]+[.][0-9]+-')"
)
_IS_ACTIVE = "has(this.status) && type(this.status) == string && this.status == 'active'"

_PREVIEW_STATUS_ACTIVE = f"""\
!has(ctx.operation) || ctx.operation != 'publish'
  || !(type(this) == map && {_IS_PRERELEASE})
  || (type(this) == map && {_IS_ACTIVE})"""

_RELEASED_STATUS_ACTIVE = f"""\
!has(ctx.operation) || ctx.operation != 'publish'
  || (type(this) == map && (({_IS_ACTIVE}) || ({_IS_PRERELEASE})))"""


ODCS_PUBLICATION_V2: Final[RuleSetContent] = RuleSetContent.model_validate(
    {
        "dsl_version": "cel-2",
        "rules": [
            _rule2(
                "published-status-active",
                "At a prerelease publish (a Preview), the document's ODCS status is 'active'.",
                "$",
                _PREVIEW_STATUS_ACTIVE,
                "warning",
                "This prerelease version was published while the document's status is not "
                "'active'. A released version (no prerelease part) needs status: active.",
                [
                    _pub({"version": "1.2.0-rc.1", "status": "active"}, "publish"),
                    _pub({"version": "1.0.0", "status": "draft"}, "publish"),
                    _pub({"status": "draft"}, "publish"),
                    _pub({"version": "1.2.0-rc.1", "status": "draft"}, "save"),
                    _pub({"version": "1.2.0-rc.1", "status": "draft"}),
                ],
                [
                    _pub({"version": "1.2.0-rc.1", "status": "draft"}, "publish"),
                    _pub({"version": "1.2.0-rc.1", "status": "proposed"}, "publish"),
                    _pub({"version": "2.0.0-alpha+build.5"}, "publish"),
                    _pub({"version": "1.2.0-rc.1", "status": 1}, "publish"),
                ],
            ),
            _rule2(
                "released-status-active",
                "At a release publish (no prerelease part), the document's ODCS status is "
                "'active'.",
                "$",
                _RELEASED_STATUS_ACTIVE,
                "error",
                "A released version needs status: active. Set status: active, or publish a "
                "prerelease version (for example 1.2.0-rc.1) as a Preview.",
                [
                    _pub({"version": "1.0.0", "status": "active"}, "publish"),
                    _pub({"version": "1.2.0-rc.1", "status": "draft"}, "publish"),
                    _pub({"version": "1.2.0-rc.1"}, "publish"),
                    _pub({"version": "1.0.0", "status": "draft"}, "save"),
                    _pub({"version": "1.0.0", "status": "draft"}),
                ],
                [
                    _pub({"version": "1.0.0", "status": "draft"}, "publish"),
                    _pub({"version": "1.0.0"}, "publish"),
                    _pub({"version": "1.0.0+build-1", "status": "draft"}, "publish"),
                    _pub({"version": "1.0.0", "status": "Active"}, "publish"),
                    _pub({"status": "draft"}, "publish"),
                ],
            ),
        ],
    }
)
