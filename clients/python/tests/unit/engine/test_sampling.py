"""Seeded sampling and limits: boundaries, reproducibility and honest row positions."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.backend import RunOptions
from contractum.engine.checks import NotNull
from contractum.engine.sampling import SamplingInfo, apply_sampling
from contractum.errors import UsageError


def _table(rows: int) -> pa.Table:
    return pa.table({"i": pa.array(range(rows), pa.int64())})


def _positions(table: pa.Table) -> list[int]:
    return table["i"].to_pylist()


@pytest.mark.parametrize("sample", [10, 11, 1000, 1.0])
def test_a_sample_covering_every_row_tests_all_rows_and_is_not_a_sample(sample: Any) -> None:
    table = _table(10)

    picked, info = apply_sampling(table, sample=sample)

    assert picked is table
    assert info == SamplingInfo(rows_total=10, rows_tested=10, sampled=False, limited=False)
    assert info.row_index is None


def test_an_integer_sample_draws_that_many_rows_in_source_order() -> None:
    picked, info = apply_sampling(_table(1000), sample=25, seed=7)

    positions = _positions(picked)
    assert len(positions) == 25 == info.rows_tested
    assert positions == sorted(positions)
    assert len(set(positions)) == 25
    assert (info.rows_total, info.sampled, info.limited) == (1000, True, False)
    assert list(info.row_index or ()) == positions


@pytest.mark.parametrize(
    ("rows", "fraction", "expected"),
    [
        (100, 0.29, 29),
        (30, 0.1, 3),
        (10, 0.7, 7),
        (5, 0.5, 2),
        (10, 0.001, 1),
        (1000, 0.25, 250),
        (3, 0.5, 1),
    ],
)
def test_a_fraction_draws_floor_of_the_decimal_product_but_at_least_one_row(
    rows: int, fraction: float, expected: int
) -> None:
    picked, info = apply_sampling(_table(rows), sample=fraction)

    assert picked.num_rows == expected == info.rows_tested
    assert info.sampled is True


def test_a_fraction_that_rounds_to_every_row_is_not_a_sample() -> None:
    _, info = apply_sampling(_table(1), sample=0.5)

    assert (info.rows_tested, info.sampled) == (1, False)


@pytest.mark.parametrize(
    "sample", [0, -1, 0.0, -0.5, 1.0000001, 1.5, float("nan"), float("inf"), True, "5", [1]]
)
def test_invalid_sample_values_are_usage_errors(sample: Any) -> None:
    with pytest.raises(UsageError, match="--sample"):
        apply_sampling(_table(10), sample=sample)


@pytest.mark.parametrize("limit", [0, -3, True, 1.5, "5"])
def test_invalid_limits_are_usage_errors(limit: Any) -> None:
    with pytest.raises(UsageError, match="--limit"):
        apply_sampling(_table(10), limit=limit)


@pytest.mark.parametrize("seed", [-1, True, 1.5])
def test_invalid_seeds_are_usage_errors(seed: Any) -> None:
    with pytest.raises(UsageError, match="--seed"):
        apply_sampling(_table(10), sample=2, seed=seed)


def test_the_same_seed_selects_the_same_rows_and_another_seed_other_rows() -> None:
    table = _table(2000)

    first = apply_sampling(table, sample=20, seed=3)[0]
    again = apply_sampling(table, sample=20, seed=3)[0]
    other = apply_sampling(table, sample=20, seed=4)[0]
    default_a = apply_sampling(table, sample=20)[0]
    default_b = apply_sampling(table, sample=20, seed=0)[0]

    assert _positions(first) == _positions(again)
    assert _positions(first) != _positions(other)
    assert _positions(default_a) == _positions(default_b)


def test_row_index_holds_the_original_positions_of_the_sampled_rows() -> None:
    picked, info = apply_sampling(_table(500), sample=0.1, seed=11)

    assert list(info.row_index or ()) == _positions(picked)


def test_limit_keeps_the_first_rows_and_reports_that_it_cut() -> None:
    picked, info = apply_sampling(_table(10), limit=4)

    assert _positions(picked) == [0, 1, 2, 3]
    assert info == SamplingInfo(rows_total=10, rows_tested=4, sampled=False, limited=True)


@pytest.mark.parametrize("limit", [10, 11])
def test_a_limit_at_or_above_the_row_count_cuts_nothing(limit: int) -> None:
    picked, info = apply_sampling(_table(10), limit=limit)

    assert picked.num_rows == 10
    assert info.limited is False


def test_limit_applies_before_the_sample() -> None:
    picked, info = apply_sampling(_table(1000), sample=10, limit=100, seed=5)

    assert max(_positions(picked)) < 100
    assert (info.rows_total, info.rows_tested, info.sampled, info.limited) == (
        1000,
        10,
        True,
        True,
    )


def test_an_empty_table_is_never_sampled() -> None:
    picked, info = apply_sampling(_table(0), sample=5)
    fraction, fraction_info = apply_sampling(_table(0), sample=0.5)

    assert (picked.num_rows, info.sampled) == (0, False)
    assert (fraction.num_rows, fraction_info.sampled) == (0, False)


def test_sampling_works_on_chunked_tables() -> None:
    table = pa.concat_tables([_table(5), pa.table({"i": pa.array(range(5, 10), pa.int64())})])

    picked, info = apply_sampling(table, sample=4, seed=1)

    assert list(info.row_index or ()) == _positions(picked)
    assert len(_positions(picked)) == 4


def test_samples_in_outcomes_cite_source_rows_not_sample_positions() -> None:
    table = pa.table({"x": pa.array([None] * 200, pa.int64())})
    picked, info = apply_sampling(table, sample=20, seed=2)
    backend = ArrowBackend.from_sampling(picked, info)
    spec = NotNull(check_id="t.x.required", category="schema", rule="required", column="x")

    (outcome,) = backend.evaluate([spec], RunOptions(max_failures=3))

    assert [s.row for s in outcome.samples] == list(info.row_index or ())[:3]
    assert outcome.sampled is True
    assert (outcome.failed_rows, outcome.total_rows) == (20, 20)
    assert backend.rows_total == 200
    assert backend.rows_tested == 20


def test_a_limited_table_keeps_its_positions_and_is_not_marked_sampled() -> None:
    table = pa.table({"x": pa.array([1, None, 3, None, 5], pa.int64())})
    picked, info = apply_sampling(table, limit=4)
    backend = ArrowBackend.from_sampling(picked, info)
    spec = NotNull(check_id="t.x.required", category="schema", rule="required", column="x")

    (outcome,) = backend.evaluate([spec], RunOptions())

    assert [s.row for s in outcome.samples] == [1, 3]
    assert outcome.sampled is False
    assert backend.rows_total == 5
