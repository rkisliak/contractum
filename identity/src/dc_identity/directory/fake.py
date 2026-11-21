"""In-memory ``DirectoryClient`` for tests: principals, groups, enable flags and outages."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from dc_auth.testing import ENTRA_TENANT_ID

from dc_identity.directory import (
    MEMBERS_LIMIT,
    SEARCH_LIMIT,
    ConnectionResult,
    DirectoryEntry,
    DirectoryGroup,
    DirectoryKind,
    DirectoryMatch,
    DirectoryMember,
    DirectoryMembers,
    DirectorySearchKind,
    DirectorySearchResult,
    DirectoryUnavailable,
    normalize_search_query,
)


@dataclass
class _Principal:
    kind: DirectoryKind
    enabled: bool
    display_name: str | None
    groups: frozenset[str]
    mail: str | None = None
    user_principal_name: str | None = None
    app_id: str | None = None


@dataclass
class _Group:
    display_name: str
    mail: str | None = None
    description: str | None = None
    members: list[DirectoryMember] = field(default_factory=list)


@dataclass
class FakeDirectoryClient:
    """Keyed by ``(tenant_id, object_id)``; every id is stored lowercase.

    ``fail`` makes every call raise ``DirectoryUnavailable``; ``calls`` records
    ``(method, tenant_id, object_id)`` for each call, failed or not (``search`` records the query in
    the object id slot).
    """

    fail: bool = False
    # Quick 261120-0yc: what ``check_connection`` answers and what the Graph app's secrets expire.
    connection_result: ConnectionResult = "ok"
    secret_expiries: list[datetime] | None = None
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    _principals: dict[tuple[str, str], _Principal] = field(default_factory=dict)
    _groups: dict[tuple[str, str], _Group] = field(default_factory=dict)

    def add_principal(
        self,
        kind: DirectoryKind,
        object_id: str,
        *,
        tenant_id: str = ENTRA_TENANT_ID,
        groups: Iterable[str] = (),
        enabled: bool = True,
        display_name: str | None = None,
        mail: str | None = None,
        user_principal_name: str | None = None,
        app_id: str | None = None,
    ) -> None:
        self._principals[(tenant_id.lower(), object_id.lower())] = _Principal(
            kind,
            enabled,
            display_name,
            frozenset(g.lower() for g in groups),
            mail,
            user_principal_name,
            app_id.lower() if app_id else None,
        )

    def add_group(
        self,
        object_id: str,
        display_name: str,
        *,
        tenant_id: str = ENTRA_TENANT_ID,
        mail: str | None = None,
        description: str | None = None,
    ) -> None:
        self._groups[(tenant_id.lower(), object_id.lower())] = _Group(
            display_name, mail, description
        )

    def set_group_members(
        self,
        object_id: str,
        members: Iterable[DirectoryMember],
        *,
        tenant_id: str = ENTRA_TENANT_ID,
    ) -> None:
        self._groups[(tenant_id.lower(), object_id.lower())].members = list(members)

    def set_groups(
        self, object_id: str, groups: Iterable[str], *, tenant_id: str = ENTRA_TENANT_ID
    ) -> None:
        self._principals[(tenant_id.lower(), object_id.lower())].groups = frozenset(
            g.lower() for g in groups
        )

    def set_enabled(
        self, object_id: str, enabled: bool, *, tenant_id: str = ENTRA_TENANT_ID
    ) -> None:
        self._principals[(tenant_id.lower(), object_id.lower())].enabled = enabled

    def remove(self, object_id: str, *, tenant_id: str = ENTRA_TENANT_ID) -> None:
        self._principals.pop((tenant_id.lower(), object_id.lower()), None)

    async def lookup(self, tenant_id: str, kind: DirectoryKind, object_id: str) -> DirectoryEntry:
        self.calls.append(("lookup", tenant_id, object_id))
        if self.fail:
            raise DirectoryUnavailable("fake directory failure")
        found = self._principals.get((tenant_id.lower(), object_id.lower()))
        if found is None or found.kind != kind:
            return DirectoryEntry(found=False)
        return DirectoryEntry(
            found=True,
            account_enabled=found.enabled,
            display_name=found.display_name,
            group_ids=found.groups,
        )

    async def group(self, tenant_id: str, object_id: str) -> DirectoryGroup | None:
        self.calls.append(("group", tenant_id, object_id))
        if self.fail:
            raise DirectoryUnavailable("fake directory failure")
        found = self._groups.get((tenant_id.lower(), object_id.lower()))
        return None if found is None else DirectoryGroup(object_id.lower(), found.display_name)

    async def search(
        self, tenant_id: str, kind: DirectorySearchKind, query: str
    ) -> DirectorySearchResult:
        self.calls.append(("search", tenant_id, query))
        if self.fail:
            raise DirectoryUnavailable("fake directory failure")
        needle = normalize_search_query(query).casefold()
        tenant = tenant_id.lower()
        rows: list[tuple[str, DirectoryMatch]] = []
        fields: tuple[str | None, ...]
        if kind == "group":
            for (t, oid), group in self._groups.items():
                if t != tenant:
                    continue
                fields = (group.display_name, group.mail)
                if oid == needle or any(f and needle in f.casefold() for f in fields):
                    secondary = group.mail or group.description
                    rows.append(
                        (
                            group.display_name,
                            DirectoryMatch(oid, group.display_name, secondary, kind),
                        )
                    )
        else:
            for (t, oid), person in self._principals.items():
                if t != tenant or person.kind != kind:
                    continue
                if kind == "user":
                    fields = (person.display_name, person.mail, person.user_principal_name)
                    secondary = person.user_principal_name or person.mail
                else:
                    fields = (person.display_name,)
                    secondary = person.app_id
                if (
                    oid == needle
                    or person.app_id == needle
                    or any(f and needle in f.casefold() for f in fields)
                ):
                    name = person.display_name or oid
                    rows.append((name, DirectoryMatch(oid, name, secondary, kind)))
        rows.sort(key=lambda r: (r[0].casefold(), r[1].object_id))
        items = tuple(m for _, m in rows[:SEARCH_LIMIT])
        return DirectorySearchResult(items=items, truncated=len(rows) > SEARCH_LIMIT)

    async def group_members(
        self, tenant_id: str, object_id: str, *, limit: int = MEMBERS_LIMIT
    ) -> DirectoryMembers | None:
        self.calls.append(("group_members", tenant_id, object_id))
        if self.fail:
            raise DirectoryUnavailable("fake directory failure")
        found = self._groups.get((tenant_id.lower(), object_id.lower()))
        if found is None:
            return None
        return DirectoryMembers(
            items=tuple(found.members[:limit]),
            truncated=len(found.members) > limit,
            total=len(found.members),
        )

    async def check_connection(self, tenant_id: str) -> ConnectionResult:
        self.calls.append(("check_connection", tenant_id, ""))
        return self.connection_result

    async def application_secret_expiries(
        self, tenant_id: str, app_id: str
    ) -> list[datetime] | None:
        self.calls.append(("application_secret_expiries", tenant_id, app_id))
        return self.secret_expiries
