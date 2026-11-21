"""Authorizer interface (D-05, D-08): the policy decision point behind every ``require()``."""

from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from fastapi import Request
from pydantic import BaseModel

from dc_auth.principal import Principal


class Outcome(StrEnum):
    ALLOW = "allow"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"


class FolderFacts(BaseModel, frozen=True):
    id: UUID
    kind: Literal["root", "organization", "project", "team"]
    slug: str
    path: str
    ancestor_ids: tuple[UUID, ...]  # root-first, includes self
    organization_id: UUID | None
    archived: bool  # self or any ancestor archived


class Decision(BaseModel, frozen=True):
    outcome: Outcome
    folder: FolderFacts | None = None
    permissions: frozenset[str] = frozenset()  # held at the target folder (empty if not allowed)


class FolderScope(BaseModel, frozen=True):
    """Compact scope: ``all`` minus ``excluded_subtrees``, plus the exact ``folder_ids``.

    ``all`` with an excluded folder hides every folder under it (the folder itself included);
    ``folder_ids`` add exact folders back, even inside an excluded subtree.
    """

    all: bool
    folder_ids: frozenset[UUID] = frozenset()
    excluded_subtrees: frozenset[UUID] = frozenset()

    @property
    def is_empty(self) -> bool:
        return not self.all and not self.folder_ids

    def covers(self, folder_id: UUID, ancestor_ids: Sequence[UUID]) -> bool:
        """``(all and no excluded id in ancestor_ids) or folder_id in folder_ids``.

        ``ancestor_ids`` is root-first and includes ``folder_id`` itself.
        """
        if self.all and not any(a in self.excluded_subtrees for a in ancestor_ids):
            return True
        return folder_id in self.folder_ids


class AuthorizerUnavailable(Exception):  # noqa: N818 - name fixed by the plan interface
    """The authorization service could not answer; callers fail closed (503)."""


class Authorizer(Protocol):
    async def check(
        self,
        principal: Principal,
        permission: str,
        folder_id: UUID | None = None,
        *,
        fresh: bool = False,
    ) -> Decision: ...

    async def accessible_folders(self, principal: Principal, permission: str) -> FolderScope: ...


@runtime_checkable
class GranteeAuthorizer(Authorizer, Protocol):
    """An ``Authorizer`` that can also answer card-membership questions (SUB-02).

    A sub-protocol so the plain ``Authorizer`` (and the structural fakes that implement it) does
    not grow. Both methods fail closed with ``AuthorizerUnavailable``; neither returns a partial
    answer.
    """

    async def grantees(self, principal: Principal) -> frozenset[tuple[str, str]]:
        """Every ``(type, id)`` the caller answers to: itself, its local and Entra groups."""
        ...

    async def check_members(
        self, principal: Principal, folder_id: UUID, members: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], bool]:
        """Whether each ``(type, id)`` may be linked at ``folder_id`` (the grant rules)."""
        ...


AuthorizerFactory = Callable[[Request], Authorizer]
