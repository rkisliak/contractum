"""The async JWKS cache on its own: key-set parsing, refresh races, size cap and stale keys."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx
from dc_auth.issuers import IssuerConfig
from dc_auth.jwks import JwksCache, JwksUnavailableError, KeyNotFoundError
from dc_auth.testing import FakeIssuer
from dc_core.http import make_client

pytestmark = pytest.mark.unit


def _static(fake: FakeIssuer, document: dict[str, Any]) -> IssuerConfig:
    return fake.issuer_config(static=False).model_copy(update={"static_jwks": document})


def _good_key(fake: FakeIssuer, **overrides: Any) -> dict[str, Any]:
    key = dict(fake.jwks()["keys"][0])
    key.update(overrides)
    return key


@pytest.mark.parametrize(
    "document",
    [{}, {"keys": "not-a-list"}, {"keys": {"kid": "k"}}],
    ids=["no-keys", "keys-a-string", "keys-an-object"],
)
async def test_a_key_set_without_a_key_list_holds_no_key(
    fake_issuer: FakeIssuer, document: dict[str, Any]
) -> None:
    cfg = _static(fake_issuer, document)
    async with make_client() as http:
        cache = JwksCache(http)
        assert not cache.knows(cfg, fake_issuer.kid)
        with pytest.raises(KeyNotFoundError):
            await cache.get_key(cfg, fake_issuer.kid)


async def test_unusable_entries_are_skipped_and_the_usable_ones_kept(
    fake_issuer: FakeIssuer,
) -> None:
    no_use = _good_key(fake_issuer, kid="no-use")
    no_use.pop("use")  # "use" is optional: a key without it is a signing key
    document = {
        "keys": [
            "not-an-object",
            {"kty": "RSA"},  # no kid
            _good_key(fake_issuer, kid=7),  # kid not a string
            _good_key(fake_issuer, kid="encryption-key", use="enc"),
            {"kid": "broken", "kty": "RSA", "n": "AQAB"},  # PyJWK refuses it (no exponent)
            {"kid": "unknown-type", "kty": "nope"},
            no_use,
            _good_key(fake_issuer),
        ]
    }
    cfg = _static(fake_issuer, document)
    async with make_client() as http:
        cache = JwksCache(http)
        for kid in ("encryption-key", "broken", "unknown-type"):
            assert not cache.knows(cfg, kid)
        assert cache.knows(cfg, "no-use")
        key = await cache.get_key(cfg, fake_issuer.kid)
    assert key.key_id == fake_issuer.kid


async def test_requests_waiting_behind_a_refresh_use_its_result_without_a_second_fetch(
    fake_issuer: FakeIssuer,
) -> None:
    cfg = fake_issuer.issuer_config(static=False)
    release = asyncio.Event()

    async def slow_jwks(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json=fake_issuer.jwks())

    async with make_client() as http:
        cache = JwksCache(http)
        with respx.mock(assert_all_called=False) as router:
            route = router.get(fake_issuer.jwks_uri).mock(side_effect=slow_jwks)
            first = asyncio.create_task(cache.get_key(cfg, fake_issuer.kid))
            second = asyncio.create_task(cache.get_key(cfg, fake_issuer.kid))
            await asyncio.sleep(0.05)  # both missed the cache; one holds the lock
            release.set()
            keys = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
            assert route.call_count == 1
    assert [k.key_id for k in keys] == [fake_issuer.kid, fake_issuer.kid]


async def test_an_oversized_key_set_is_an_outage_not_a_key(fake_issuer: FakeIssuer) -> None:
    cfg = fake_issuer.issuer_config(static=False)
    async with make_client() as http:
        cache = JwksCache(http)
        with respx.mock(assert_all_called=False) as router:
            router.get(fake_issuer.jwks_uri).respond(200, content=b" " * 1_048_577)
            with pytest.raises(JwksUnavailableError):
                await cache.get_key(cfg, fake_issuer.kid)


async def test_known_keys_survive_a_failed_refresh_for_up_to_an_hour_then_are_dropped(
    fake_issuer: FakeIssuer,
) -> None:
    now = [0.0]
    cfg = fake_issuer.issuer_config(static=False)
    async with make_client() as http:
        cache = JwksCache(http, ttl_s=300, clock=lambda: now[0])
        with respx.mock(assert_all_called=False) as router:
            route = router.get(fake_issuer.jwks_uri).respond(json=fake_issuer.jwks())
            await cache.get_key(cfg, fake_issuer.kid)  # fetched at t=0

            route.mock(side_effect=httpx.ConnectError("down"))
            now[0] = 400.0  # past the TTL, inside the one-hour stale window
            stale = await cache.get_key(cfg, fake_issuer.kid)
            assert stale.key_id == fake_issuer.kid
            assert route.call_count == 2
            assert cache.knows(cfg, fake_issuer.kid)

            now[0] = 3700.0  # the last good fetch is now more than an hour old
            with pytest.raises(JwksUnavailableError):
                await cache.get_key(cfg, fake_issuer.kid)
            assert route.call_count == 3
            assert not cache.knows(cfg, fake_issuer.kid)
