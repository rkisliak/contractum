"""Rule-impact pages cannot starve save and publish runs (WR-03, D-161).

The scarce resource is the worker: a batch document holds a run slot and a worker while it is
evaluated. So the batch pool is kept below ``eval_workers`` (and the run slots), a configuration
that could let batches hold every worker is refused at start-up, and a page stops once its
wall-clock budget is spent or its caller has disconnected.
"""

from __future__ import annotations

import asyncio

import pytest
from dc_core.problems import ProblemError
from dc_validation.batch import PageClock
from dc_validation.settings import ValidationSettings
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def settings(**overrides: object) -> ValidationSettings:
    return ValidationSettings(database_url="postgresql+psycopg://x@localhost/x", **overrides)  # type: ignore[arg-type]


def test_the_default_pool_leaves_a_worker_for_runs() -> None:
    defaults = settings()
    assert defaults.eval_workers == 2
    assert defaults.batch_slot_count() == 1 < defaults.eval_workers


@pytest.mark.parametrize(
    ("workers", "runs", "expected"),
    [(2, 4, 1), (4, 4, 2), (4, 8, 3), (8, 4, 2), (1, 4, 1), (1, 1, 1)],
)
def test_the_derived_pool_is_below_the_workers_and_the_run_slots(
    workers: int, runs: int, expected: int
) -> None:
    sized = settings(eval_workers=workers, max_concurrent_runs=runs)
    assert sized.batch_slot_count() == expected
    if workers > 1:
        assert sized.batch_slot_count() <= workers - 1


@pytest.mark.parametrize(
    ("overrides", "word"),
    [
        ({"eval_workers": 2, "max_concurrent_runs": 4, "batch_slots": 2}, "eval_workers"),
        ({"eval_workers": 4, "max_concurrent_runs": 3, "batch_slots": 3}, "max_concurrent_runs"),
    ],
)
def test_a_pool_that_could_hold_every_worker_or_run_slot_is_refused(
    overrides: dict[str, int], word: str
) -> None:
    with pytest.raises(ValidationError, match=word):
        settings(**overrides)


def test_a_configured_pool_below_the_workers_is_kept() -> None:
    assert settings(eval_workers=4, max_concurrent_runs=8, batch_slots=3).batch_slot_count() == 3


async def test_a_page_clock_passes_while_the_budget_lasts_and_the_caller_is_there() -> None:
    async def connected() -> bool:
        return False

    clock = PageClock(60.0, connected)
    await clock.check()


async def test_a_spent_budget_stops_the_page_with_503() -> None:
    clock = PageClock(0.01, None)
    await asyncio.sleep(0.02)
    with pytest.raises(ProblemError) as stopped:
        await clock.check()
    assert stopped.value.status == 503
    assert stopped.value.slug == "validation-unavailable"


async def test_a_disconnected_caller_stops_the_page_with_503() -> None:
    async def gone() -> bool:
        return True

    with pytest.raises(ProblemError) as stopped:
        await PageClock(60.0, gone).check()
    assert stopped.value.status == 503
