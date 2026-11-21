"""Brute-force reference implementation of the Phase 2 access rules (D-28, D-29).

Pure Python, no I/O, deliberately written as the *literal* reading of the rules rather than the
SQL's depth arithmetic, so later plans can prove the SQL evaluator against an independent answer
(unit tests here, Hypothesis in identity's integration tests).

Rule: a grant made at folder G applies at folder D (G an ancestor-or-self of D) unless a blocked
folder sits strictly below G on the path down to D. A grant made at a blocked folder itself still
applies there and below. ``global_admin`` is never cut (D-28); every other role is, including
Global reader and ROOT-level Policy Manager and Consumer (A2).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from dc_auth.roles import PERMISSIONS, Role, permissions_of

Grantee = tuple[str, str]
"""``(principal_type, principal_id)``: the principal and every group it belongs to."""

DecisionOutcome = Literal["allow", "not_found", "forbidden"]


@dataclass(frozen=True)
class TreeFolder:
    id: UUID
    parent_id: UUID | None
    kind: str


@dataclass(frozen=True)
class Tree:
    folders: Mapping[UUID, TreeFolder]

    @property
    def root_id(self) -> UUID:
        roots = [f.id for f in self.folders.values() if f.parent_id is None]
        if len(roots) != 1:
            raise ValueError(f"a tree needs exactly one root, found {len(roots)}")
        return roots[0]

    def chain(self, folder_id: UUID) -> tuple[UUID, ...]:
        """Ancestors-or-self of ``folder_id``, root first."""
        walked: list[UUID] = []
        current: UUID | None = folder_id
        while current is not None:
            walked.append(current)
            current = self.folders[current].parent_id
        return tuple(reversed(walked))


@dataclass(frozen=True)
class Grant:
    folder_id: UUID
    principal_type: str
    principal_id: str
    role: str


def roles_at(
    tree: Tree,
    blocks: Collection[UUID],
    grants: Iterable[Grant],
    grantees: Collection[Grantee],
    folder_id: UUID,
) -> frozenset[str]:
    """Role values the grantees hold at ``folder_id`` after inheritance and block flags."""
    if folder_id not in tree.folders:
        return frozenset()
    blocked = frozenset(blocks)
    chain = tree.chain(folder_id)
    position = {fid: index for index, fid in enumerate(chain)}
    held: set[str] = set()
    for grant in grants:
        if (grant.principal_type, grant.principal_id) not in grantees:
            continue
        index = position.get(grant.folder_id)
        if index is None:
            continue  # granted outside the chain to the folder
        cut = any(fid in blocked for fid in chain[index + 1 :])
        if cut and grant.role != Role.GLOBAL_ADMIN:
            continue
        held.add(grant.role)
    return frozenset(held)


def permissions_at(
    tree: Tree,
    blocks: Collection[UUID],
    grants: Iterable[Grant],
    grantees: Collection[Grantee],
    folder_id: UUID,
) -> frozenset[str]:
    return permissions_of(roles_at(tree, blocks, grants, grantees, folder_id))


def decide(
    tree: Tree,
    blocks: Collection[UUID],
    grants: Iterable[Grant],
    grantees: Collection[Grantee],
    permission: str,
    folder_id: UUID | None,
) -> DecisionOutcome:
    """The policy decision for one permission at one folder, mirroring the PDP algorithm."""
    spec = PERMISSIONS.get(permission)
    if spec is None:
        return "forbidden"
    if spec.target == "special":
        raise ValueError(f"{permission} is answered by a dedicated branch, not modelled here")
    if spec.target == "principal":
        return "allow"
    grant_list = list(grants)
    if spec.target == "root":
        held = permissions_at(tree, blocks, grant_list, grantees, tree.root_id)
        return "allow" if permission in held else "forbidden"
    if folder_id is None:
        return "forbidden"
    folder = tree.folders.get(folder_id)
    if folder is None:
        return "not_found"
    evaluated = folder_id
    if spec.evaluated_at == "parent" and folder.parent_id is not None:
        evaluated = folder.parent_id
    if permission in permissions_at(tree, blocks, grant_list, grantees, evaluated):
        return "allow"
    if spec.visible_via is not None and spec.visible_via in permissions_at(
        tree, blocks, grant_list, grantees, folder_id
    ):
        return "forbidden"
    return "not_found"


def folders_with(
    tree: Tree,
    blocks: Collection[UUID],
    grants: Iterable[Grant],
    grantees: Collection[Grantee],
    permission: str,
) -> frozenset[UUID]:
    """Every folder where the grantees hold ``permission`` (folder-target permissions only)."""
    spec = PERMISSIONS.get(permission)
    if spec is None:
        return frozenset()
    if spec.target != "folder":
        raise ValueError(f"{permission} is not a folder-target permission")
    grant_list = list(grants)
    return frozenset(
        fid
        for fid in tree.folders
        if permission in permissions_at(tree, blocks, grant_list, grantees, fid)
    )
