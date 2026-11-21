"""FolderScope.covers is the exact predicate the oracle comparison uses (research Pattern 5)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from dc_auth.authorizer import FolderScope

pytestmark = pytest.mark.unit

ROOT, ORG_O, ORG_O2 = uuid4(), uuid4(), uuid4()
TEAM_T, TEAM_U = uuid4(), uuid4()


def chain(*ids: UUID) -> tuple[UUID, ...]:
    return (ROOT, *ids)


def test_all_without_exclusions_covers_everything() -> None:
    scope = FolderScope(all=True)
    assert scope.covers(TEAM_T, chain(ORG_O, TEAM_T))
    assert scope.covers(ROOT, (ROOT,))
    assert not scope.is_empty


def test_all_with_exclusion_hides_the_subtree_including_the_excluded_folder() -> None:
    scope = FolderScope(all=True, excluded_subtrees=frozenset({ORG_O}))
    assert not scope.covers(TEAM_T, chain(ORG_O, TEAM_T))
    assert not scope.covers(ORG_O, chain(ORG_O))
    assert scope.covers(TEAM_U, chain(ORG_O2, TEAM_U))
    assert scope.covers(ROOT, (ROOT,))


def test_folder_ids_add_back_inside_an_excluded_subtree() -> None:
    scope = FolderScope(
        all=True, folder_ids=frozenset({TEAM_T}), excluded_subtrees=frozenset({ORG_O})
    )
    assert scope.covers(TEAM_T, chain(ORG_O, TEAM_T))
    assert not scope.covers(TEAM_U, chain(ORG_O, TEAM_U))


def test_without_all_only_exact_folders_count_and_exclusions_are_irrelevant() -> None:
    scope = FolderScope(
        all=False, folder_ids=frozenset({TEAM_T}), excluded_subtrees=frozenset({ORG_O})
    )
    assert scope.covers(TEAM_T, chain(ORG_O, TEAM_T))
    assert not scope.covers(TEAM_U, chain(ORG_O2, TEAM_U))
    assert not scope.is_empty


def test_empty_scope_covers_nothing() -> None:
    scope = FolderScope(all=False)
    assert scope.is_empty
    assert not scope.covers(TEAM_T, chain(ORG_O, TEAM_T))
    assert FolderScope(all=False, excluded_subtrees=frozenset({ORG_O})).is_empty
