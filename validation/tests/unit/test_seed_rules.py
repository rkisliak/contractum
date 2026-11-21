"""The seeded odcs-structural rules, proven in the real engine (plan 05-13; D-130, D-139, VAL-01).

A real worker subprocess evaluates every rule. The set is enforced from version 1, so an evaluation
error in one of its rules would block publish for every contract: the odd-typed documents below
must give a pass or a fail, never an error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from dc_rules.checks import EMPTY_REFS, assemble_check, check_units, static_problems
from dc_rules.model import RuleDef, RuleSetContent, content_sha256
from dc_rules.ports import Limits
from dc_rules.protocol import EvalUnit
from dc_validation.rulesets import fixture_changes
from dc_validation.sandbox import Sandbox
from dc_validation.seed import (
    ODCS_CONSISTENCY_V1,
    ODCS_PUBLICATION_V1,
    ODCS_REFERENCES_V1,
    ODCS_STRUCTURAL_NAME,
    ODCS_STRUCTURAL_V1,
    PLATFORM_ODCS_STRUCTURAL_ID,
    SEED_ACTOR,
)
from dc_validation.settings import ValidationSettings

pytestmark = pytest.mark.unit

# The hash migration 0005 stores. Editing ODCS_STRUCTURAL_V1 changes it: a change ships as a new
# constant and a new migration, never as an edit of version 1.
V1_CONTENT_SHA256 = "6c351add6890e81d4ca854cff4926b904c7450cebdaa3b48a2f6bf36755f4e7a"
RULES: dict[str, RuleDef] = {rule.key: rule for rule in ODCS_STRUCTURAL_V1.rules}


@asynccontextmanager
async def sandbox() -> AsyncIterator[Sandbox]:
    box = Sandbox(
        ValidationSettings(
            database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never",  # type: ignore[arg-type]
            eval_workers=1,
        )
    )
    try:
        yield box
    finally:
        await box.aclose()


async def outcomes(document: Any) -> dict[str, str]:
    """Evaluate every seed rule on ``document``: rule key -> pass, fail or error."""
    units = [EvalUnit(id=i, rule=rule) for i, rule in enumerate(ODCS_STRUCTURAL_V1.rules)]
    async with sandbox() as box:
        results = await box.evaluate(units, document)
    return {unit.rule.key: results[unit.id].outcome for unit in units}


def table(*properties: Any, name: Any = "orders") -> dict[str, Any]:
    return {"schema": [{"name": name, "properties": list(properties)}]}


def key(name: str, position: Any = None, *, primary: Any = True) -> dict[str, Any]:
    prop: dict[str, Any] = {"name": name, "primaryKey": primary}
    if position is not None:
        prop["primaryKeyPosition"] = position
    return prop


def test_the_seed_is_one_small_versioned_set_with_a_stable_hash_and_sorted_keys() -> None:
    assert ODCS_STRUCTURAL_NAME == "odcs-structural"
    keys = [rule.key for rule in ODCS_STRUCTURAL_V1.rules]
    assert keys == [
        "logical-type-option-bounds",
        "primary-key-positions-valid",
        "property-names-unique",
        "schema-names-unique",
    ]
    assert keys == sorted(keys)
    assert {r.key: r.severity for r in ODCS_STRUCTURAL_V1.rules} == {
        "logical-type-option-bounds": "warning",
        "primary-key-positions-valid": "error",
        "property-names-unique": "error",
        "schema-names-unique": "error",
    }
    assert content_sha256(ODCS_STRUCTURAL_V1) == V1_CONTENT_SHA256
    assert SEED_ACTOR.id == "system:validation-seed"
    assert str(PLATFORM_ODCS_STRUCTURAL_ID) == "05d2384d-a57d-4922-a576-921b2f7113bd"


def test_every_seed_rule_has_a_pass_and_a_fail_fixture() -> None:
    for rule in ODCS_STRUCTURAL_V1.rules:
        assert rule.fixtures.pass_, rule.key
        assert rule.fixtures.fail, rule.key


@pytest.mark.timing
async def test_the_rule_set_check_is_clean_through_the_real_worker() -> None:
    limits = Limits()
    static = static_problems(ODCS_STRUCTURAL_V1, limits)
    assert static == []
    async with sandbox() as box:
        results = await box.check(check_units(ODCS_STRUCTURAL_V1, limits))

    report = assemble_check(ODCS_STRUCTURAL_V1, static, results, limits)

    assert report.rule_set_problems == []
    assert [(r.rule_key, r.ok, r.problems) for r in report.rules] == [
        (rule.key, True, []) for rule in ODCS_STRUCTURAL_V1.rules
    ]
    assert report.ok is True


ODD_DOCUMENTS = {
    "odd field types": {
        "schema": [
            {
                "name": 5,
                "properties": [
                    {
                        "name": 7,
                        "primaryKey": "yes",
                        "primaryKeyPosition": "1",
                        "logicalTypeOptions": [1, 2],
                    }
                ],
            }
        ]
    },
    "properties is an object": {"schema": [{"name": "t", "properties": {"a": {"name": "a"}}}]},
    "schema is missing": {},
    "schema is a string": {"schema": "orders"},
    "schema holds non-objects": {"schema": [1, None, "s", [1], True]},
    "properties hold non-objects": {
        "schema": [
            {"name": "t", "properties": [1, None, "x", [2], {"name": None, "primaryKey": None}]}
        ]
    },
    "options of every wrong type": table(
        {
            "name": "a",
            "logicalTypeOptions": {
                "minLength": "a",
                "maxLength": None,
                "minimum": True,
                "maximum": [1],
                "minItems": {},
                "maxItems": 2.5,
            },
        }
    ),
    "positions of every wrong type": table(
        key("a", 1.5), key("b", None, primary=None), key("c", [1]), key("d", True), key("e", 2**70)
    ),
    "root is a list": [],
    "root is a string": "contract",
    "root is null": None,
}


@pytest.mark.parametrize("document", ODD_DOCUMENTS.values(), ids=ODD_DOCUMENTS.keys())
async def test_no_rule_errors_on_a_document_whose_fields_have_unexpected_types(
    document: Any,
) -> None:
    got = await outcomes(document)

    assert set(got) == set(RULES)
    assert all(outcome in ("pass", "fail") for outcome in got.values()), got


@pytest.mark.parametrize(
    "properties",
    [
        pytest.param([key("id")], id="a primary key without a position takes the ODCS default -1"),
        pytest.param([key("id", -1), key("region", -1)], id="explicit -1 on every key"),
        pytest.param([key("id", 1), key("region", 2)], id="positions 1 and 2"),
        pytest.param([key("id", 2), key("region", 1)], id="positions in reverse order"),
        pytest.param([key("id", 1), key("note", primary=False)], id="a non-key needs no position"),
        pytest.param(
            [key("id", 3, primary=False), key("a", 1), key("b", 2)], id="non-key position"
        ),
        pytest.param([], id="no properties"),
    ],
)
async def test_primary_key_positions_that_are_valid_pass(properties: list[Any]) -> None:
    assert (await outcomes(table(*properties)))["primary-key-positions-valid"] == "pass"


@pytest.mark.parametrize(
    "properties",
    [
        pytest.param([key("id", 1), key("region", 1)], id="positions 1 and 1"),
        pytest.param([key("id", 1), key("region", 3)], id="positions 1 and 3 among two keys"),
        pytest.param([key("id", 1), key("region")], id="one key has a position, another none"),
        pytest.param([key("id", 1), key("region", -1)], id="one key has a position, another -1"),
        pytest.param([key("id", 0), key("region", 1)], id="a position below 1"),
        pytest.param([key("id", 2)], id="a single key at position 2"),
    ],
)
async def test_primary_key_positions_that_break_the_rule_fail(properties: list[Any]) -> None:
    assert (await outcomes(table(*properties)))["primary-key-positions-valid"] == "fail"


async def test_property_and_schema_names_are_judged_per_object_and_per_contract() -> None:
    duplicated = {
        "schema": [
            {"name": "orders", "properties": [{"name": "id"}, {"name": "id"}]},
            {"name": "orders", "properties": [{"name": "id"}, {"name": "name"}]},
        ]
    }
    got = await outcomes(duplicated)
    assert got["property-names-unique"] == "fail"
    assert got["schema-names-unique"] == "fail"

    # The same property name in two different objects is fine.
    separate = {
        "schema": [
            {"name": "orders", "properties": [{"name": "id"}]},
            {"name": "customers", "properties": [{"name": "id"}]},
        ]
    }
    got = await outcomes(separate)
    assert got["property-names-unique"] == "pass"
    assert got["schema-names-unique"] == "pass"


async def test_option_bounds_warn_only_on_an_inverted_pair() -> None:
    def with_options(**options: Any) -> dict[str, Any]:
        return table({"name": "a", "logicalTypeOptions": options})

    rule = "logical-type-option-bounds"
    assert (await outcomes(with_options(minLength=2, maxLength=2)))[rule] == "pass"
    assert (await outcomes(with_options(minimum=-5, maximum=2.5)))[rule] == "pass"
    assert (await outcomes(with_options(minLength=2)))[rule] == "pass"  # one bound alone
    assert (await outcomes(with_options(minItems=4, maxItems=1)))[rule] == "fail"
    assert (await outcomes(with_options(minLength=1, maxLength=5, minimum=9, maximum=3)))[
        rule
    ] == "fail"


# -- the semantic sets (plan 06-09; D-150, D-151) -----------------------------------------------

# The hashes migration 0006 stores. The constants are never edited after this plan: a change ships
# as a new constant and a new migration.
CONSISTENCY_V1_SHA256 = "afbcfeb2ae7585d3d04231e0643b143185bebe56141234edd01f7d7eb8f62c23"
REFERENCES_V1_SHA256 = "f877858d8902e614c7f62e53d4b27d125c38825c76c3e1074e15f38a0deb79a4"

# The hash migration 0009 stores (quick 261119-rpi). Never edited: a change is a new constant.
PUBLICATION_V1_SHA256 = "5268111919adcfa52e806f06f53eaf5cbc8289e00508a09531a84fc13156bbe3"

SEEDED_SETS: dict[str, RuleSetContent] = {
    "odcs-structural": ODCS_STRUCTURAL_V1,
    "odcs-consistency": ODCS_CONSISTENCY_V1,
    "odcs-references": ODCS_REFERENCES_V1,
    "odcs-publication": ODCS_PUBLICATION_V1,
}


def test_the_semantic_sets_are_pinned_cel2_with_sorted_keys_and_the_d151_severities() -> None:
    assert ODCS_STRUCTURAL_V1.dsl_version == "cel-1"
    assert content_sha256(ODCS_STRUCTURAL_V1) == V1_CONTENT_SHA256
    assert content_sha256(ODCS_CONSISTENCY_V1) == CONSISTENCY_V1_SHA256
    assert content_sha256(ODCS_REFERENCES_V1) == REFERENCES_V1_SHA256
    for content in (ODCS_CONSISTENCY_V1, ODCS_REFERENCES_V1):
        assert content.dsl_version == "cel-2"
        keys = [rule.key for rule in content.rules]
        assert keys == sorted(keys)
    assert {r.key: r.severity for r in ODCS_CONSISTENCY_V1.rules} == {
        "logical-physical-coherent": "warning",
        "partition-key-position": "warning",
        "pii-needs-classification": "warning",
        "pii-needs-retention": "warning",
        "primary-key-required-unique": "error",
        "quality-element-resolves": "error",
        "relationship-target-resolves": "error",
        "sla-element-resolves": "error",
    }
    assert {r.key: r.severity for r in ODCS_REFERENCES_V1.rules} == {
        "linked-contract-published": "error",
        "server-reference-exists": "error",
        "team-exists": "warning",
        "team-member-exists": "warning",
    }
    # odcs-structural alone judges primary-key positions; the consistency set must not repeat it.
    assert "primary-key-positions-valid" not in {r.key for r in ODCS_CONSISTENCY_V1.rules}


def test_every_semantic_rule_has_pass_and_fail_fixtures_and_never_reads_previous_unguarded() -> (
    None
):
    for content in (ODCS_CONSISTENCY_V1, ODCS_REFERENCES_V1):
        for rule in content.rules:
            assert rule.fixtures.pass_, rule.key
            assert rule.fixtures.fail, rule.key
            if "previous" in rule.condition:
                assert "previous == null ||" in rule.condition, rule.key


@pytest.mark.timing
@pytest.mark.parametrize("name", SEEDED_SETS)
async def test_every_seeded_fixture_and_the_scale_probe_pass_through_the_real_worker(
    name: str,
) -> None:
    """Pass fixtures pass, fail fixtures fail, and each rule survives the 100 x 100 probe.

    This is the same check a Policy Manager's publish runs, with the dc_odcs.diff-derived
    ``changes`` for fixtures that carry ``previous``: a rule that errored or blew its budget on
    the probe, or let a fixture through the wrong way, shows up as a problem here.
    """
    content = SEEDED_SETS[name]
    limits = Limits()
    static = static_problems(content, limits)
    assert static == []
    async with sandbox() as box:
        results = await box.check(check_units(content, limits, changes_for=fixture_changes))

    report = assemble_check(content, static, results, limits)

    assert report.rule_set_problems == []
    assert [(r.rule_key, r.ok, r.problems) for r in report.rules] == [
        (rule.key, True, []) for rule in content.rules
    ]
    assert report.ok is True


async def semantic_outcomes(
    content: RuleSetContent, document: Any, refs: dict[str, Any] | None = None
) -> dict[str, str]:
    units = [EvalUnit(id=i, rule=rule, dsl_version="cel-2") for i, rule in enumerate(content.rules)]
    async with sandbox() as box:
        results = await box.evaluate(
            units, document, previous=None, changes=[], refs=EMPTY_REFS if refs is None else refs
        )
    return {unit.rule.key: results[unit.id].outcome for unit in units}


def odd_object(**fields: Any) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": "a", **fields}]}]}


SEMANTIC_ODD_DOCUMENTS = {
    **ODD_DOCUMENTS,
    "pii tags and custom properties of every wrong type": odd_object(
        tags="pii", customProperties={"property": "pii", "value": True}, classification=5
    ),
    "pii custom property entries of every wrong type": odd_object(
        customProperties=[1, None, "pii", {"property": 3}, {"property": "pii"}, {"value": True}]
    ),
    "pii tag entries of every wrong type": odd_object(tags=[1, None, {"a": 1}, ["pii"]]),
    "types of every wrong kind": odd_object(logicalType=5, physicalType=["x"], partitioned="yes"),
    "partition fields of every wrong kind": odd_object(partitioned=True, partitionKeyPosition="1"),
    "sla of every wrong kind": {
        "slaProperties": [1, None, "x", {"element": 5}, {"element": None}, {"element": ""}]
    },
    "sla is an object": {"slaProperties": {"element": "a.b"}},
    "sla element lists with blanks": {"slaProperties": [{"element": " , a.b ,, "}]},
    "retention sla of every wrong kind": {
        "schema": [{"name": "o", "properties": [{"name": "e", "tags": ["pii"]}]}],
        "slaProperties": [{"property": "retention", "element": 7}, {"property": 7}, 3, None],
    },
    "relationships of every wrong kind": {
        "relationships": [1, None, "x", {"from": 5, "to": [None, 1, {"a": 1}]}, {"to": {"a": 1}}],
        "schema": [{"name": "o", "relationships": "x", "properties": [{"relationships": [[]]}]}],
    },
    "quality of every wrong kind": {
        "schema": [
            {
                "name": "o",
                "quality": [1, None, {"arguments": 5}, {"arguments": {"properties": 5}}],
                "properties": [{"name": "p", "quality": {"arguments": {"properties": ["x"]}}}],
            }
        ]
    },
    "links of every wrong kind": {
        "authoritativeDefinitions": [1, None, {"url": 5}, {"url": None}, {"url": "x"}],
        "relationships": [{"to": [5, None, "/api/v1/contracts/contracts/z"]}],
    },
    "team of every wrong kind": {"team": {"name": 5, "members": [1, None, {"username": 5}]}},
    "team is a string": {"team": "payments"},
    "team members is an object": {"team": {"members": {"username": "a"}}},
    "server properties of every wrong kind": odd_object(
        customProperties=[{"property": "server"}, {"property": "server", "value": [1, None]}]
    ),
}


@pytest.mark.parametrize(
    "document", SEMANTIC_ODD_DOCUMENTS.values(), ids=SEMANTIC_ODD_DOCUMENTS.keys()
)
async def test_no_semantic_rule_errors_on_a_document_whose_fields_have_unexpected_types(
    document: Any,
) -> None:
    """An error outcome in an enforced mandatory rule blocks publish (D-139): never an error."""
    for content in (ODCS_CONSISTENCY_V1, ODCS_REFERENCES_V1):
        got = await semantic_outcomes(content, document)

        assert set(got) == {rule.key for rule in content.rules}
        assert all(outcome in ("pass", "fail") for outcome in got.values()), got


def test_the_publication_set_is_one_cel2_warning_with_a_pinned_hash() -> None:
    assert content_sha256(ODCS_PUBLICATION_V1) == PUBLICATION_V1_SHA256
    assert ODCS_PUBLICATION_V1.dsl_version == "cel-2"
    assert [(r.key, r.severity) for r in ODCS_PUBLICATION_V1.rules] == [
        ("published-status-active", "warning")
    ]
    message = ODCS_PUBLICATION_V1.rules[0].message
    assert "{" not in message and "$" not in message  # static text, never templated (T-rpi-01)


PUBLICATION_ODD_DOCUMENTS: dict[str, Any] = {
    **ODD_DOCUMENTS,
    "status draft": {"status": "draft"},
    "status absent": {"apiVersion": "v3.2.0"},
    "status integer": {"status": 1},
    "status null": {"status": None},
    "status list": {"status": ["active"]},
    "status map": {"status": {"value": "active"}},
    "status bool": {"status": True},
    "status mixed case": {"status": "Active"},
    "root is a list": [{"status": "active"}],
    "root is a string": "active",
    "root is null": None,
}


@pytest.mark.parametrize("operation", [None, "save", "publish"])
@pytest.mark.parametrize(
    "document", PUBLICATION_ODD_DOCUMENTS.values(), ids=PUBLICATION_ODD_DOCUMENTS.keys()
)
async def test_the_publication_rule_never_errors_under_any_ctx_operation(
    document: Any, operation: str | None
) -> None:
    """A warning never blocks (D-124), but an error outcome would still be a broken rule."""
    ctx = {"folder_kind": "team", "folder_path": "a", "organization_id": None}
    if operation is not None:
        ctx["operation"] = operation
    units = [
        EvalUnit(id=i, rule=rule, dsl_version="cel-2")
        for i, rule in enumerate(ODCS_PUBLICATION_V1.rules)
    ]
    async with sandbox() as box:
        results = await box.evaluate(
            units, document, ctx=ctx, previous=None, changes=[], refs=EMPTY_REFS
        )

    assert [results[unit.id].outcome for unit in units] in (["pass"], ["fail"])
    if operation != "publish":
        assert results[units[0].id].outcome == "pass"
