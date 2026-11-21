"""SUB-08 (D-163, D-164): who is told about a breaking publish, and what the notice carries.

`plan_breaking_notices` is pure: cards are grouped by the set of affected pinned versions, the
sections are ordered by SemVer precedence, and the payload is built from an allow-list.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from dc_contracts.notices import (
    EVENT_HANDLERS,
    MAX_BREAKING_CHANGES,
    MAX_BREAKING_PIN_VERSIONS,
    NoticePlan,
    PinnedDiff,
    cap_pinned_versions,
    notice_payload,
    plan_breaking_notices,
)

pytestmark = pytest.mark.unit

CONTRACT = uuid.uuid4()
NEW = uuid.uuid4()
V100, V190, V110 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
SEMVERS = {V100: "1.0.0", V190: "1.9.0", V110: "1.10.0"}


def change(index: int = 0, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "code": "property-removed",
        "class": "breaking",
        "element_kind": "property",
        "path": f"schema.orders.properties.col{index}",
        "attribute": None,
        "before": None,
        "after": None,
    }
    entry.update(extra)
    return entry


def pinned(version: uuid.UUID, count: int) -> PinnedDiff:
    return PinnedDiff(version, SEMVERS[version], count, tuple(change(i) for i in range(count)))


def plan(pins: dict[str, list[uuid.UUID]], diffs: dict[uuid.UUID, PinnedDiff], **kw: Any) -> Any:
    return plan_breaking_notices(
        pins=pins,
        diffs=diffs,
        contract_id=CONTRACT,
        odcs_id="urn:dc:orders",
        new_version_id=NEW,
        new_semver="2.0.0",
        classifier_version="odcs-compat-1",
        **kw,
    )


def test_cards_are_grouped_by_their_affected_pinned_versions() -> None:
    # 1.0.0 -> 2.0.0 breaks, 1.9.0 -> 2.0.0 does not.
    diffs = {V100: pinned(V100, 1), V190: pinned(V190, 0)}
    groups = plan({"A": [V100], "B": [V100], "C": [V190], "D": [V100, V190]}, diffs)

    assert [g.card_ids for g in groups] == [("A", "B"), ("D",)]
    for group in groups:
        # card D also pins 1.9.0, which does not break: exactly one section (1.0.0)
        assert [s["semver"] for s in group.payload["pinned_versions"]] == ["1.0.0"]
    # A and B share one payload object content; C has no plan at all
    assert groups[0].payload == groups[1].payload
    assert all("C" not in g.card_ids for g in groups)


def test_a_publish_with_no_pins_or_no_breaking_change_plans_nothing() -> None:
    assert plan({}, {}) == []
    assert plan({"A": [V100]}, {V100: pinned(V100, 0)}) == []


def test_sections_follow_semver_precedence_ascending_not_text_or_pin_order() -> None:
    diffs = {v: pinned(v, 1) for v in (V100, V190, V110)}
    (group,) = plan({"A": [V110, V100, V190]}, diffs)
    assert [s["semver"] for s in group.payload["pinned_versions"]] == ["1.0.0", "1.9.0", "1.10.0"]


def test_changes_keep_the_classifier_order_and_are_capped_with_a_total() -> None:
    (group,) = plan({"A": [V100]}, {V100: pinned(V100, 60)})
    (section,) = group.payload["pinned_versions"]
    assert section["breaking_count"] == 60
    assert section["changes_total"] == 60
    assert len(section["changes"]) == MAX_BREAKING_CHANGES == 50
    assert [c["path"] for c in section["changes"]] == [
        f"schema.orders.properties.col{i}" for i in range(50)
    ]
    assert section["diff_url"] == (
        f"/api/v1/contracts/contracts/{CONTRACT}/diff?from=1.0.0&to=2.0.0"
    )


def test_the_diff_url_query_encodes_build_metadata() -> None:
    """IN-01: a raw "+" decodes as a space and the diff link answered 404."""
    from urllib.parse import parse_qs, urlsplit

    built = uuid.uuid4()
    diffs = {built: PinnedDiff(built, "1.0.0+build.5", 1, (change(0),))}
    (group,) = plan_breaking_notices(
        pins={"A": [built]},
        diffs=diffs,
        contract_id=CONTRACT,
        odcs_id="urn:dc:orders",
        new_version_id=NEW,
        new_semver="2.0.0+build.6",
        classifier_version="odcs-compat-1",
    )
    (section,) = group.payload["pinned_versions"]
    url = section["diff_url"]
    assert url == (
        f"/api/v1/contracts/contracts/{CONTRACT}/diff?from=1.0.0%2Bbuild.5&to=2.0.0%2Bbuild.6"
    )
    query = parse_qs(urlsplit(url).query)
    assert query == {"from": ["1.0.0+build.5"], "to": ["2.0.0+build.6"]}


def test_the_payload_has_exactly_the_allow_listed_keys() -> None:
    (group,) = plan({"A": [V100]}, {V100: pinned(V100, 1)}, truncated_pins=3)
    payload = group.payload
    assert set(payload) == {
        "contract",
        "version",
        "classifier_version",
        "truncated_pins",
        "pinned_versions",
    }
    assert payload["version"] == {"id": str(NEW), "semver": "2.0.0"}
    assert payload["truncated_pins"] == 3
    (section,) = payload["pinned_versions"]
    assert set(section) == {
        "version_id",
        "semver",
        "breaking_count",
        "changes_total",
        "changes",
        "diff_url",
    }
    assert set(section["changes"][0]) == {"code", "path", "attribute"}


def test_values_appear_only_for_allow_listed_attributes() -> None:
    allowed = change(0, code="constraint-tightened", attribute="maxLength", before=10, after=5)
    text_attr = change(1, code="attribute-changed", attribute="description", before="a", after="b")
    # element kind "server" may carry ``type``, any other element may not
    server = change(2, element_kind="server", attribute="type", before="x", after="y")
    other = change(3, element_kind="property", attribute="type", before="x", after="y")
    diff = PinnedDiff(V100, "1.0.0", 4, (allowed, text_attr, server, other))
    (group,) = plan({"A": [V100]}, {V100: diff})
    entries = group.payload["pinned_versions"][0]["changes"]
    assert (entries[0]["before"], entries[0]["after"]) == (10, 5)
    assert "before" not in entries[1]
    assert (entries[2]["before"], entries[2]["after"]) == ("x", "y")
    assert "before" not in entries[3]


@pytest.mark.parametrize("key", ["description", "examples", "quality", "query", "customProperties"])
def test_the_guard_rejects_document_content_keys_at_any_depth(key: str) -> None:
    with pytest.raises(ValueError, match="must not carry"):
        notice_payload(
            contract_id=CONTRACT,
            odcs_id="urn:dc:orders",
            version_id=NEW,
            semver="2.0.0",
            pinned_versions=[{"changes": [{key: "text"}]}],
        )


def test_more_pinned_versions_than_the_cap_keep_the_newest() -> None:
    ids = {uuid.uuid4(): f"1.{minor}.0" for minor in range(MAX_BREAKING_PIN_VERSIONS + 4)}
    kept, truncated = cap_pinned_versions(ids)
    assert len(kept) == MAX_BREAKING_PIN_VERSIONS
    assert truncated == 4
    # newest first: 1.28.0 down to 1.4.0; the four oldest are the ones left out
    assert [ids[v] for v in kept][:2] == ["1.28.0", "1.27.0"]
    assert {ids[v] for v in kept}.isdisjoint({"1.0.0", "1.1.0", "1.2.0", "1.3.0"})


def test_a_cap_that_is_not_reached_leaves_nothing_out() -> None:
    kept, truncated = cap_pinned_versions(SEMVERS)
    assert sorted(kept) == sorted(SEMVERS)
    assert truncated == 0


def test_the_published_event_has_both_handlers_and_each_other_event_exactly_one() -> None:
    assert len(EVENT_HANDLERS["contract.version_published"]) == 2
    others = {k: v for k, v in EVENT_HANDLERS.items() if k != "contract.version_published"}
    assert all(len(handlers) == 1 for handlers in others.values())


def test_a_plan_carries_its_own_dedupe_key() -> None:
    assert "dedupe_key" in NoticePlan._fields
