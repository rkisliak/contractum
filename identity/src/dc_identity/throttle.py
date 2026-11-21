"""Login backoff per account and per client IP, enforced inside identity (D-04, T-01-39).

The gateway's own rate limit is only an extra layer: identity must stay safe behind any gateway.
Counters live in ``login_throttle`` and are written in their own committed transaction by the
``system:login-throttle`` actor, so they persist although the login request itself fails. Updates
are exempt from the audit tripwire and write no event (D-23): high-volume, non-business churn.

A key locks once its failure count reaches its threshold, for ``min(2 ** (failures - threshold),
max_lock_s)`` seconds. A locked key answers 429 ``too-many-attempts`` with ``Retry-After`` before
any password hashing, so lockout also saves CPU. The IP key is best effort (shared egress,
proxies): the per-account key still limits an attacker who rotates addresses.

Counters are a sliding window (CR-01): a failure that arrives more than ``throttle_window_s``
after the key's previous failure starts a fresh count at 1 instead of adding to a lifetime total,
so a few typos a week behind one NAT never accumulate into a permanent lock. The window is longer
than ``throttle_max_lock_s``, so an attacker who keeps guessing as each lock expires keeps
escalating. A successful login resets only the account key: resetting the IP key as well would
let a client with one valid account wipe its own address counter between password-spraying
attempts, so the IP key relies on the window alone.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from dc_core.problems import ProblemError
from sqlalchemy import case, delete, func, literal, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from dc_identity.models import LoginThrottle

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dc_identity.security_policy import SecurityPolicy
    from dc_identity.settings import IdentitySettings

THROTTLE_ACTOR = "system:login-throttle"
ACCOUNT_PREFIX = "acct:"
IP_PREFIX = "ip:"

# 2 ** 20 seconds is far above any sane cap; bounding the exponent keeps power() finite in SQL.
_MAX_EXPONENT = 20


def throttle_keys(username: str, client_ip: str) -> list[str]:
    """The account key (lowercased username) followed by the client-IP key."""
    return [f"{ACCOUNT_PREFIX}{username.strip().lower()}", f"{IP_PREFIX}{client_ip}"]


class LoginThrottler:
    """``policy`` (quick 261120-0ye, D-40) carries the stored lockout thresholds and the longest
    lock; without one the env values apply. The failure-count window is the env window or twice the
    longest lock, whichever is larger, so escalation keeps working with a long lock (CR-01)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: IdentitySettings,
        policy: SecurityPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._account_threshold = (
            policy.lockout_account_threshold if policy else settings.throttle_account_threshold
        )
        self._ip_threshold = (
            policy.lockout_ip_threshold if policy else settings.throttle_ip_threshold
        )
        self._max_lock_s = policy.lockout_max_lock_s if policy else settings.throttle_max_lock_s
        self._window_s = max(settings.throttle_window_s, 2 * self._max_lock_s)

    def _threshold(self, key: str) -> int:
        if key.startswith(IP_PREFIX):
            return self._ip_threshold
        return self._account_threshold

    def _lock_until(self, failures: ColumnElement[int], threshold: int) -> ColumnElement[datetime]:
        exponent = func.least(failures - threshold, _MAX_EXPONENT)
        seconds = func.least(
            func.power(literal_column("2::double precision"), exponent),
            self._max_lock_s,
        )
        return func.now() + seconds * literal_column("interval '1 second'")

    def _window(self) -> ColumnElement[Any]:
        """The failure-count window as an SQL interval (an int from settings, never user input)."""
        return literal_column(f"interval '{int(self._window_s)} seconds'")

    async def ensure_not_locked(self, keys: Sequence[str]) -> None:
        """Raise 429 with ``Retry-After`` when any key is locked (no hashing has happened yet)."""
        remaining = func.extract("epoch", LoginThrottle.locked_until - func.now())
        stmt = select(func.max(remaining)).where(
            LoginThrottle.key.in_(list(keys)), LoginThrottle.locked_until > func.now()
        )
        async with self._session_factory() as session:
            seconds = (await session.execute(stmt)).scalar_one_or_none()
        if seconds is None:
            return
        retry_after = max(1, math.ceil(float(seconds)))
        raise ProblemError(
            429,
            "too-many-attempts",
            "Too Many Requests",
            "Too many failed sign-in attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    async def record_failure(self, keys: Sequence[str]) -> None:
        """Count one failed attempt on every key, committed in its own transaction."""
        async with self._session_factory() as session, session.begin():
            for key in sorted(set(keys)):  # fixed order: concurrent logins cannot deadlock
                threshold = self._threshold(key)
                stmt = pg_insert(LoginThrottle).values(
                    key=key,
                    failures=1,
                    locked_until=case(
                        (literal(1) >= threshold, self._lock_until(literal(1), threshold)),
                        else_=None,
                    ),
                    created_by=THROTTLE_ACTOR,
                    updated_by=THROTTLE_ACTOR,
                )
                stale = LoginThrottle.updated_at < func.now() - self._window()
                new_failures = case((stale, literal(1)), else_=LoginThrottle.failures + 1)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[LoginThrottle.key],
                    set_={
                        "failures": new_failures,
                        "locked_until": case(
                            (new_failures >= threshold, self._lock_until(new_failures, threshold)),
                            (stale, None),
                            else_=LoginThrottle.locked_until,
                        ),
                        "updated_by": THROTTLE_ACTOR,
                        "updated_at": func.now(),
                    },
                )
                await session.execute(stmt)

    async def reset(self, key: str) -> None:
        """Clear a key after a successful login (only the account key; IP keys only decay)."""
        async with self._session_factory() as session, session.begin():
            await session.execute(delete(LoginThrottle).where(LoginThrottle.key == key))
