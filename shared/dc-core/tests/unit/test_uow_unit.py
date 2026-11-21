"""UnitOfWork guards and the JSON-safe snapshot (no database needed)."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from dc_core.actor import system_actor
from dc_core.uow import UnitOfWork

pytestmark = pytest.mark.unit


class Colour(enum.Enum):
    RED = "red"
    NESTED = uuid.UUID(int=3)


class Snapshotted:
    __audit_fields__ = ("id", "when", "day", "colour", "nested", "labels", "tags", "plain")

    def __init__(self) -> None:
        self.id = uuid.UUID(int=1)
        self.when = datetime(2026, 11, 21, 8, 30, tzinfo=UTC)
        self.day = date(2026, 11, 21)
        self.colour = Colour.RED
        self.nested = Colour.NESTED
        self.labels = {1: uuid.UUID(int=2), "k": [date(2026, 1, 2)]}
        self.tags = ("a", Colour.RED)
        self.plain = 5
        self.secret = "never in a snapshot"


def _unused_factory() -> Any:
    raise AssertionError("the session factory must not be called")


def _uow() -> UnitOfWork:
    return UnitOfWork(
        _unused_factory,  # type: ignore[arg-type]
        actor=system_actor("probe"),
        outbox_model=object,
        service="probe",
    )


def test_snapshot_converts_every_value_to_json_safe_types() -> None:
    assert UnitOfWork.snapshot(Snapshotted()) == {
        "id": "00000000-0000-0000-0000-000000000001",
        "when": "2026-11-21T08:30:00+00:00",
        "day": "2026-11-21",
        "colour": "red",
        "nested": "00000000-0000-0000-0000-000000000003",
        "labels": {"1": "00000000-0000-0000-0000-000000000002", "k": ["2026-01-02"]},
        "tags": ["a", "red"],
        "plain": 5,
    }


def test_the_session_is_only_available_inside_async_with() -> None:
    with pytest.raises(RuntimeError, match="outside 'async with'"):
        _ = _uow().session


async def test_an_unentered_unit_has_nothing_pending_and_exits_quietly() -> None:
    uow = _uow()
    assert uow.has_pending() is False
    await uow.commit_if_pending()  # nothing to commit: no session is ever opened
    await uow.__aexit__(None, None, None)
    assert uow.committed is False
