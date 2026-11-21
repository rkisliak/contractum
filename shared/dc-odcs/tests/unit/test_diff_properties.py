"""VAL-02 invariants of the contract diff under generated input (D-144, T-6-01).

Properties: a document equals itself, permuting every keyed array changes nothing, compare is
deterministic and never mutates its inputs, duplicate names never crash it, and a huge change set
is bounded by ``max_changes`` while the true totals are kept.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from dc_odcs.diff import MAX_DIFF_CHANGES, compare
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.unit

# A tiny alphabet makes collisions (duplicates, shared names across documents) likely.
_NAME = st.text(alphabet="abcde", min_size=1, max_size=2)
_TYPES = st.sampled_from(["string", "integer", "date", "array"])
_CASE = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _names(unique: bool, max_size: int = 4) -> st.SearchStrategy[list[str]]:
    return st.lists(_NAME, max_size=max_size, unique=unique)


@st.composite
def _property(draw: st.DrawFn, name: str, unique: bool, depth: int = 0) -> dict[str, Any]:
    prop: dict[str, Any] = {"name": name}
    if draw(st.booleans()):
        prop["logicalType"] = draw(_TYPES)
    if draw(st.booleans()):
        prop["required"] = draw(st.booleans())
    if draw(st.booleans()):
        prop["logicalTypeOptions"] = {"maxLength": draw(st.integers(1, 99))}
    if draw(st.booleans()):
        prop["tags"] = draw(_names(True))
    if draw(st.booleans()):
        prop["quality"] = draw(
            st.lists(
                st.fixed_dictionaries(
                    {"type": st.just("library"), "metric": _NAME, "mustBe": st.integers(0, 3)}
                ),
                max_size=3,
            )
        )
    if depth == 0 and draw(st.booleans()):
        prop["properties"] = [
            draw(_property(inner, unique, 1)) for inner in draw(_names(unique, 3))
        ]
    return prop


@st.composite
def _documents(draw: st.DrawFn, unique: bool) -> dict[str, Any]:
    objects = []
    for name in draw(_names(unique, 3)):
        obj: dict[str, Any] = {
            "name": name,
            "properties": [draw(_property(p, unique)) for p in draw(_names(unique))],
        }
        objects.append(obj)
    if unique:
        # ids are optional but unique inside one collection
        for index, obj in enumerate(objects):
            if draw(st.booleans()):
                obj["id"] = f"o{index}"
            for position, prop in enumerate(obj["properties"]):
                if draw(st.booleans()):
                    prop["id"] = f"o{index}p{position}"
    document: dict[str, Any] = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": "c",
        "version": draw(st.sampled_from(["1.0.0", "1.1.0", "2.0.0"])),
        "schema": objects,
    }
    if draw(st.booleans()):
        document["servers"] = [
            {"server": name, "type": draw(st.sampled_from(["postgres", "mysql"]))}
            for name in draw(_names(unique, 3))
        ]
    if draw(st.booleans()):
        document["slaProperties"] = [
            {"property": prop, "element": element, "value": draw(st.integers(0, 9))}
            for prop in draw(_names(True, 2))
            for element in draw(_names(True, 2))
        ]
    if draw(st.booleans()):
        document["roles"] = [{"role": name} for name in draw(_names(unique, 3))]
    if draw(st.booleans()):
        document["team"] = {"members": [{"username": u} for u in draw(_names(unique, 3))]}
    if draw(st.booleans()):
        document["tags"] = draw(_names(True))
    return document


_KEYED_LISTS = {"schema", "properties", "servers", "slaProperties", "roles", "members", "tags",
                "quality"}  # fmt: skip


def _permute(node: Any, draw: st.DrawFn, key: str = "") -> Any:
    """A copy of ``node`` with every keyed array permuted (and ``team`` sometimes as an array)."""
    if isinstance(node, dict):
        out = {k: _permute(v, draw, k) for k, v in node.items()}
        if key == "team" and list(out) == ["members"] and draw(st.booleans()):
            return out["members"]  # the deprecated array-of-members form
        return out
    if isinstance(node, list):
        items = [_permute(item, draw, key) for item in node]
        return draw(st.permutations(items)) if key in _KEYED_LISTS else items
    return node


@_CASE
@given(_documents(unique=False))
def test_a_document_equals_itself(document: dict[str, Any]) -> None:
    assert compare(document, document).changes == ()


@_CASE
@given(st.data())
def test_permuting_every_keyed_array_is_not_a_change(data: st.DataObject) -> None:
    document = data.draw(_documents(unique=True))
    shuffled = _permute(document, data.draw)
    assert compare(document, shuffled).changes == ()
    assert compare(shuffled, document).changes == ()


@_CASE
@given(_documents(unique=False), _documents(unique=False))
def test_compare_is_deterministic_ordered_and_never_mutates(
    before: dict[str, Any], after: dict[str, Any]
) -> None:
    snapshot_before, snapshot_after = copy.deepcopy(before), copy.deepcopy(after)
    first = compare(before, after)
    second = compare(copy.deepcopy(before), copy.deepcopy(after))
    assert first == second
    assert before == snapshot_before
    assert after == snapshot_after
    keys = [(c.path, c.code, c.attribute or "") for c in first.changes]
    assert keys == sorted(keys)
    assert first.changes_total == len(first.changes) == sum(first.counts.values())
    assert first.truncated is False


@_CASE
@given(_documents(unique=False), _documents(unique=False))
def test_duplicate_names_never_raise(before: dict[str, Any], after: dict[str, Any]) -> None:
    result = compare(before, after)
    assert result.changes_total == sum(result.counts.values())


def test_a_huge_change_set_is_bounded_but_the_totals_are_true() -> None:
    wide = {
        "name": "wide",
        "properties": [{"name": f"p{index}"} for index in range(20_000)],
    }
    before = {"apiVersion": "v3.2.0", "kind": "DataContract", "id": "c", "schema": [wide]}
    after = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": "c",
        "schema": [{"name": "wide", "properties": []}],
    }
    result = compare(before, after, max_changes=100)
    assert len(result.changes) == 100
    assert result.truncated is True
    assert result.changes_total == 20_000
    assert result.counts == {"breaking": 20_000, "warning": 0, "info": 0}
    keys = [(c.path, c.code, c.attribute or "") for c in result.changes]
    assert keys == sorted(keys)
    again = compare(before, after, max_changes=100)
    assert again == result
    default = compare(before, after)
    assert (len(default.changes), default.truncated) == (MAX_DIFF_CHANGES, True)
    assert default.changes_total == 20_000
