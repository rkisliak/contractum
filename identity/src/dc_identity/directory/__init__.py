"""The directory port: how identity asks an external directory about an Entra principal.

D-62, D-63 (groundwork), D-77. The gate depends only on this protocol; Plan 04 adds the Microsoft
Graph client and a short cache beside the in-memory fake. Every call is tenant-aware so a second
Entra tenant needs no code change (AUTH-03). The port answers three questions: what is this
principal (``lookup``), what is this group (``group``) and, for the admin dialogs that register
Entra principals by name, which objects match a typed query (``search``, quick 261119-ofa) and,
for the group detail page, who the members of a group are right now (``group_members``, quick
261119-wp6; Contractum never stores Entra membership).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

DirectoryKind = Literal["user", "service_principal"]
DirectorySearchKind = Literal["user", "group", "service_principal"]
DirectoryMemberKind = Literal["user", "group", "service_principal", "other"]
# Quick 261120-0yc (D-12): the answers of ``check_connection``.
ConnectionResult = Literal[
    "ok",
    "not-configured",
    "token-refused",
    "graph-forbidden",
    "graph-throttled",
    "graph-unreachable",
    "graph-error",
]

SEARCH_MIN = 2
SEARCH_MAX = 100
SEARCH_LIMIT = 20
MEMBERS_LIMIT = 200

_WHITESPACE = re.compile(r"\s+")


def normalize_search_query(raw: str) -> str:
    """The query as it may reach a directory: no quotes, backslashes or control characters.

    Double quotes and backslashes would break out of an OData ``$search`` phrase, so they are
    removed rather than escaped. Whitespace is collapsed, the result trimmed and cut to
    ``SEARCH_MAX``; fewer than ``SEARCH_MIN`` characters left is a ``ValueError`` (never echoing
    the input).
    """
    kept = "".join(
        " " if ch.isspace() else ch
        for ch in raw
        if ch not in '"\\' and (ch.isspace() or unicodedata.category(ch)[0] != "C")
    )
    cleaned = _WHITESPACE.sub(" ", kept).strip()[:SEARCH_MAX].strip()
    if len(cleaned) < SEARCH_MIN:
        raise ValueError("the search query is too short")
    return cleaned


@dataclass(frozen=True)
class DirectoryEntry:
    """The directory's answer for one principal: existence, state and transitive group GUIDs."""

    found: bool
    account_enabled: bool = False
    display_name: str | None = None
    group_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DirectoryGroup:
    object_id: str
    display_name: str


@dataclass(frozen=True)
class DirectoryMatch:
    """One search hit: the object, its display name and one disambiguating line."""

    object_id: str
    display_name: str
    secondary: str | None
    kind: DirectorySearchKind


@dataclass(frozen=True)
class DirectorySearchResult:
    """At most ``SEARCH_LIMIT`` matches; ``truncated`` says the directory had more."""

    items: tuple[DirectoryMatch, ...]
    truncated: bool


@dataclass(frozen=True)
class DirectoryMember:
    """One transitive member of an Entra group: a user, a nested group, a service principal."""

    object_id: str
    display_name: str
    secondary: str | None
    kind: DirectoryMemberKind
    account_enabled: bool | None


@dataclass(frozen=True)
class DirectoryMembers:
    """At most ``limit`` members; ``truncated`` says there were more, ``total`` is Graph's count."""

    items: tuple[DirectoryMember, ...]
    truncated: bool
    total: int | None


class DirectoryUnavailable(Exception):
    """The directory could not answer (outage, throttling, misconfiguration): never admit."""


class DirectoryClient(Protocol):
    async def lookup(
        self, tenant_id: str, kind: DirectoryKind, object_id: str
    ) -> DirectoryEntry: ...

    async def group(self, tenant_id: str, object_id: str) -> DirectoryGroup | None: ...

    async def search(
        self, tenant_id: str, kind: DirectorySearchKind, query: str
    ) -> DirectorySearchResult: ...

    async def group_members(
        self, tenant_id: str, object_id: str, *, limit: int = MEMBERS_LIMIT
    ) -> DirectoryMembers | None:
        """The group's transitive members, or ``None`` when the directory has no such group."""
        ...

    async def check_connection(self, tenant_id: str) -> ConnectionResult:
        """Quick 261120-0yc (D-12): a Graph token and one Graph call; answers one fixed code."""
        ...

    async def application_secret_expiries(
        self, tenant_id: str, app_id: str
    ) -> list[datetime] | None:
        """Quick 261120-0yc (D-11): only the ``endDateTime`` of the Graph app's client secrets.

        ``None`` when the directory cannot say (refusal, not found, unreachable). No other field
        of a credential (``hint``, ``keyId``, ``displayName``) ever leaves the implementation.
        """
        ...


class UnconfiguredDirectory:
    """Placeholder that fails closed: every call raises ``DirectoryUnavailable``."""

    async def lookup(self, tenant_id: str, kind: DirectoryKind, object_id: str) -> DirectoryEntry:
        raise DirectoryUnavailable("no directory is configured")

    async def group(self, tenant_id: str, object_id: str) -> DirectoryGroup | None:
        raise DirectoryUnavailable("no directory is configured")

    async def search(
        self, tenant_id: str, kind: DirectorySearchKind, query: str
    ) -> DirectorySearchResult:
        raise DirectoryUnavailable("no directory is configured")

    async def group_members(
        self, tenant_id: str, object_id: str, *, limit: int = MEMBERS_LIMIT
    ) -> DirectoryMembers | None:
        raise DirectoryUnavailable("no directory is configured")

    async def check_connection(self, tenant_id: str) -> ConnectionResult:
        return "not-configured"

    async def application_secret_expiries(
        self, tenant_id: str, app_id: str
    ) -> list[datetime] | None:
        return None
