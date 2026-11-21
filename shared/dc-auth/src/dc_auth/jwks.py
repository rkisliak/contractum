"""Async JWKS cache (T-01-07).

Not PyJWT's built-in synchronous JWKS client (blocking urllib I/O on a cache miss). This cache
uses ``dc_core.http`` clients, keeps keys per issuer for ``ttl_s``, refreshes on an unknown ``kid``
at most once per ``min_refresh_interval_s`` per issuer, and serves ``static_jwks`` without any
HTTP call.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import structlog

from dc_auth.issuers import IssuerConfig

_log = structlog.get_logger("dc_auth.jwks")
_MAX_JWKS_BYTES = 1_048_576
_MAX_STALE_S = 3600.0


class KeyNotFoundError(Exception):
    """No usable signing key for this kid."""


class JwksUnavailableError(Exception):
    """The issuer's key set could not be fetched and the kid is not cached (an IdP outage).

    Distinct from :class:`KeyNotFoundError`: the token may be fine, so the caller answers 503,
    not a credential-rejecting 401.
    """


@dataclass
class _IssuerKeys:
    keys: dict[str, jwt.PyJWK] = field(default_factory=dict)
    fetched_at: float | None = None
    last_attempt: float | None = None
    last_refresh_failed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _parse_keys(document: dict[str, Any]) -> dict[str, jwt.PyJWK]:
    keys: dict[str, jwt.PyJWK] = {}
    raw_keys = document.get("keys")
    if not isinstance(raw_keys, list):
        return keys
    for raw in raw_keys:
        if not isinstance(raw, dict) or not isinstance(raw.get("kid"), str):
            continue
        if raw.get("use") not in (None, "sig"):
            continue
        try:
            keys[raw["kid"]] = jwt.PyJWK.from_dict(raw)
        except jwt.PyJWTError:
            continue
    return keys


class JwksCache:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        ttl_s: float = 300,
        min_refresh_interval_s: float = 30,
        failure_retry_s: float = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._ttl_s = ttl_s
        self._min_refresh_interval_s = min_refresh_interval_s
        self._failure_retry_s = failure_retry_s
        self._clock = clock
        self._state: dict[str, _IssuerKeys] = {}

    def _state_for(self, cfg: IssuerConfig) -> _IssuerKeys:
        """This issuer's key state; a static key set is parsed on first use (no I/O)."""
        state = self._state.setdefault(cfg.issuer, _IssuerKeys())
        if cfg.static_jwks is not None and state.fetched_at is None:
            state.keys = _parse_keys(cfg.static_jwks)
            state.fetched_at = self._clock()
        return state

    def knows(self, cfg: IssuerConfig, kid: str) -> bool:
        """Is ``kid`` in this issuer's current key map, at any age? Never does I/O.

        The verifier uses it to find the issuers that own a token's key before verifying, so a
        known kid never triggers another issuer's JWKS fetch.
        """
        return kid in self._state_for(cfg).keys

    async def get_key(self, cfg: IssuerConfig, kid: str) -> jwt.PyJWK:
        state = self._state_for(cfg)
        if cfg.static_jwks is not None:
            return self._lookup(state, kid)

        # Lock-free fast path: a fresh cached kid never waits behind an in-flight refresh.
        hit = self._cached(state, kid, self._clock())
        if hit is not None:
            return hit

        async with state.lock:
            now = self._clock()
            cached = self._cached(state, kid, now)  # another request may have refreshed meanwhile
            if cached is not None:
                return cached
            # After a failed fetch with nothing cached, retry quickly (a cold start racing the
            # IdP); once keys exist, unknown kids are rate limited to one fetch per interval.
            interval = (
                self._min_refresh_interval_s
                if state.keys
                else min(self._failure_retry_s, self._min_refresh_interval_s)
            )
            may_refresh = state.last_attempt is None or now - state.last_attempt >= interval
            if may_refresh:
                await self._refresh(cfg, state, now)
            key = state.keys.get(kid)
            if key is None:
                if state.last_refresh_failed:
                    raise JwksUnavailableError(cfg.issuer)
                raise KeyNotFoundError(kid)
            return key

    def _cached(self, state: _IssuerKeys, kid: str, now: float) -> jwt.PyJWK | None:
        fresh = state.fetched_at is not None and now - state.fetched_at < self._ttl_s
        return state.keys.get(kid) if fresh else None

    def _lookup(self, state: _IssuerKeys, kid: str) -> jwt.PyJWK:
        key = state.keys.get(kid)
        if key is None:
            raise KeyNotFoundError(kid)
        return key

    async def _refresh(self, cfg: IssuerConfig, state: _IssuerKeys, now: float) -> None:
        state.last_attempt = now
        try:
            response = await self._http.get(cfg.jwks_uri)
            response.raise_for_status()
            if len(response.content) > _MAX_JWKS_BYTES:
                raise ValueError("JWKS document too large")
            document = response.json()
            if not isinstance(document, dict):
                raise ValueError("JWKS document is not an object")
            keys = _parse_keys(document)
        except (httpx.HTTPError, ValueError) as exc:
            _log.warning("jwks_refresh_failed", issuer=cfg.issuer, error=type(exc).__name__)
            state.last_refresh_failed = True
            stale = state.fetched_at is not None and now - state.fetched_at < _MAX_STALE_S
            if not stale:
                state.keys = {}
            return
        state.keys = keys
        state.fetched_at = now
        state.last_refresh_failed = False
