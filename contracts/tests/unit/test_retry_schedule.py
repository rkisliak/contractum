"""The delivery retry policy (SUB-07, D-99): schedule, jitter, cap and its settings."""

from __future__ import annotations

import pytest
from dc_contracts.delivery import retry_delay
from dc_contracts.settings import ContractsSettings

pytestmark = pytest.mark.unit


def mid() -> float:
    return 0.5


def test_the_wait_follows_the_schedule_and_stops_at_the_attempt_cap() -> None:
    assert retry_delay(1, (60, 300), 3, rand=mid) == 60
    assert retry_delay(2, (60, 300), 3, rand=mid) == 300
    assert retry_delay(3, (60, 300), 3, rand=mid) is None


def test_jitter_is_plus_or_minus_ten_percent() -> None:
    assert retry_delay(1, (60, 300), 3, rand=lambda: 0.0) == pytest.approx(54.0)
    assert retry_delay(2, (60, 300), 3, rand=lambda: 0.0) == pytest.approx(270.0)
    assert retry_delay(1, (60, 300), 3, rand=lambda: 1.0) == pytest.approx(66.0)
    assert retry_delay(2, (60, 300), 3, rand=lambda: 1.0) == pytest.approx(330.0)


def test_an_attempt_past_the_schedule_reuses_its_last_entry() -> None:
    assert retry_delay(4, (60,), 5, rand=mid) == 60
    assert retry_delay(3, (60, 300), 9, rand=mid) == 300
    assert retry_delay(8, (60, 300), 9, rand=mid) == 300


def test_one_allowed_attempt_never_retries() -> None:
    assert retry_delay(1, (60, 300), 1, rand=mid) is None


def test_the_default_jitter_stays_inside_the_band() -> None:
    for _ in range(200):
        delay = retry_delay(1, (100,), 7)
        assert delay is not None
        assert 90.0 <= delay <= 110.0


def settings(**overrides: object) -> ContractsSettings:
    return ContractsSettings(
        database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never",  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


def test_the_defaults_are_the_documented_schedule() -> None:
    cfg = settings()
    assert cfg.notify_retry_schedule_s == (60, 300, 1800, 7200, 21600, 43200)
    assert cfg.notify_max_attempts == 7
    assert cfg.webhook_timeout_s == 10.0
    assert cfg.notify_lease_s == 120


@pytest.mark.parametrize(
    "overrides",
    [
        {"notify_retry_schedule_s": ()},
        {"notify_retry_schedule_s": (60, 0)},
        {"notify_retry_schedule_s": (-5,)},
        {"notify_max_attempts": 0},
        {"webhook_timeout_s": 0},
        {"notify_lease_s": 0},
        {"notify_batch_size": 0},
        {"notify_poll_interval_s": 0},
    ],
)
def test_settings_refuse_values_that_would_disable_or_hang_delivery(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="notify|webhook"):
        settings(**overrides)
