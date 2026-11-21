"""DirectoryCache: TTL, single flight, no failure caching, bounded size (D-61, D-62)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from dc_auth.testing import ENTRA_TENANT_ID
from dc_identity.directory import DirectoryEntry, DirectoryUnavailable
from dc_identity.directory.cache import DirectoryCache
from dc_identity.directory.fake import FakeDirectoryClient

pytestmark = pytest.mark.unit

OTHER_TENANT = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _setup(
    *, ttl_s: float = 60.0, maxsize: int = 100
) -> tuple[DirectoryCache, FakeDirectoryClient, Clock]:
    fake = FakeDirectoryClient()
    clock = Clock()
    return DirectoryCache(fake, ttl_s=ttl_s, maxsize=maxsize, clock=clock), fake, clock


def _lookups(fake: FakeDirectoryClient) -> int:
    return sum(1 for call in fake.calls if call[0] == "lookup")


async def test_a_second_lookup_inside_the_ttl_is_served_from_the_cache() -> None:
    cache, fake, clock = _setup()
    oid = str(uuid.uuid4())
    fake.add_principal("user", oid)
    first = await cache.lookup(ENTRA_TENANT_ID, "user", oid)
    clock.now += 59
    second = await cache.lookup(ENTRA_TENANT_ID, "user", oid)
    assert first == second
    assert _lookups(fake) == 1


async def test_a_lookup_after_the_ttl_asks_the_directory_again() -> None:
    cache, fake, clock = _setup()
    oid = str(uuid.uuid4())
    fake.add_principal("user", oid)
    await cache.lookup(ENTRA_TENANT_ID, "user", oid)
    clock.now += 61
    await cache.lookup(ENTRA_TENANT_ID, "user", oid)
    assert _lookups(fake) == 2


async def test_the_same_object_id_in_two_tenants_is_two_keys() -> None:
    cache, fake, _clock = _setup()
    oid = str(uuid.uuid4())
    fake.add_principal("user", oid)
    fake.add_principal("user", oid, tenant_id=OTHER_TENANT, groups=(str(uuid.uuid4()),))
    a = await cache.lookup(ENTRA_TENANT_ID, "user", oid)
    b = await cache.lookup(OTHER_TENANT, "user", oid)
    assert a != b
    assert _lookups(fake) == 2


async def test_the_same_object_id_as_user_and_service_principal_is_two_keys() -> None:
    cache, fake, _clock = _setup()
    oid = str(uuid.uuid4())
    fake.add_principal("user", oid)
    user = await cache.lookup(ENTRA_TENANT_ID, "user", oid)
    sp = await cache.lookup(ENTRA_TENANT_ID, "service_principal", oid)
    assert user.found is True
    assert sp.found is False
    assert _lookups(fake) == 2


class _SlowDirectory(FakeDirectoryClient):
    async def lookup(self, tenant_id, kind, object_id):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.05)
        return await super().lookup(tenant_id, kind, object_id)


async def test_twenty_concurrent_lookups_of_one_key_make_one_call() -> None:
    fake = _SlowDirectory()
    cache = DirectoryCache(fake, ttl_s=60, maxsize=10)
    oid = str(uuid.uuid4())
    fake.add_principal("user", oid)
    results = await asyncio.gather(*(cache.lookup(ENTRA_TENANT_ID, "user", oid) for _ in range(20)))
    assert all(result == results[0] for result in results)
    assert _lookups(fake) == 1


async def test_a_failure_is_not_cached() -> None:
    cache, fake, _clock = _setup()
    oid = str(uuid.uuid4())
    fake.add_principal("user", oid)
    fake.fail = True
    with pytest.raises(DirectoryUnavailable):
        await cache.lookup(ENTRA_TENANT_ID, "user", oid)
    fake.fail = False
    entry = await cache.lookup(ENTRA_TENANT_ID, "user", oid)
    assert entry.found is True
    assert _lookups(fake) == 2


async def test_a_failure_after_the_window_is_not_masked_by_the_stale_entry() -> None:
    cache, fake, clock = _setup()
    oid = str(uuid.uuid4())
    fake.add_principal("user", oid)
    await cache.lookup(ENTRA_TENANT_ID, "user", oid)
    fake.fail = True
    clock.now += 30
    assert (await cache.lookup(ENTRA_TENANT_ID, "user", oid)).found is True  # inside the window
    clock.now += 31
    with pytest.raises(DirectoryUnavailable):
        await cache.lookup(ENTRA_TENANT_ID, "user", oid)


async def test_not_found_is_cached() -> None:
    cache, fake, _clock = _setup()
    oid = str(uuid.uuid4())
    first = await cache.lookup(ENTRA_TENANT_ID, "user", oid)
    second = await cache.lookup(ENTRA_TENANT_ID, "user", oid)
    assert first == second == DirectoryEntry(found=False)
    assert _lookups(fake) == 1


async def test_more_keys_than_maxsize_evicts_the_oldest() -> None:
    cache, fake, clock = _setup(maxsize=2)
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    for oid in (a, b, c):
        fake.add_principal("user", oid)
    await cache.lookup(ENTRA_TENANT_ID, "user", a)
    clock.now += 1
    await cache.lookup(ENTRA_TENANT_ID, "user", b)
    clock.now += 1
    await cache.lookup(ENTRA_TENANT_ID, "user", c)  # evicts a
    assert _lookups(fake) == 3
    await cache.lookup(ENTRA_TENANT_ID, "user", b)
    await cache.lookup(ENTRA_TENANT_ID, "user", c)
    assert _lookups(fake) == 3
    await cache.lookup(ENTRA_TENANT_ID, "user", a)
    assert _lookups(fake) == 4


async def test_group_lookups_are_not_cached() -> None:
    cache, fake, _clock = _setup()
    group = str(uuid.uuid4())
    fake.add_group(group, "Engineers")
    await cache.group(ENTRA_TENANT_ID, group)
    await cache.group(ENTRA_TENANT_ID, group)
    assert [call[0] for call in fake.calls] == ["group", "group"]


def test_a_ttl_of_zero_is_refused() -> None:
    with pytest.raises(ValueError):
        DirectoryCache(FakeDirectoryClient(), ttl_s=0, maxsize=10)


# --- search (quick 261119-ofa) --------------------------------------------------------------------


def _searches(fake: FakeDirectoryClient) -> int:
    return sum(1 for call in fake.calls if call[0] == "search")


async def test_a_search_is_cached_per_tenant_kind_and_casefolded_query_for_a_short_ttl() -> None:
    cache, fake, clock = _setup(ttl_s=600.0)
    fake.add_principal("user", str(uuid.uuid4()), display_name="Alice")
    first = await cache.search(ENTRA_TENANT_ID, "user", "Alice")
    clock.now += 29
    assert await cache.search(ENTRA_TENANT_ID, "user", "aLICE") == first
    assert _searches(fake) == 1
    await cache.search(ENTRA_TENANT_ID, "group", "alice")  # another kind is another key
    assert _searches(fake) == 2
    clock.now += 2  # past the 30 s search TTL although the lookup TTL is 600 s
    await cache.search(ENTRA_TENANT_ID, "user", "alice")
    assert _searches(fake) == 3


async def test_a_failed_search_is_never_cached() -> None:
    cache, fake, _clock = _setup()
    fake.fail = True
    with pytest.raises(DirectoryUnavailable):
        await cache.search(ENTRA_TENANT_ID, "user", "alice")
    fake.fail = False
    await cache.search(ENTRA_TENANT_ID, "user", "alice")
    assert _searches(fake) == 2


async def test_the_search_cache_is_bounded() -> None:
    cache, fake, _clock = _setup(maxsize=2)
    for query in ("aa", "bb", "cc"):
        await cache.search(ENTRA_TENANT_ID, "user", query)
    await cache.search(ENTRA_TENANT_ID, "user", "aa")  # evicted, asked again
    assert _searches(fake) == 4


# --- Group members (quick 261119-wp6) ---------------------------------------------------------


def _members_calls(fake: FakeDirectoryClient) -> int:
    return sum(1 for call in fake.calls if call[0] == "group_members")


async def test_group_members_are_cached_for_the_members_ttl_and_then_asked_again() -> None:
    cache, fake, clock = _setup(ttl_s=300.0)
    gid = str(uuid.uuid4())
    fake.add_group(gid, "Eng")
    first = await cache.group_members(ENTRA_TENANT_ID, gid)
    clock.now += 29
    assert await cache.group_members(ENTRA_TENANT_ID, gid) == first
    assert _members_calls(fake) == 1
    clock.now += 2  # past the 30 s ceiling although the entry ttl is 300 s
    await cache.group_members(ENTRA_TENANT_ID, gid)
    assert _members_calls(fake) == 2


async def test_an_unknown_group_is_kept_and_a_failure_is_never_cached() -> None:
    cache, fake, _clock = _setup()
    gid = str(uuid.uuid4())
    assert await cache.group_members(ENTRA_TENANT_ID, gid) is None
    assert await cache.group_members(ENTRA_TENANT_ID, gid) is None
    assert _members_calls(fake) == 1
    other = str(uuid.uuid4())
    fake.fail = True
    with pytest.raises(DirectoryUnavailable):
        await cache.group_members(ENTRA_TENANT_ID, other)
    fake.fail = False
    fake.add_group(other, "Back")
    assert await cache.group_members(ENTRA_TENANT_ID, other) is not None
    assert _members_calls(fake) == 3


async def test_the_members_cache_is_bounded_and_keyed_by_tenant_and_limit() -> None:
    cache, fake, _clock = _setup(maxsize=2)
    gid = str(uuid.uuid4())
    fake.add_group(gid, "A", tenant_id=ENTRA_TENANT_ID)
    fake.add_group(gid, "A", tenant_id=OTHER_TENANT)
    await cache.group_members(ENTRA_TENANT_ID, gid)
    await cache.group_members(OTHER_TENANT, gid)
    await cache.group_members(ENTRA_TENANT_ID, gid, limit=5)
    assert _members_calls(fake) == 3
    assert len(cache._members) == 2


async def test_an_unconfigured_directory_refuses_member_listing_like_every_other_call() -> None:
    from dc_identity.directory import UnconfiguredDirectory

    with pytest.raises(DirectoryUnavailable):
        await UnconfiguredDirectory().group_members(ENTRA_TENANT_ID, str(uuid.uuid4()))


def test_a_cache_without_room_for_one_entry_is_refused() -> None:
    with pytest.raises(ValueError, match="maxsize must be at least 1"):
        DirectoryCache(FakeDirectoryClient(), ttl_s=60.0, maxsize=0)


async def test_the_connection_probe_is_live_on_every_call() -> None:
    cache, fake, _clock = _setup()
    fake.connection_result = "token-refused"
    assert await cache.check_connection(ENTRA_TENANT_ID) == "token-refused"
    fake.connection_result = "ok"
    assert await cache.check_connection(ENTRA_TENANT_ID) == "ok"
    assert [call[0] for call in fake.calls] == ["check_connection", "check_connection"]


async def test_secret_expiries_are_passed_through_uncached() -> None:
    cache, fake, _clock = _setup()
    ends = [datetime(2027, 1, 1, tzinfo=UTC)]
    fake.secret_expiries = ends
    assert await cache.application_secret_expiries(ENTRA_TENANT_ID, "app") == ends
    fake.secret_expiries = None
    assert await cache.application_secret_expiries(ENTRA_TENANT_ID, "app") is None
    assert fake.calls == [
        ("application_secret_expiries", ENTRA_TENANT_ID, "app"),
        ("application_secret_expiries", ENTRA_TENANT_ID, "app"),
    ]
