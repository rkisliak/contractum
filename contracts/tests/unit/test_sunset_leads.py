"""D-96: the lead rule picks the smallest configured lead that still covers the days left."""

from __future__ import annotations

import pytest
from dc_contracts.scheduler import LOCK_KEY, pick_lead
from dc_contracts.settings import ContractsSettings
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("days_left", "expected"),
    [
        (30, 30),  # the boundary is inclusive
        (31, None),  # further away than the largest lead
        (29, 30),
        (8, 30),
        (7, 7),  # the boundary is inclusive
        (3, 7),
        (0, 7),  # the sunset is today
        (-1, None),  # the sunset has passed
    ],
)
def test_pick_lead_uses_the_smallest_lead_that_covers_the_days_left(
    days_left: int, expected: int | None
) -> None:
    assert pick_lead(days_left, (30, 7)) == expected


def test_the_order_of_the_configured_leads_does_not_matter() -> None:
    for days_left in range(-2, 40):
        assert pick_lead(days_left, (7, 30)) == pick_lead(days_left, (30, 7))


def test_the_lock_key_is_the_documented_one() -> None:
    assert LOCK_KEY == "dc-card-sunset"


@given(
    leads=st.sets(st.integers(min_value=1, max_value=400), min_size=1, max_size=6),
    days_left=st.integers(min_value=-5, max_value=400),
)
def test_pick_lead_is_none_or_the_minimum_lead_covering_the_days_left(
    leads: set[int], days_left: int
) -> None:
    ordered = sorted(leads)
    first = pick_lead(days_left, ordered)
    covering = [lead for lead in leads if lead >= days_left]
    if days_left < 0 or not covering:
        assert first is None
    else:
        assert first == min(covering)
    # Idempotent and order-independent: the same inputs always give the same lead.
    assert pick_lead(days_left, ordered) == first
    assert pick_lead(days_left, list(reversed(ordered))) == first


def test_the_default_leads_are_thirty_and_seven_and_stored_ascending() -> None:
    assert ContractsSettings().sunset_lead_days == (7, 30)
    assert ContractsSettings(sunset_lead_days=(30, 7, 7)).sunset_lead_days == (7, 30)


def test_settings_reject_an_empty_lead_list() -> None:
    with pytest.raises(ValidationError):
        ContractsSettings(sunset_lead_days=())


@pytest.mark.parametrize("bad", [(0,), (-3, 7), (30, 0)])
def test_settings_reject_non_positive_leads(bad: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError):
        ContractsSettings(sunset_lead_days=bad)


def test_settings_reject_a_non_positive_scan_interval() -> None:
    with pytest.raises(ValidationError):
        ContractsSettings(sunset_scan_interval_s=0)
