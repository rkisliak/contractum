"""A small TTL cache in front of a ``DirectoryClient`` (D-61, D-62).

Found and not-found answers are kept for ``ttl_s`` seconds; a failure is never cached, so an outage
is retried on the next request and, once the cached answer expired, fails closed (no stale grace
period). One in-flight fetch per ``(tenant_id, kind, object_id)`` stops a burst after expiry from
turning into N identical Graph calls (a throttled Graph would fail everyone closed). ``group`` is
asked once at registration or grant time and is never cached. ``search`` keeps successful answers
for at most ``SEARCH_TTL_S`` seconds in a separate bounded dict (typing in a dialog must not become
a Graph call per keystroke); a failure is never kept.
``group_members`` (quick 261119-wp6) is kept the same way for at most ``MEMBERS_TTL_S`` seconds, so
reloading a group page is not a Graph call each time; an unknown group (``None``) is kept too.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime

from dc_identity.directory import (
    MEMBERS_LIMIT,
    ConnectionResult,
    DirectoryClient,
    DirectoryEntry,
    DirectoryGroup,
    DirectoryKind,
    DirectoryMembers,
    DirectorySearchKind,
    DirectorySearchResult,
)

_Key = tuple[str, DirectoryKind, str]
_SearchKey = tuple[str, DirectorySearchKind, str]
SEARCH_TTL_S = 30.0
MEMBERS_TTL_S = 30.0


class DirectoryCache:
    def __init__(
        self,
        inner: DirectoryClient,
        *,
        ttl_s: float,
        maxsize: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be above 0 (leave the cache out to disable it)")
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        self._inner = inner
        self._ttl_s = ttl_s
        self._maxsize = maxsize
        self._clock = clock
        self._entries: dict[_Key, tuple[float, DirectoryEntry]] = {}
        self._locks: dict[_Key, asyncio.Lock] = {}
        self._searches: dict[_SearchKey, tuple[float, DirectorySearchResult]] = {}
        self._members: dict[tuple[str, str, int], tuple[float, DirectoryMembers | None]] = {}

    def _fresh(self, key: _Key) -> DirectoryEntry | None:
        held = self._entries.get(key)
        if held is None or held[0] <= self._clock():
            return None
        return held[1]

    async def lookup(self, tenant_id: str, kind: DirectoryKind, object_id: str) -> DirectoryEntry:
        key: _Key = (tenant_id, kind, object_id)
        entry = self._fresh(key)
        if entry is not None:
            return entry
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                entry = self._fresh(key)  # a concurrent caller may have filled it meanwhile
                if entry is not None:
                    return entry
                entry = await self._inner.lookup(tenant_id, kind, object_id)
                self._store(key, entry)
                return entry
        finally:
            if not lock.locked() and self._locks.get(key) is lock:
                del self._locks[key]

    def _store(self, key: _Key, entry: DirectoryEntry) -> None:
        self._entries[key] = (self._clock() + self._ttl_s, entry)
        while len(self._entries) > self._maxsize:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest]

    async def group(self, tenant_id: str, object_id: str) -> DirectoryGroup | None:
        return await self._inner.group(tenant_id, object_id)

    async def search(
        self, tenant_id: str, kind: DirectorySearchKind, query: str
    ) -> DirectorySearchResult:
        key: _SearchKey = (tenant_id, kind, query.casefold())
        held = self._searches.get(key)
        if held is not None and held[0] > self._clock():
            return held[1]
        result = await self._inner.search(tenant_id, kind, query)
        self._searches[key] = (self._clock() + min(self._ttl_s, SEARCH_TTL_S), result)
        while len(self._searches) > self._maxsize:
            del self._searches[min(self._searches, key=lambda k: self._searches[k][0])]
        return result

    async def group_members(
        self, tenant_id: str, object_id: str, *, limit: int = MEMBERS_LIMIT
    ) -> DirectoryMembers | None:
        key = (tenant_id, object_id, limit)
        held = self._members.get(key)
        if held is not None and held[0] > self._clock():
            return held[1]
        result = await self._inner.group_members(tenant_id, object_id, limit=limit)
        self._members[key] = (self._clock() + min(self._ttl_s, MEMBERS_TTL_S), result)
        while len(self._members) > self._maxsize:
            del self._members[min(self._members, key=lambda k: self._members[k][0])]
        return result

    async def check_connection(self, tenant_id: str) -> ConnectionResult:
        return await self._inner.check_connection(tenant_id)  # a live probe, never cached

    async def application_secret_expiries(
        self, tenant_id: str, app_id: str
    ) -> list[datetime] | None:
        return await self._inner.application_secret_expiries(tenant_id, app_id)
