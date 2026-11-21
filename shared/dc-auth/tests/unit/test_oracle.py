"""The brute-force access oracle on hand-built trees (D-28, D-29, A1, A2, A5)."""

from __future__ import annotations

from uuid import UUID

import pytest
from dc_auth.oracle import (
    Grant,
    Tree,
    TreeFolder,
    decide,
    folders_with,
    permissions_at,
    roles_at,
)

pytestmark = pytest.mark.unit

ROOT, O, P, T, O2, P2, T2 = (UUID(int=i) for i in range(1, 8))  # noqa: E741
TREE = Tree(
    {
        ROOT: TreeFolder(ROOT, None, "root"),
        O: TreeFolder(O, ROOT, "organization"),
        P: TreeFolder(P, O, "project"),
        T: TreeFolder(T, P, "team"),
        O2: TreeFolder(O2, ROOT, "organization"),
        P2: TreeFolder(P2, O2, "project"),
        T2: TreeFolder(T2, P2, "team"),
    }
)
ME = ("user", "me")
GRANTEES = {ME}


def grant(folder: UUID, role: str) -> Grant:
    return Grant(folder, "user", "me", role)


def dec(
    grants: list[Grant],
    permission: str,
    folder: UUID | None,
    blocks: frozenset[UUID] = frozenset(),
) -> str:
    return decide(TREE, blocks, grants, GRANTEES, permission, folder)


def roles(grants: list[Grant], folder: UUID, blocks: frozenset[UUID] = frozenset()) -> set[str]:
    return set(roles_at(TREE, blocks, grants, GRANTEES, folder))


def test_tree_chain_is_root_first_and_includes_self() -> None:
    assert TREE.root_id == ROOT
    assert TREE.chain(T) == (ROOT, O, P, T)
    assert TREE.chain(ROOT) == (ROOT,)


def test_team_editor_at_team_hand_cases() -> None:
    grants = [grant(T, "team_editor")]
    assert dec(grants, "contract:create", T) == "allow"
    assert dec(grants, "contract:create", T2) == "not_found"
    # A5: descendant grants do not reveal ancestors.
    assert dec(grants, "folder:read", O) == "not_found"


def test_team_reader_cannot_create() -> None:
    assert dec([grant(T, "team_reader")], "contract:create", T) == "forbidden"


def test_grants_inherit_downward() -> None:
    assert roles([grant(O, "org_reader")], T) == {"org_reader"}
    assert roles([grant(O, "org_reader")], T2) == set()
    assert roles([grant(O, "org_reader")], ROOT) == set()


def test_block_at_team_cuts_inherited_grants() -> None:
    blocks = frozenset({T})
    assert roles([grant(O, "org_reader")], T, blocks) == set()
    assert roles([grant(O, "org_reader")], P, blocks) == {"org_reader"}


def test_adjacency_grant_made_at_the_blocked_folder_still_applies() -> None:
    blocks = frozenset({T})
    assert roles([grant(T, "team_reader")], T, blocks) == {"team_reader"}
    assert dec([grant(T, "team_reader")], "contract:read", T, blocks) == "allow"


def test_global_admin_is_never_cut_but_global_reader_and_consumer_are() -> None:
    blocks = frozenset({T})
    assert roles([grant(ROOT, "global_admin")], T, blocks) == {"global_admin"}
    assert dec([grant(ROOT, "global_admin")], "contract:create", T, blocks) == "allow"
    assert roles([grant(ROOT, "global_reader")], T, blocks) == set()  # A2
    assert roles([grant(ROOT, "consumer")], T, blocks) == set()
    assert roles([grant(ROOT, "policy_manager")], T, blocks) == set()


def test_block_at_org_keeps_grants_made_at_the_org_and_cuts_root_grants() -> None:
    blocks = frozenset({O})
    grants = [grant(O, "org_reader")]
    for folder in (O, P, T):
        assert roles(grants, folder, blocks) == {"org_reader"}
        assert roles([grant(ROOT, "global_reader")], folder, blocks) == set()
    assert dec(grants, "contract:read", T, blocks) == "allow"


def test_ordering_of_two_blocks_on_one_chain_does_not_matter() -> None:
    grants = [grant(P, "project_editor"), grant(ROOT, "global_reader")]
    forward = [O, T]
    backward = [T, O]
    for blocks in (forward, backward, frozenset(forward)):
        assert set(roles_at(TREE, blocks, grants, GRANTEES, T)) == set()
        assert set(roles_at(TREE, blocks, grants, GRANTEES, P)) == {"project_editor"}
        assert set(roles_at(TREE, blocks, grants, GRANTEES, O)) == set()
        assert set(roles_at(TREE, blocks, grants, GRANTEES, ROOT)) == {"global_reader"}


def test_set_block_is_evaluated_at_the_parent() -> None:
    org_admin = [grant(O, "org_admin")]
    assert dec(org_admin, "folder:set-block", T) == "allow"
    assert dec(org_admin, "folder:set-block", O) == "forbidden"
    team_admin = [grant(T, "team_admin")]
    assert dec(team_admin, "folder:set-block", T) == "forbidden"
    assert dec([grant(ROOT, "global_admin")], "folder:set-block", ROOT) == "allow"


def test_policy_manager_reads_metadata_not_content() -> None:
    pm = [grant(O, "policy_manager")]
    assert dec(pm, "contract:read", T) == "not_found"
    assert dec(pm, "contract:read-metadata", T) == "allow"
    assert dec(pm, "template:create", T) == "allow"


def test_unknown_permission_is_forbidden_even_for_global_admin() -> None:
    assert dec([grant(ROOT, "global_admin")], "contract:search", T) == "forbidden"


def test_empty_grant_set_holds_nothing_anywhere() -> None:
    for folder in TREE.folders:
        assert roles([], folder) == set()
        assert permissions_at(TREE, frozenset(), [], GRANTEES, folder) == frozenset()
    assert dec([], "folder:read", T) == "not_found"


def test_other_principals_grants_are_ignored_and_groups_count() -> None:
    other = [Grant(T, "user", "someone-else", "team_admin")]
    assert roles(other, T) == set()
    via_group = [Grant(T, "local_group", "g1", "team_editor")]
    got = roles_at(TREE, frozenset(), via_group, {ME, ("local_group", "g1")}, T)
    assert set(got) == {"team_editor"}


def test_target_kinds_special_principal_root_and_missing_folder() -> None:
    admin = [grant(ROOT, "global_admin")]
    assert dec([], "folder:list", None) == "allow"
    assert dec(admin, "audit:read", None) == "allow"
    assert dec([grant(O, "org_admin")], "audit:read", None) == "forbidden"
    assert dec(admin, "folder:read", None) == "forbidden"
    assert dec(admin, "folder:read", UUID(int=99)) == "not_found"
    with pytest.raises(ValueError, match="dedicated branch"):
        dec(admin, "password:rotate", None)


def test_folders_with_lists_every_folder_where_the_permission_is_held() -> None:
    grants = [grant(O, "org_reader")]
    assert folders_with(TREE, frozenset(), grants, GRANTEES, "contract:read") == {O, P, T}
    assert folders_with(TREE, frozenset({T}), grants, GRANTEES, "contract:read") == {O, P}
    assert folders_with(TREE, frozenset(), grants, GRANTEES, "contract:search") == frozenset()
    with pytest.raises(ValueError, match="folder-target"):
        folders_with(TREE, frozenset(), grants, GRANTEES, "folder:list")


@pytest.mark.parametrize(
    ("folders", "found"),
    [
        ({O: TreeFolder(O, ROOT, "organization")}, 0),
        ({ROOT: TreeFolder(ROOT, None, "root"), O2: TreeFolder(O2, None, "root")}, 2),
    ],
    ids=["no-root", "two-roots"],
)
def test_a_tree_without_exactly_one_root_has_no_root_id(
    folders: dict[UUID, TreeFolder], found: int
) -> None:
    with pytest.raises(ValueError, match=f"exactly one root, found {found}"):
        _ = Tree(folders).root_id


def test_a_folder_outside_the_tree_holds_no_roles_even_with_a_global_grant() -> None:
    outside = UUID(int=99)
    assert roles([grant(ROOT, "global_admin")], outside) == set()
