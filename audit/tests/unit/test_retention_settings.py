"""Retention settings and the effective-days rule (quick 261120-0ye, D-42)."""

from __future__ import annotations

import pytest
from dc_audit.retention import effective_days
from dc_audit.settings import AuditSettings
from pydantic import ValidationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("stored", "env", "floor", "expected"),
    [
        (None, None, 90, None),  # nothing anywhere: keep for ever
        (0, 365, 90, None),  # a stored zero beats the env value
        (None, 365, 90, 365),
        (400, 365, 90, 400),  # stored beats env
        (None, 30, 90, 90),  # the floor beats a lower env value
        (31, None, 90, 90),  # and a lower stored value
        (True, 365, 90, 365),  # a bool is not a number of days
        ("100", None, 90, None),  # nor is a string
        (36500, None, 90, 36500),
    ],
)
def test_effective_days(stored: object, env: int | None, floor: int, expected: int | None) -> None:
    assert effective_days(stored, env, floor) == expected


def test_the_defaults_keep_everything() -> None:
    settings = AuditSettings()
    assert settings.retention_days is None
    assert (settings.retention_min_days, settings.retention_interval_s) == (90, 3600)
    assert (settings.retention_batch, settings.retention_max_batches) == (5000, 100)


@pytest.mark.parametrize("floor", [0, 29])
def test_a_floor_below_thirty_days_is_refused_at_startup(floor: int) -> None:
    with pytest.raises(ValidationError):
        AuditSettings(retention_min_days=floor)


def test_a_floor_of_thirty_is_allowed() -> None:
    assert AuditSettings(retention_min_days=30).retention_min_days == 30
