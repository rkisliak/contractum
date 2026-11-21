"""Limit and sample a table before the checks run (DATA-04).

``limit`` keeps the first N rows. ``sample`` keeps N random rows (an integer) or a fraction of
them (a float in (0, 1]), chosen with ``pyarrow.compute.random`` seeded by ``seed``, so the same
seed and the same pyarrow version pick the same rows. Sampled rows keep their order and their
original positions, which is what failure samples in the report cite.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

import pyarrow as pa
import pyarrow.compute as pc

from contractum.errors import UsageError

_BAD_SAMPLE = "--sample must be a row count of at least 1 or a fraction above 0 and up to 1"


@dataclass(frozen=True)
class SamplingInfo:
    """What was tested out of the source.

    ``rows_total`` is the source size, ``rows_tested`` the rows the checks see, ``sampled`` is true
    only when a random subset was drawn, ``limited`` when ``limit`` cut rows off. ``row_index``
    holds the source position of each tested row of a sample, else None (positions are the
    table positions).
    """

    rows_total: int
    rows_tested: int
    sampled: bool = False
    limited: bool = False
    row_index: array[int] | None = None


def _checked_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise UsageError("--limit must be a whole number of at least 1")
    return limit


def _checked_seed(seed: int) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise UsageError("--seed must be a whole number of at least 0")
    return seed


def _sample_size(sample: int | float | None, rows: int) -> int | None:
    """How many rows to draw, or None when no sample was asked for."""
    if sample is None:
        return None
    if isinstance(sample, bool):
        raise UsageError(_BAD_SAMPLE)
    if isinstance(sample, int):
        if sample < 1:
            raise UsageError(_BAD_SAMPLE)
        return sample
    if not isinstance(sample, float) or not 0 < sample <= 1:
        raise UsageError(_BAD_SAMPLE)
    # The fraction is the decimal the user typed: 0.29 of 100 rows is exactly 29.
    wanted = int((Decimal(repr(sample)) * rows).to_integral_value(ROUND_FLOOR))
    return max(1, wanted)


def _choose(rows: int, size: int, seed: int) -> pa.Array:
    """Source positions of ``size`` seeded random rows, ascending."""
    randoms = pc.random(rows, initializer=seed)
    chosen = pc.sort_indices(randoms)[:size]
    return pc.take(chosen, pc.sort_indices(chosen))


def apply_sampling(
    table: pa.Table,
    *,
    sample: int | float | None = None,
    seed: int = 0,
    limit: int | None = None,
) -> tuple[pa.Table, SamplingInfo]:
    """Limit then sample a table; returns the rows to test and what was done.

    An integer ``sample`` at or above the row count tests every row (``sampled`` false); a float
    ``sample`` f tests ``max(1, floor(f * rows))`` rows. A ``limit`` or ``sample`` below 1 and a
    fraction outside (0, 1] raise a UsageError.
    """
    keep = _checked_limit(limit)
    start = _checked_seed(seed)
    total = table.num_rows
    window = table if keep is None else table.slice(0, keep)
    limited = keep is not None and keep < total
    rows = window.num_rows
    size = _sample_size(sample, rows)
    if size is None or size >= rows:
        return window, SamplingInfo(total, rows, sampled=False, limited=limited)
    chosen = _choose(rows, size, start)
    index = array("q", chosen.to_pylist())
    return window.take(chosen), SamplingInfo(
        total, size, sampled=True, limited=limited, row_index=index
    )
