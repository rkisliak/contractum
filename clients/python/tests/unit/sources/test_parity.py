"""The same logical rows give the same verdict in every input format and in many threads."""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import pyarrow as pa
import pytest
from support.datasets import (
    arrow_table,
    flat_orders_contract,
    orders_rows,
    write_all_formats,
    write_delta,
)
from support.odcs import contract_doc

from contractum.report.model import Report
from contractum.testing import TestOptions, run_test

_HAS_DELTA = importlib.util.find_spec("deltalake") is not None

# format -> how to hand the rows to run_test; "csv" and "tsv" cannot hold arrays and objects.
FORMATS = [
    "pandas",
    "polars",
    "pyarrow",
    "parquet",
    "csv",
    "tsv",
    "json",
    "ndjson",
    pytest.param(
        "delta",
        marks=pytest.mark.skipif(not _HAS_DELTA, reason="the delta extra is not installed"),
    ),
]
NESTED_FORMATS = [name for name in FORMATS if name not in ("csv", "tsv")]


def _data(fmt: str, tmp_path: Path, rows: list[dict[str, Any]]) -> Any:
    table = arrow_table(rows)
    if fmt == "pandas":
        return pd.DataFrame(rows)
    if fmt == "polars":
        return pl.from_arrow(table)
    if fmt == "pyarrow":
        return table
    if fmt == "delta":
        return write_delta(tmp_path / "delta", rows)
    return write_all_formats(tmp_path, rows)[fmt]


def _verdict(report: Report) -> list[tuple[Any, ...]]:
    """Check id, status and failed rows; a coerced text column reports its type check by count."""
    return [(c.id, c.status, None if c.rule == "type" else c.failed_rows) for c in report.checks]


@pytest.mark.parametrize("good", [True, False], ids=["good rows", "bad rows"])
@pytest.mark.parametrize("fmt", FORMATS)
def test_flat_rows_give_the_pyarrow_verdict_in_every_format(
    fmt: str, good: bool, tmp_path: Path
) -> None:
    rows = orders_rows(good=good, flat=True)
    contract = flat_orders_contract()
    baseline = run_test(contract, arrow_table(rows))

    report = run_test(contract, _data(fmt, tmp_path, rows))

    assert _verdict(report) == _verdict(baseline)
    assert report.ok is good
    assert report.dataset.rows_tested == len(rows)


@pytest.mark.parametrize("good", [True, False], ids=["good rows", "bad rows"])
@pytest.mark.parametrize("fmt", NESTED_FORMATS)
def test_nested_rows_give_the_pyarrow_verdict_in_every_format_that_holds_them(
    fmt: str, good: bool, tmp_path: Path
) -> None:
    rows = orders_rows(good=good)
    baseline = run_test(contract_doc(), arrow_table(rows))

    report = run_test(contract_doc(), _data(fmt, tmp_path, rows))

    assert _verdict(report) == _verdict(baseline)
    assert report.ok is good


def test_the_bad_rows_really_fail_the_named_checks(tmp_path: Path) -> None:
    report = run_test(flat_orders_contract(), _data("csv", tmp_path, orders_rows(False, flat=True)))

    failed = {check.id for check in report.checks if check.status == "failed"}
    assert {
        "orders.order_id.required",
        "orders.order_id.pattern",
        "orders.primary_key",
        "orders.customer_email.format",
        "orders.amount.range",
        "orders.amount.multiple_of",
        "orders.quantity.range",
        "orders.status.enum",
    } <= failed


def _scenarios(tmp_path: Path) -> list[tuple[dict[str, Any], Any]]:
    flat, full = flat_orders_contract(), contract_doc()
    files = write_all_formats(tmp_path, orders_rows(False, flat=True))
    return [
        (flat, arrow_table(orders_rows(True, flat=True))),
        (flat, arrow_table(orders_rows(False, flat=True))),
        (full, pd.DataFrame(orders_rows(True))),
        (flat, files["csv"]),
        (flat, files["ndjson"]),
        (full, pl.from_arrow(arrow_table(orders_rows(False)))),
    ]


def test_concurrent_run_test_calls_equal_their_sequential_results(tmp_path: Path) -> None:
    scenarios = _scenarios(tmp_path)
    options = TestOptions(sample=2, seed=5)
    sequential = [_verdict(run_test(c, d, options=options)) for c, d in scenarios]
    start = threading.Barrier(len(scenarios))

    def worker(index: int) -> list[tuple[Any, ...]]:
        start.wait()
        contract, data = scenarios[index]
        return _verdict(run_test(contract, data, options=options))

    for _ in range(3):
        with ThreadPoolExecutor(max_workers=len(scenarios)) as pool:
            concurrent = list(pool.map(worker, range(len(scenarios))))
        assert concurrent == sequential


def test_the_same_dataset_tested_from_many_threads_gives_one_answer(tmp_path: Path) -> None:
    contract = flat_orders_contract()
    data = _data("csv", tmp_path, orders_rows(False, flat=True))
    expected = _verdict(run_test(contract, data))
    run: Callable[[int], list[tuple[Any, ...]]] = lambda _: _verdict(run_test(contract, data))  # noqa: E731

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(16)))

    assert all(result == expected for result in results)


def test_pyarrow_baseline_is_not_trivially_empty() -> None:
    report = run_test(flat_orders_contract(), arrow_table(orders_rows(True, flat=True)))

    assert report.summary.passed > 20
    assert isinstance(arrow_table(orders_rows()), pa.Table)
