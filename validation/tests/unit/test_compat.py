"""The compatibility section and native findings (plan 06-06; VAL-02, VAL-03, D-147, D-148)."""

from __future__ import annotations

from typing import Any

import pytest
from dc_odcs import MAX_VERSION_CHARS
from dc_odcs.diff import compare
from dc_validation.compat import (
    COMPAT_CHANGES_CAP,
    BaselineRef,
    build_compatibility,
    floor_from_version,
    version_text,
)
from dc_validation.report import NATIVE_SOURCE

pytestmark = pytest.mark.unit

BASELINE = BaselineRef(version_id=None, semver="1.0.0")


def contract(*names: str, version: str = "1.0.0", **extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "kind": "DataContract",
        "apiVersion": "v3.2.0",
        "id": "urn:datacontract:test:orders",
        "version": version,
        "status": "draft",
        "schema": [
            {
                "name": "orders",
                "properties": [{"name": n, "logicalType": "string"} for n in names],
            }
        ],
    }
    document.update(extra)
    return document


def build(
    before: dict[str, Any], after: dict[str, Any], *, floor: int | None = 1, mode: Any = "publish"
):
    return build_compatibility(
        mode=mode,
        baseline=BASELINE,
        result=compare(before, after),
        declared=after["version"],
        stable_major_floor=floor,
    )


def test_a_removed_property_under_a_minor_bump_adds_the_native_blocking_finding() -> None:
    compat = build(contract("id", "amount"), contract("id", version="1.1.0"))

    section = compat.section
    assert section.status == "compared"
    assert section.required_bump == "major"
    assert section.bump_ok is False
    assert section.counts.breaking == 1
    assert section.declared_version == "1.1.0"
    assert section.changes[0].code == "property-removed"
    assert section.changes[0].path == "schema.orders.properties.amount"
    (finding,) = compat.findings
    assert finding.source == NATIVE_SOURCE
    assert finding.rule_key == "semver-breaking-change"
    assert finding.severity == "error"
    assert finding.blocking is True
    assert finding.rule_set.id is None
    assert "2.0.0" in finding.message


def test_a_major_bump_passes_but_the_change_is_still_listed() -> None:
    compat = build(contract("id", "amount"), contract("id", version="2.0.0"))

    assert compat.section.bump_ok is True
    assert compat.section.counts.breaking == 1
    assert compat.section.changes[0].code == "property-removed"
    assert compat.findings == []


def test_a_prerelease_only_history_has_no_floor_and_never_blocks() -> None:
    compat = build(contract("id", "amount"), contract("id", version="0.2.0"), floor=None)

    assert compat.section.bump_ok is True
    assert compat.findings == []
    assert compat.section.changes[0].code == "property-removed"


def test_no_baseline_says_so_and_adds_nothing() -> None:
    compat = build_compatibility(
        mode="publish", baseline=None, result=None, declared="1.0.0", stable_major_floor=1
    )

    assert compat.section.status == "no-baseline"
    assert compat.section.baseline is None
    assert compat.section.changes == []
    assert compat.findings == []


def test_an_unusable_baseline_blocks_publish_and_only_advises_in_save_mode() -> None:
    publish = build_compatibility(
        mode="publish", baseline=BASELINE, result=None, declared="1.1.0", stable_major_floor=1
    )
    save = build_compatibility(
        mode="save", baseline=BASELINE, result=None, declared="1.1.0", stable_major_floor=1
    )

    assert publish.section.status == "unavailable"
    (finding,) = publish.findings
    assert finding.rule_key == "compatibility-unavailable"
    assert finding.blocking is True
    assert finding.source == NATIVE_SOURCE
    assert save.section.status == "unavailable"
    assert save.findings == []


def many_removals(count: int) -> tuple[dict[str, Any], dict[str, Any]]:
    names = [f"p{n:04d}" for n in range(count)]
    return contract(*names), contract(version="2.0.0")


@pytest.mark.parametrize(
    ("count", "listed", "truncated"), [(COMPAT_CHANGES_CAP, 200, False), (201, 200, True)]
)
def test_the_change_list_is_capped_at_200_with_the_true_total(
    count: int, listed: int, truncated: bool
) -> None:
    before, after = many_removals(count)

    section = build(before, after).section

    assert len(section.changes) == listed
    assert section.changes_total == count
    assert section.changes_truncated is truncated
    assert section.counts.breaking == count


def test_values_appear_only_for_allow_listed_attributes() -> None:
    before = contract("id", description="the secret order table")
    after = contract("id", description="another free text", version="1.0.1")
    before["schema"][0]["properties"][0]["description"] = "free text one"
    after["schema"][0]["properties"][0]["description"] = "free text two"
    before["schema"][0]["properties"][0]["logicalType"] = "integer"
    after["schema"][0]["properties"][0]["logicalType"] = "string"

    section = build(before, after).section

    by_attribute = {c.attribute: c for c in section.changes}
    assert by_attribute["logicalType"].before == "integer"
    assert by_attribute["logicalType"].after == "string"
    dumped = section.model_dump_json()
    assert "free text" not in dumped
    assert "secret order table" not in dumped


def test_an_allow_listed_list_value_is_reported_item_by_item() -> None:
    before, after = contract("status"), contract("status", version="2.0.0")
    before["schema"][0]["properties"][0]["logicalTypeOptions"] = {"enum": ["new", "paid"]}
    after["schema"][0]["properties"][0]["logicalTypeOptions"] = {"enum": ["new", "shipped"]}

    section = build(before, after).section

    (change,) = [c for c in section.changes if c.attribute == "enum"]
    assert change.before == ["new", "paid"]
    assert change.after == ["new", "shipped"]


def test_the_change_class_serialises_under_its_wire_name() -> None:
    section = build(contract("id", "amount"), contract("id", version="2.0.0")).section

    (change,) = section.changes
    dumped = change.model_dump(mode="json")
    assert dumped["class"] == "breaking"
    assert "change_class" not in dumped


def test_a_hostile_version_string_never_reaches_the_section() -> None:
    compat = build_compatibility(
        mode="save",
        baseline=BaselineRef(version_id=None, semver="not a version at all"),
        result=compare(contract("id"), contract("id")),
        declared="1.0.0 <script>alert(1)</script>",
        stable_major_floor=None,
    )

    assert compat.section.declared_version is None
    assert compat.section.baseline is not None
    assert compat.section.baseline.semver is None


@pytest.mark.parametrize(
    ("value", "floor"),
    [
        ("1.4.0", 1),
        ("0.9.0", 0),
        ("2.0.0+build", 2),
        ("2.0.0-rc.1", None),
        ("nope", None),
        (3, None),
    ],
)
def test_the_on_demand_floor_is_the_previous_major_unless_a_prerelease(
    value: object, floor: int | None
) -> None:
    assert floor_from_version(value) == floor


def test_version_text_accepts_only_plain_version_characters() -> None:
    assert version_text("1.2.3-rc.1+b5") == "1.2.3-rc.1+b5"
    assert version_text("1 2") is None
    # The display cap is the shared wire cap (CR-02): long CI build metadata is still shown.
    long_build = "1.0.0+build.20261113.sha." + "0123456789abcdef" * 2 + "012345678"
    assert len(long_build) == 66 and version_text(long_build) == long_build
    assert version_text("x" * MAX_VERSION_CHARS) == "x" * MAX_VERSION_CHARS
    assert version_text("x" * (MAX_VERSION_CHARS + 1)) is None
    assert version_text(None) is None
