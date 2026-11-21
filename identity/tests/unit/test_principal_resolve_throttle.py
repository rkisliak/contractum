"""``PrincipalResolveThrottle``: a rolling-minute budget per principal that forgets idle callers."""

from __future__ import annotations

import pytest
from dc_core.problems import ProblemError
from dc_identity.principal_resolve import PrincipalResolveThrottle

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_spending_past_the_budget_is_429_with_the_seconds_until_room_returns() -> None:
    clock = Clock()
    throttle = PrincipalResolveThrottle(10, clock=clock)
    throttle.spend("local:a", 6)
    clock.now += 20
    with pytest.raises(ProblemError) as caught:
        throttle.spend("local:a", 5)
    assert (caught.value.status, caught.value.slug) == (429, "too-many-requests")
    assert caught.value.headers == {"Retry-After": "40"}


def test_names_older_than_a_minute_return_to_the_budget() -> None:
    clock = Clock()
    throttle = PrincipalResolveThrottle(10, clock=clock)
    throttle.spend("local:a", 10)
    clock.now += 60
    throttle.spend("local:a", 10)  # the first batch left the window: no 429
    with pytest.raises(ProblemError):
        throttle.spend("local:a", 1)


def test_each_principal_has_its_own_budget() -> None:
    throttle = PrincipalResolveThrottle(5, clock=Clock())
    throttle.spend("local:a", 5)
    throttle.spend("local:b", 5)
    with pytest.raises(ProblemError):
        throttle.spend("local:a", 1)


def test_many_idle_principals_are_swept_while_active_ones_keep_their_spend() -> None:
    clock = Clock()
    throttle = PrincipalResolveThrottle(3, clock=clock)
    for index in range(1025):
        throttle.spend(f"local:idle-{index}", 1)
    clock.now += 30
    throttle.spend("local:active", 3)
    clock.now += 31  # the idle callers are now older than the window, "active" is not
    throttle.spend("local:new", 1)
    assert set(throttle._spent) == {"local:active", "local:new"}
    with pytest.raises(ProblemError):
        throttle.spend("local:active", 1)
