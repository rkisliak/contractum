"""Mandatory resolution is a pure function of its inputs and an injected clock (plan 05-09 task 2).

Covers the RULE-05 and RULE-08 edges with exact instants, and Hypothesis invariants: order
independence, "never weaken" (adding a set never removes or changes an entry), completeness (no
intermediate version of a rollout is skipped) and the D-142 restore (clear then set never lowers the
enforced version).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from dc_validation.resolution import (
    Attachment,
    CandidateSet,
    ResolvedRuleSet,
    VersionInfo,
    enforced_version_no,
    order_key,
    resolve_explicit,
    resolve_mandatory,
    restamp_on_set,
)
from dc_validation.settings import ValidationSettings
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
GRACE = timedelta(days=14)


def uid(number: int) -> uuid.UUID:
    return uuid.UUID(int=number)


def version(number: int, enforce_at: datetime | None, *, deprecated: bool = False) -> VersionInfo:
    return VersionInfo(
        id=uid(10_000 + number),
        version_no=number,
        dsl_version="1",
        enforce_at=enforce_at,
        deprecated_at=NOW if deprecated else None,
    )


def candidate(
    number: int,
    versions: list[VersionInfo],
    *,
    name: str = "set",
    depth: int = 1,
    paused: bool = False,
) -> CandidateSet:
    return CandidateSet(
        rule_set_id=uid(number),
        name=name,
        scope="platform" if depth == 0 else "folder",
        folder_id=None if depth == 0 else uid(500 + depth),
        folder_kind=None if depth == 0 else "organization",
        depth=depth,
        versions=tuple(versions),
        paused=paused,
    )


def shape(entries: list[ResolvedRuleSet]) -> list[tuple[int, str]]:
    return [(e.version_no, e.mode) for e in entries]


# -- RULE-05 edges --------------------------------------------------------------------------------


def test_a_chain_with_no_mandatory_set_resolves_to_nothing() -> None:
    assert resolve_mandatory([], NOW) == []


def test_the_same_set_reached_twice_appears_once_as_mandatory() -> None:
    rule_set = candidate(1, [version(1, NOW - timedelta(days=1))])
    entries = resolve_mandatory([rule_set, rule_set], NOW)
    assert shape(entries) == [(1, "enforce")]
    assert entries[0].source == "mandatory"


def test_a_set_at_the_contract_folder_depth_resolves_like_any_other() -> None:
    entries = resolve_mandatory([candidate(1, [version(1, NOW)], depth=4)], NOW)
    assert [(e.depth, e.mode) for e in entries] == [(4, "enforce")]


def test_output_is_ordered_by_depth_then_lower_name_then_id_then_mode_then_version() -> None:
    sets = [
        candidate(3, [version(1, NOW - timedelta(days=1))], name="Beta", depth=2),
        candidate(2, [version(1, NOW - timedelta(days=1))], name="alpha", depth=2),
        candidate(9, [version(1, NOW - timedelta(days=1))], name="zeta", depth=0),
        candidate(
            1,
            [version(1, NOW - timedelta(days=1)), version(2, NOW + timedelta(days=1))],
            name="Gamma",
            depth=1,
        ),
    ]
    entries = resolve_mandatory(sets, NOW)
    assert [(e.depth, e.name, e.version_no, e.mode) for e in entries] == [
        (0, "zeta", 1, "enforce"),
        (1, "Gamma", 1, "enforce"),
        (1, "Gamma", 2, "warn"),
        (2, "alpha", 1, "enforce"),
        (2, "Beta", 1, "enforce"),
    ]
    assert entries == sorted(entries, key=order_key)


# -- pause overlay (quick 261118-vpa) --------------------------------------------------------------


def test_a_paused_set_resolves_the_same_versions_all_as_warn_with_the_paused_marker() -> None:
    versions = [version(1, NOW - timedelta(days=1)), version(2, NOW + timedelta(days=3))]
    live = resolve_mandatory([candidate(1, versions, depth=0)], NOW)
    paused = resolve_mandatory([candidate(1, versions, depth=0, paused=True)], NOW)

    assert shape(live) == [(1, "enforce"), (2, "warn")]
    assert shape(paused) == [(1, "warn"), (2, "warn")]
    assert [e.paused for e in paused] == [True, True]
    assert [e.paused for e in live] == [False, False]
    # the real dates stay on the entries; only the mode changes
    assert [e.enforce_at for e in paused] == [e.enforce_at for e in live]
    assert [e.rule_set_version_id for e in paused] == [e.rule_set_version_id for e in live]


def test_the_pause_does_not_change_the_enforced_version_number() -> None:
    versions = [version(1, NOW - timedelta(days=1)), version(2, NOW + timedelta(days=3))]
    assert enforced_version_no(candidate(1, versions, paused=True), NOW) == 1


def test_resume_restores_the_computed_mode_from_the_dates() -> None:
    versions = [version(1, NOW + timedelta(days=1))]
    paused = resolve_mandatory([candidate(1, versions, paused=True)], NOW)
    assert shape(paused) == [(1, "warn")]
    # the same dates one day later, no longer paused: the version enforces without any restamp
    later = NOW + timedelta(days=2)
    assert shape(resolve_mandatory([candidate(1, versions)], later)) == [(1, "enforce")]
    # and a version whose date passed while paused enforces at once on resume
    assert shape(resolve_mandatory([candidate(1, versions, paused=True)], later)) == [(1, "warn")]


# -- RULE-08 edges --------------------------------------------------------------------------------


def test_enforce_at_equal_to_now_is_enforce_and_one_microsecond_later_is_warn() -> None:
    exact = resolve_mandatory([candidate(1, [version(1, NOW)])], NOW)
    assert shape(exact) == [(1, "enforce")]
    later = resolve_mandatory([candidate(1, [version(1, NOW + timedelta(microseconds=1))])], NOW)
    assert shape(later) == [(1, "warn")]


def test_zero_grace_enforces_a_newly_stamped_version_immediately() -> None:
    stamps = restamp_on_set([version(1, None)], None, NOW, timedelta(0))
    assert stamps == {1: NOW}
    entries = resolve_mandatory([candidate(1, [version(1, stamps[1])])], NOW)
    assert shape(entries) == [(1, "enforce")]


def test_a_negative_warn_period_is_refused_by_the_setting() -> None:
    with pytest.raises(ValidationError):
        ValidationSettings(
            database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never",  # type: ignore[arg-type]
            mandatory_warn_days=-1,
        )
    assert (
        ValidationSettings(
            database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never"  # type: ignore[arg-type]
        ).mandatory_warn_days
        == 14
    )


def test_every_version_in_its_warn_window_is_reported_as_time_passes() -> None:
    t2, t3 = NOW + timedelta(days=3), NOW + timedelta(days=9)

    def at(moment: datetime, versions: list[VersionInfo]) -> list[tuple[int, str]]:
        return shape(resolve_mandatory([candidate(1, versions)], moment))

    versions = [version(1, NOW - timedelta(days=5)), version(2, t2), version(3, t3)]
    assert at(NOW, versions) == [(1, "enforce"), (2, "warn"), (3, "warn")]
    assert at(t2, versions) == [(2, "enforce"), (3, "warn")]  # v1 drops out: v2 is newer, enforced
    assert at(t3, versions) == [(3, "enforce")]
    early = [version(1, NOW - timedelta(days=5)), version(2, t2), version(3, NOW)]
    assert at(NOW, early) == [(3, "enforce")]  # an older warn version is superseded


def test_versions_without_an_enforce_at_never_resolve() -> None:
    entries = resolve_mandatory(
        [candidate(1, [version(1, None), version(2, NOW - timedelta(days=1)), version(3, None)])],
        NOW,
    )
    assert shape(entries) == [(2, "enforce")]
    assert resolve_mandatory([candidate(1, [version(1, None)])], NOW) == []


def test_a_deprecated_version_stays_while_enforced_or_inside_its_warn_window() -> None:
    only = resolve_mandatory(
        [candidate(1, [version(1, NOW - timedelta(days=1), deprecated=True)])], NOW
    )
    assert shape(only) == [(1, "enforce")]
    warning = resolve_mandatory(
        [
            candidate(
                1,
                [
                    version(1, NOW - timedelta(days=1)),
                    version(2, NOW + timedelta(days=1), deprecated=True),
                ],
            )
        ],
        NOW,
    )
    assert shape(warning) == [(1, "enforce"), (2, "warn")]


def test_restamp_without_a_restore_stamps_only_the_newest() -> None:
    versions = [version(1, None), version(2, None), version(3, None)]
    assert restamp_on_set(versions, None, NOW, GRACE) == {3: NOW + GRACE}
    assert restamp_on_set([], None, NOW, GRACE) == {}


def test_restamp_with_a_restore_enforces_it_now_and_gives_newer_versions_a_grace() -> None:
    versions = [version(n, None) for n in (1, 2, 3, 4)]
    assert restamp_on_set(versions, 2, NOW, GRACE) == {2: NOW, 3: NOW + GRACE, 4: NOW + GRACE}
    assert restamp_on_set(versions, 4, NOW, GRACE) == {4: NOW}


def test_enforced_version_no_is_the_newest_passed_one() -> None:
    rule_set = candidate(
        1,
        [version(1, NOW - timedelta(days=9)), version(2, NOW), version(3, NOW + timedelta(days=1))],
    )
    assert enforced_version_no(rule_set, NOW) == 2
    assert enforced_version_no(rule_set, NOW - timedelta(days=10)) is None


# -- Hypothesis invariants ------------------------------------------------------------------------

OFFSETS = st.sampled_from([None, -100, -1, 0, 1, 100]) | st.integers(-50, 50)


@st.composite
def version_lists(draw: st.DrawFn) -> list[VersionInfo]:
    count = draw(st.integers(1, 5))
    return [
        version(
            number,
            None if (offset := draw(OFFSETS)) is None else NOW + timedelta(seconds=offset),
            deprecated=draw(st.booleans()),
        )
        for number in range(1, count + 1)
    ]


@st.composite
def candidate_sets(draw: st.DrawFn) -> list[CandidateSet]:
    count = draw(st.integers(1, 6))
    names = st.sampled_from(["alpha", "Alpha", "beta", "Gamma", "delta"])
    return [
        candidate(number, draw(version_lists()), name=draw(names), depth=draw(st.integers(0, 4)))
        for number in range(1, count + 1)
    ]


@settings(max_examples=150, deadline=None)
@given(sets=candidate_sets(), data=st.data())
def test_resolution_does_not_depend_on_input_order(
    sets: list[CandidateSet], data: st.DataObject
) -> None:
    shuffled = data.draw(st.permutations(sets))
    assert resolve_mandatory(shuffled, NOW) == resolve_mandatory(sets, NOW)


@settings(max_examples=150, deadline=None)
@given(sets=candidate_sets(), extra=version_lists(), depth=st.integers(0, 4))
def test_adding_a_mandatory_set_never_removes_or_changes_an_entry(
    sets: list[CandidateSet], extra: list[VersionInfo], depth: int
) -> None:
    before = resolve_mandatory(sets, NOW)
    added = candidate(len(sets) + 100, extra, name="added", depth=depth)
    after = resolve_mandatory([*sets, added], NOW)
    assert all(entry in after for entry in before)


@settings(max_examples=200, deadline=None)
@given(sets=candidate_sets())
def test_every_version_in_a_warn_window_is_reported_and_nothing_else(
    sets: list[CandidateSet],
) -> None:
    entries = resolve_mandatory(sets, NOW)
    for rule_set in sets:
        mine = [e for e in entries if e.rule_set_id == rule_set.rule_set_id]
        enforce = [e for e in mine if e.mode == "enforce"]
        warn = [e for e in mine if e.mode == "warn"]
        assert len(enforce) <= 1
        enforced = enforced_version_no(rule_set, NOW)
        assert [e.version_no for e in enforce] == ([] if enforced is None else [enforced])
        for entry in enforce:
            assert entry.enforce_at is not None and entry.enforce_at <= NOW
        floor = enforced if enforced is not None else 0
        for entry in warn:
            assert entry.enforce_at is not None and entry.enforce_at > NOW
            assert entry.version_no > floor
        expected_warn = {
            v.version_no
            for v in rule_set.versions
            if v.enforce_at is not None and v.enforce_at > NOW and v.version_no > floor
        }
        assert {e.version_no for e in warn} == expected_warn
        assert all(e.source == "mandatory" for e in mine)


@settings(max_examples=200, deadline=None)
@given(
    offsets=st.lists(OFFSETS, min_size=1, max_size=6),
    clear_gap=st.integers(0, 10_000),
    reset_gap=st.integers(0, 10_000),
    grace_seconds=st.integers(1, 100_000),
    published_after=st.integers(0, 3),
)
def test_clear_then_set_never_lowers_the_enforced_version(
    offsets: list[int | None],
    clear_gap: int,
    reset_gap: int,
    grace_seconds: int,
    published_after: int,
) -> None:
    """D-142: record the enforced version at clear time, restamp at re-set time."""
    history = [
        version(n, None if offset is None else NOW + timedelta(seconds=offset))
        for n, offset in enumerate(offsets, start=1)
    ]
    clear_at = NOW + timedelta(seconds=clear_gap)
    enforced = enforced_version_no(candidate(1, history), clear_at)
    if enforced is None:
        return  # nothing was enforced: re-setting is a first-time set, which D-142 leaves alone
    # While cleared more versions may have been published (they carry no date).
    count = len(history)
    published = [version(count + k, None) for k in range(1, published_after + 1)]
    versions = [version(v.version_no, None) for v in history] + published  # clear nulls every date

    reset_at = clear_at + timedelta(seconds=reset_gap)
    grace = timedelta(seconds=grace_seconds)
    stamps = restamp_on_set(versions, enforced, reset_at, grace)
    restored = [version(v.version_no, stamps.get(v.version_no)) for v in versions]

    entries = resolve_mandatory([candidate(1, restored)], reset_at)
    enforce = [e for e in entries if e.mode == "enforce"]
    assert len(enforce) == 1
    assert enforce[0].version_no >= enforced
    assert enforce[0].enforce_at is not None and enforce[0].enforce_at <= reset_at
    newer = {v.version_no for v in versions if v.version_no > enforced}
    assert {e.version_no for e in entries if e.mode == "warn"} == newer
    # Governance never drops while the new grace runs, and older versions stay out.
    assert all(e.version_no >= enforced for e in entries)


# -- explicit attachments (plan 05-11, RULE-09, D-122, D-123) -------------------------------------

ANCESTORS = (uid(501), uid(502))  # ROOT and an organization; sets at depth 1 are held by uid(501)


def attach(number: int, spec: int | str = "latest") -> Attachment:
    return Attachment(rule_set_id=uid(number), version=spec)  # type: ignore[arg-type]


def explicit(
    attachments: list[Attachment],
    candidates: list[CandidateSet],
    *,
    mandatory: frozenset[uuid.UUID] = frozenset(),
    source: str = "explicit",
) -> tuple[list[ResolvedRuleSet], list[object]]:
    by_id = {c.rule_set_id: c for c in candidates}
    resolved, unresolved = resolve_explicit(attachments, by_id, ANCESTORS, mandatory, source)  # type: ignore[arg-type]
    return resolved, list(unresolved)


def test_latest_resolves_to_the_newest_non_deprecated_version_enforced() -> None:
    rule_set = candidate(1, [version(1, None), version(2, None), version(3, None, deprecated=True)])
    resolved, unresolved = explicit([attach(1)], [rule_set])
    assert unresolved == []
    assert [(e.version_no, e.source, e.mode, e.enforce_at) for e in resolved] == [
        (2, "explicit", "enforce", None)
    ]
    assert resolved[0].rule_set_version_id == uid(10_002)
    assert resolved[0].name == "set"
    assert resolved[0].depth == 1


def test_latest_with_every_version_deprecated_is_the_newest_version() -> None:
    rule_set = candidate(1, [version(1, None, deprecated=True), version(2, None, deprecated=True)])
    resolved, unresolved = explicit([attach(1)], [rule_set])
    assert unresolved == []
    assert [e.version_no for e in resolved] == [2]


def test_an_exact_version_resolves_even_when_it_is_deprecated() -> None:
    rule_set = candidate(1, [version(1, None, deprecated=True), version(2, None)])
    resolved, unresolved = explicit([attach(1, 1)], [rule_set])
    assert unresolved == []
    assert [(e.version_no, e.source) for e in resolved] == [(1, "explicit")]


def test_the_source_is_recorded_as_given_so_adhoc_sets_resolve_the_same_way() -> None:
    resolved, _ = explicit([attach(1)], [candidate(1, [version(1, None)])], source="adhoc")
    assert [e.source for e in resolved] == ["adhoc"]


def test_an_attachment_that_cannot_resolve_is_reported_never_skipped() -> None:
    sibling = CandidateSet(
        rule_set_id=uid(2),
        name="sibling",
        scope="folder",
        folder_id=uid(999),  # not an ancestor of the folder
        folder_kind="organization",
        depth=1,
        versions=(version(1, None),),
    )
    never_published = candidate(3, [])
    held_here = candidate(4, [version(1, None)])
    resolved, unresolved = explicit(
        [attach(1), attach(2), attach(3), attach(4, 9)],
        [sibling, never_published, held_here],  # set 1 is not a candidate at all
    )
    assert resolved == []
    assert [(u.rule_set_id, u.version_spec, u.source) for u in unresolved] == [  # type: ignore[attr-defined]
        (uid(1), "latest", "explicit"),
        (uid(2), "latest", "explicit"),
        (uid(3), "latest", "explicit"),
        (uid(4), "9", "explicit"),
    ]


def test_a_platform_set_is_visible_everywhere() -> None:
    platform = candidate(1, [version(1, None)], depth=0)
    resolved, unresolved = explicit([attach(1)], [platform])
    assert unresolved == []
    assert [(e.depth, e.scope) for e in resolved] == [(0, "platform")]


def test_a_set_that_is_already_mandatory_is_neither_resolved_nor_unresolved_here() -> None:
    rule_set = candidate(1, [version(1, None)])
    resolved, unresolved = explicit(
        [attach(1), attach(1, 1)], [rule_set], mandatory=frozenset({uid(1)})
    )
    assert (resolved, unresolved) == ([], [])


def test_the_same_attachment_twice_resolves_once_and_latest_equal_to_exact_is_one_entry() -> None:
    rule_set = candidate(1, [version(1, None), version(2, None)])
    resolved, unresolved = explicit([attach(1), attach(1), attach(1, 2)], [rule_set])
    assert unresolved == []
    assert [e.version_no for e in resolved] == [2]
    _, unresolved_twice = explicit([attach(7), attach(7)], [])
    assert len(unresolved_twice) == 1


def test_two_different_versions_of_one_set_are_two_entries_in_version_order() -> None:
    rule_set = candidate(1, [version(1, None), version(2, None)])
    resolved, _ = explicit([attach(1, 2), attach(1, 1)], [rule_set])
    assert [e.version_no for e in resolved] == [1, 2]


def test_the_result_is_ordered_by_depth_name_and_id_whatever_the_attachment_order() -> None:
    a = candidate(1, [version(1, None)], name="Zeta", depth=1)
    b = candidate(2, [version(2, None)], name="alpha", depth=1)
    c = candidate(3, [version(3, None)], name="Mid", depth=0)
    one, _ = explicit([attach(1), attach(2), attach(3)], [a, b, c])
    other, _ = explicit([attach(3), attach(2), attach(1)], [a, b, c])
    assert [e.name for e in one] == ["Mid", "alpha", "Zeta"]
    assert one == other
