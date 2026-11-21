"""Schema-only runs: names and types from a schema, a footer, a frame or a log, never rows."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from support.datasets import ARROW_SCHEMA, arrow_table, orders_rows, write_csv
from support.odcs import contract_doc

from contractum.engine.types import is_string
from contractum.errors import (
    DataLoadError,
    ExtraNotInstalledError,
    UnsupportedDatasetError,
    UsageError,
)
from contractum.sources import load_dataset, schema_of
from contractum.sources import schema_only as schema_only_module
from contractum.testing import SCHEMA_ONLY_REASON, TestOptions, run_test

SCHEMA_ONLY = TestOptions(schema_only=True)


def _status(report: Any) -> dict[str, str]:
    return {check.id: check.status for check in report.checks}


def test_an_arrow_schema_is_judged_on_presence_and_types_only() -> None:
    report = run_test(contract_doc(), ARROW_SCHEMA, options=SCHEMA_ONLY)
    status = _status(report)

    assert report.ok
    assert report.dataset.schema_only
    assert report.dataset.rows_tested == 0
    assert report.dataset.rows_total is None
    assert status["orders.order_id.present"] == "passed"
    assert status["orders.order_id.type"] == "passed"
    assert status["orders.shipping.city.present"] == "passed"
    assert status["orders.shipping.city.type"] == "passed"
    assert status["orders.extra_columns"] == "passed"


def test_every_check_that_needs_rows_is_reported_skipped() -> None:
    report = run_test(contract_doc(), ARROW_SCHEMA, options=SCHEMA_ONLY)
    value_checks = [
        check
        for check in report.checks
        if check.rule not in ("present", "type", "physical_type", "extra_columns")
    ]

    assert {"required", "pattern", "primary_key", "range", "enum"} <= {c.rule for c in value_checks}
    assert {c.status for c in value_checks} == {"skipped"}
    assert {c.skipped_reason for c in value_checks} == {SCHEMA_ONLY_REASON}
    assert report.summary.failed == 0


def test_a_missing_wrong_typed_and_extra_column_are_reported() -> None:
    schema = pa.schema(
        [
            pa.field("order_id", pa.int64()),
            pa.field("amount", pa.float64()),
            pa.field("surprise", pa.string()),
            pa.field("shipping", pa.struct([pa.field("zip", pa.string())])),
        ]
    )

    report = run_test(contract_doc(), schema, options=SCHEMA_ONLY)
    status = _status(report)

    assert status["orders.order_id.type"] == "failed"
    assert status["orders.quantity.present"] == "failed"
    assert status["orders.shipping.city.present"] == "failed"
    assert status["orders.shipping.zip.present"] == "passed"
    assert status["orders.extra_columns"] == "warning"
    assert not report.ok


def test_a_physical_type_is_compared_without_data() -> None:
    document = contract_doc()
    document["schema"][0]["properties"][3]["physicalType"] = "int32"
    ok = pa.schema([pa.field("quantity", pa.int32())])
    wrong = pa.schema([pa.field("quantity", pa.int64())])

    passed = _status(run_test(document, ok, options=SCHEMA_ONLY))
    failed = _status(run_test(document, wrong, options=SCHEMA_ONLY))

    assert passed["orders.quantity.physical_type"] == "passed"
    assert failed["orders.quantity.physical_type"] == "failed"


def _spy_on_data_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []

    def spy(name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            reads.append(name)
            raise AssertionError(f"{name} must not be called in a schema-only run")

        return fail

    monkeypatch.setattr(pq, "read_table", spy("read_table"))
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", spy("iter_batches"))
    monkeypatch.setattr(pq.ParquetFile, "read", spy("read"))
    monkeypatch.setattr(pq.ParquetFile, "read_row_group", spy("read_row_group"))
    return reads


def test_a_parquet_footer_is_checked_without_reading_a_data_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "orders.parquet"
    pq.write_table(arrow_table(orders_rows(good=True)), path)
    reads = _spy_on_data_reads(monkeypatch)

    report = run_test(contract_doc(), path, options=SCHEMA_ONLY)

    assert reads == []
    assert report.ok
    assert report.dataset.kind == "parquet"
    assert report.dataset.location == str(path)
    assert report.dataset.schema_only
    assert _status(report)["orders.shipping.city.type"] == "passed"
    assert _status(report)["orders.order_id.required"] == "skipped"


def test_a_parquet_footer_with_a_bad_type_fails_the_type_check(tmp_path: Path) -> None:
    path = tmp_path / "orders.parquet"
    pq.write_table(pa.table({"order_id": [1, 2], "quantity": ["1", "2"]}), path)

    report = run_test(contract_doc(), path, options=SCHEMA_ONLY)

    assert _status(report)["orders.quantity.type"] == "failed"
    assert report.exit_code() == 1


def test_a_broken_parquet_footer_is_a_load_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.parquet"
    path.write_bytes(b"not parquet")

    with pytest.raises(DataLoadError) as raised:
        schema_of(path)

    assert "broken.parquet" in str(raised.value)


def _spy_on_frame_rows(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    original = schema_only_module.frame_to_arrow

    def spy(obj: object) -> Any:
        table, text = original(obj)
        seen.append(table.num_rows)
        return table, text

    monkeypatch.setattr(schema_only_module, "frame_to_arrow", spy)
    return seen


def test_a_pandas_frame_is_typed_from_at_most_a_thousand_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_on_frame_rows(monkeypatch)
    frame = pd.DataFrame({"quantity": range(5000), "order_id": ["a"] * 5000})

    schema = schema_of(frame)

    assert seen == [schema_only_module.FRAME_SCHEMA_ROWS]
    assert schema.field("quantity").type == pa.int64()
    assert is_string(schema.field("order_id").type)


def test_a_polars_frame_is_typed_from_at_most_a_thousand_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_on_frame_rows(monkeypatch)
    frame = pl.DataFrame({"quantity": list(range(3000))})

    report = run_test(contract_doc(), frame, options=SCHEMA_ONLY)

    assert seen == [schema_only_module.FRAME_SCHEMA_ROWS]
    assert report.dataset.kind == "polars"
    assert _status(report)["orders.quantity.type"] == "passed"


def test_a_polars_lazy_frame_is_refused_with_a_hint() -> None:
    with pytest.raises(UnsupportedDatasetError) as raised:
        schema_of(pl.DataFrame({"a": [1]}).lazy())

    assert "collect" in (raised.value.hint or "")


def test_table_batch_reader_and_stream_give_their_schema_without_rows() -> None:
    table = arrow_table(orders_rows(good=True))
    batch = table.to_batches()[0]
    reader = pa.RecordBatchReader.from_batches(table.schema, iter(()))

    class Stream:
        def __arrow_c_stream__(self, requested_schema: Any = None) -> Any:
            return table.__arrow_c_stream__(requested_schema)

    assert schema_of(table) == table.schema
    assert schema_of(batch) == table.schema
    assert schema_of(reader) == table.schema
    assert schema_of(Stream()) == table.schema


def test_an_unreadable_arrow_stream_is_a_load_error() -> None:
    class Broken:
        def __arrow_c_stream__(self, requested_schema: Any = None) -> Any:
            raise ValueError("boom with secret-value")

    with pytest.raises(DataLoadError) as raised:
        schema_of(Broken())

    assert "secret-value" not in str(raised.value)


def test_an_object_that_is_no_dataset_is_refused() -> None:
    with pytest.raises(UnsupportedDatasetError):
        schema_of(object())


def test_a_format_makes_no_sense_for_an_in_memory_dataset() -> None:
    with pytest.raises(UsageError):
        schema_of(ARROW_SCHEMA, format="parquet")


@pytest.mark.parametrize("suffix", ["csv", "json", "ndjson"])
def test_text_files_have_no_schema_without_their_rows(tmp_path: Path, suffix: str) -> None:
    path = write_csv(tmp_path / f"orders.{suffix}", orders_rows(flat=True))

    with pytest.raises(UnsupportedDatasetError) as raised:
        run_test(contract_doc(), path, options=SCHEMA_ONLY)

    assert "Parquet" in (raised.value.hint or "")


def test_load_dataset_returns_an_empty_table_with_the_schema(tmp_path: Path) -> None:
    path = tmp_path / "orders.parquet"
    pq.write_table(arrow_table(orders_rows(good=True)), path)

    from_path = load_dataset(path, schema_only=True)
    from_object = load_dataset(ARROW_SCHEMA, schema_only=True)

    assert from_path.schema_only and from_object.schema_only
    assert from_path.table.num_rows == 0
    assert from_path.schema.names == ARROW_SCHEMA.names[: len(from_path.schema.names)]
    assert from_object.kind == "arrow"
    assert from_object.location == "pyarrow.Schema"
    assert from_object.rows_total is None


def test_a_delta_table_gives_its_schema_from_the_log(tmp_path: Path) -> None:
    deltalake = pytest.importorskip("deltalake")
    path = tmp_path / "table"
    deltalake.write_deltalake(str(path), arrow_table(orders_rows(good=True)))

    report = run_test(contract_doc(), path, options=SCHEMA_ONLY)

    assert report.dataset.kind == "delta"
    assert _status(report)["orders.quantity.type"] == "passed"
    assert report.dataset.rows_tested == 0


def test_a_missing_delta_extra_names_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "table"
    (path / "_delta_log").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "deltalake", None)

    with pytest.raises(ExtraNotInstalledError) as raised:
        schema_of(path)

    assert raised.value.extra == "delta"


def test_a_broken_delta_table_is_a_load_error(tmp_path: Path) -> None:
    pytest.importorskip("deltalake")
    path = tmp_path / "broken"
    (path / "_delta_log").mkdir(parents=True)

    with pytest.raises(DataLoadError):
        schema_of(path)


def test_a_nesting_cut_off_keeps_its_own_reason_in_a_schema_only_run() -> None:
    leaf: dict[str, Any] = {"name": "leaf", "logicalType": "string"}
    for level in range(12):
        leaf = {"name": f"n{level}", "logicalType": "object", "properties": [leaf]}
    document = {"schema": [{"name": "t", "properties": [leaf]}]}

    report = run_test(document, pa.schema([pa.field("n11", pa.struct([]))]), options=SCHEMA_ONLY)
    reasons = {check.skipped_reason for check in report.checks if check.status == "skipped"}

    assert any("nesting deeper" in (reason or "") for reason in reasons)
