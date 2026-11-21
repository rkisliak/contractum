"""Bulk, tri-state principal existence check (D-153, VAL-05).

Answers, per submitted name, exactly one of ``found``, ``not_found`` or ``unverifiable``: never a
display name, id, e-mail, membership or activity. A user name matches, lower-cased and trimmed, a
local user's username or e-mail or a known Entra user's UPN hint. An unmatched name that looks like
an e-mail or UPN while an Entra issuer is configured is ``unverifiable`` (identity has simply never
seen that Entra user), any other unmatched name is ``not_found``. A group name matches a local
group's name or a registered Entra group's display name, case-insensitively.
"""

from __future__ import annotations

import math
import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from dc_auth.authz_wire import PrincipalResolveOut, RefState
from dc_core.problems import ProblemError
from fastapi import FastAPI
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_identity.models import EntraRegistration, EntraUser, LocalGroup, User

_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+$")
_WINDOW_S = 60.0
_SWEEP_ABOVE = 1024


def _normalise(name: str) -> str:
    return name.strip().lower()


async def _existing(session: AsyncSession, statements: Iterable[Select[Any]]) -> set[str]:
    found: set[str] = set()
    for statement in statements:
        found.update(row[0] for row in (await session.execute(statement)).all())
    return found


async def resolve_principals(
    session: AsyncSession, *, entra_configured: bool, users: list[str], groups: list[str]
) -> PrincipalResolveOut:
    """Set-based lookups (one query per source), then the tri-state per submitted name."""
    user_keys = {_normalise(name) for name in users}
    group_keys = {_normalise(name) for name in groups}

    known_users: set[str] = set()
    if user_keys:
        known_users = await _existing(
            session,
            [
                select(func.lower(User.username)).where(func.lower(User.username).in_(user_keys)),
                select(func.lower(User.email)).where(func.lower(User.email).in_(user_keys)),
                select(func.lower(EntraUser.preferred_username)).where(
                    func.lower(EntraUser.preferred_username).in_(user_keys)
                ),
            ],
        )
    known_groups: set[str] = set()
    if group_keys:
        known_groups = await _existing(
            session,
            [
                select(func.lower(LocalGroup.name)).where(
                    func.lower(LocalGroup.name).in_(group_keys)
                ),
                select(func.lower(EntraRegistration.display_name)).where(
                    EntraRegistration.kind == "group",
                    func.lower(EntraRegistration.display_name).in_(group_keys),
                ),
            ],
        )

    def user_state(name: str) -> RefState:
        key = _normalise(name)
        if key in known_users:
            return "found"
        if entra_configured and _EMAIL_SHAPE.match(key):
            return "unverifiable"
        return "not_found"

    return PrincipalResolveOut(
        users={name: user_state(name) for name in users},
        groups={
            name: "found" if _normalise(name) in known_groups else "not_found" for name in groups
        },
    )


class PrincipalResolveThrottle:
    """Per-principal rolling-minute budget of submitted names, in process memory (Pitfall 12).

    The residual risk is accepted: identity may run several replicas, so the effective budget is
    the limit times the replica count. The route stays a bounded existence oracle, not a directory.
    """

    def __init__(self, names_per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = names_per_minute
        self._clock = clock
        self._spent: dict[str, deque[tuple[float, int]]] = {}

    def spend(self, principal_id: str, names: int) -> None:
        """Take ``names`` from the principal's budget, or raise 429 with ``Retry-After``."""
        now = self._clock()
        if len(self._spent) > _SWEEP_ABOVE:
            self._sweep(now)
        window = self._spent.setdefault(principal_id, deque())
        while window and window[0][0] <= now - _WINDOW_S:
            window.popleft()
        if sum(count for _, count in window) + names > self._limit:
            oldest = window[0][0] if window else now
            retry_after = max(1, math.ceil(oldest + _WINDOW_S - now))
            raise ProblemError(
                429,
                "too-many-requests",
                "Too Many Requests",
                "Too many principal lookups. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )
        window.append((now, names))

    def _sweep(self, now: float) -> None:
        for key in [k for k, w in self._spent.items() if not w or w[-1][0] <= now - _WINDOW_S]:
            del self._spent[key]


def throttle_of(app: FastAPI) -> PrincipalResolveThrottle:
    """The process-wide throttle, created on first use from the identity settings."""
    throttle = getattr(app.state, "principal_resolve_throttle", None)
    if throttle is None:
        throttle = PrincipalResolveThrottle(app.state.settings.principal_resolve_names_per_minute)
        app.state.principal_resolve_throttle = throttle
    return throttle
