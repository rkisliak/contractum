"""``effective_warn_days``: stored over env, env when nothing was read (quick 261120-0ye, D-34)."""

from __future__ import annotations

from typing import Any

import pytest
from dc_validation.runtime_settings import effective_warn_days
from dc_validation.settings import ValidationSettings

pytestmark = pytest.mark.unit


class FakeClient:
    def __init__(self, value: dict[str, Any] | None) -> None:
        self.value = value
        self.calls: list[tuple[str, str]] = []

    async def get(self, section: str, *, bearer: str) -> dict[str, Any] | None:
        self.calls.append((section, bearer))
        return self.value


def settings(days: int = 14) -> ValidationSettings:
    return ValidationSettings(mandatory_warn_days=days)


async def test_a_stored_value_wins_and_is_read_with_the_callers_bearer() -> None:
    client = FakeClient({"mandatory_warn_days": 3})
    assert await effective_warn_days(client, "tok", settings()) == 3  # type: ignore[arg-type]
    assert client.calls == [("governance", "tok")]


async def test_a_stored_zero_is_a_value_not_an_absence() -> None:
    assert await effective_warn_days(FakeClient({"mandatory_warn_days": 0}), "t", settings()) == 0  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value", [None, {}, {"mandatory_warn_days": None}, {"mandatory_warn_days": -4}, {"x": 1}]
)
async def test_everything_else_falls_back_to_the_env_value(value: dict[str, Any] | None) -> None:
    assert await effective_warn_days(FakeClient(value), "t", settings(9)) == 9  # type: ignore[arg-type]


async def test_no_client_or_no_bearer_uses_the_env_value() -> None:
    assert await effective_warn_days(None, "t", settings(7)) == 7
    assert await effective_warn_days(FakeClient({"mandatory_warn_days": 1}), None, settings(7)) == 7  # type: ignore[arg-type]
