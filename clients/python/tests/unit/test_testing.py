"""Tracer: run_test on an in-memory frame and on a Parquet file returns a Report."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from support.datasets import arrow_table, orders_rows, write_csv
from support.odcs import contract_doc

from contractum.errors import UsageError
from contractum.odcs import load_document
from contractum.report.model import ContractInfo
from contractum.testing import TestOptions, run_test


def _failing(report: Any) -> set[str]:
    return {check.id for check in report.checks if check.status == "failed"}


def test_a_conforming_pandas_frame_passes_with_exit_zero() -> None:
    frame = pd.DataFrame(orders_rows(good=True))

    report = run_test(contract_doc(), frame)

    assert report.ok
    assert report.exit_code() == 0
    assert report.dataset.kind == "pandas"
    assert report.dataset.rows_tested == 3
    assert report.object == "orders"
    assert report.summary.failed == 0


def test_a_bad_parquet_file_fails_the_named_checks(tmp_path: Path) -> None:
    path = tmp_path / "orders.parquet"
    pq.write_table(arrow_table(orders_rows(good=False)), path)

    report = run_test(contract_doc(), path)

    assert not report.ok
    assert report.exit_code() == 1
    assert {
        "orders.order_id.required",
        "orders.order_id.pattern",
        "orders.primary_key",
        "orders.amount.range",
        "orders.quantity.range",
        "orders.status.enum",
        "orders.shipping.city.required",
    } <= _failing(report)
    assert report.dataset.kind == "parquet"
    assert report.dataset.location == str(path)
    assert report.dataset.rows_total == 4


def test_only_referenced_columns_are_read_from_parquet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = orders_rows(good=True)
    table = arrow_table(rows).append_column("unused", arrow_table(rows)["quantity"])
    path = tmp_path / "wide.parquet"
    pq.write_table(table, path)
    seen: list[list[str]] = []
    original = pq.ParquetFile.iter_batches

    def spy(self: pq.ParquetFile, *args: Any, **kwargs: Any) -> Any:
        seen.append(list(kwargs["columns"]))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", spy)

    report = run_test(contract_doc(), path, options=TestOptions(extra="allow"))

    assert report.ok
    assert len(seen) == 1
    assert "unused" not in seen[0]
    assert set(seen[0]) == {
        "order_id",
        "customer_email",
        "amount",
        "quantity",
        "status",
        "created_at",
        "order_date",
        "tags",
        "shipping",
    }


def test_an_unread_extra_column_is_still_reported_as_extra(tmp_path: Path) -> None:
    rows = orders_rows(good=True)
    table = arrow_table(rows).append_column("unused", arrow_table(rows)["quantity"])
    path = tmp_path / "wide.parquet"
    pq.write_table(table, path)

    warned = run_test(contract_doc(), path)
    failed = run_test(contract_doc(), path, options=TestOptions(extra="fail"))

    by_id = {check.id: check for check in warned.checks}
    assert by_id["orders.extra_columns"].status == "warning"
    assert "unused" in (by_id["orders.extra_columns"].message or "")
    assert "orders.extra_columns" in _failing(failed)


def test_importing_the_testing_module_does_not_import_pandas_or_polars() -> None:
    code = (
        "import contractum.testing, sys; "
        "assert not {'pandas', 'polars', 'deltalake'} & set(sys.modules)"
    )

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)  # noqa: S603

    assert result.returncode == 0, result.stderr.decode()


# --- contract inputs -------------------------------------------------------------------------

_YAML = (
    "apiVersion: v3.2.0\nkind: DataContract\nid: shop.items\nname: Items\nversion: 2.1.0\n"
    "status: active\nschema:\n  - name: items\n    properties:\n      - name: sku\n"
    "        logicalType: string\n        required: true\n"
)


class _Version:
    """What run_test needs of a fetched ContractVersion: ``document`` and ``info``."""

    def __init__(self, document: dict[str, Any], info: ContractInfo) -> None:
        self.document = document
        self.info = info


def _items() -> pa.Table:
    return pa.table({"sku": ["a", "b"]})


def test_the_contract_can_be_a_mapping() -> None:
    report = run_test(contract_doc(), pd.DataFrame(orders_rows()))

    assert report.contract.source == "file"
    assert report.contract.odcs_id == "payments.orders"
    assert report.contract.semver == "1.0.0"
    assert report.contract.content_sha256 is None


@pytest.mark.parametrize("source", ["text", "bytes", "path", "path_str", "pathlike_json"])
def test_the_contract_can_be_text_bytes_or_a_file(source: str, tmp_path: Path) -> None:
    path = tmp_path / "items.yaml"
    path.write_text(_YAML, encoding="utf-8")
    contract: Any = {
        "text": _YAML,
        "bytes": _YAML.encode("utf-8"),
        "path": path,
        "path_str": str(path),
        "pathlike_json": _json_file(tmp_path),
    }[source]

    report = run_test(contract, _items())

    assert report.ok
    assert report.object == "items"
    assert report.contract.odcs_id == "shop.items"
    assert report.contract.name == "Items"
    assert report.contract.status == "active"
    assert report.contract.content_sha256 is not None
    if source != "pathlike_json":
        assert report.contract.content_sha256 == hashlib.sha256(_YAML.encode()).hexdigest()


def _json_file(tmp_path: Path) -> Path:
    path = tmp_path / "items.json"
    path.write_text(json.dumps(load_document(_YAML)), encoding="utf-8")
    return path


def test_a_fetched_version_supplies_its_own_info() -> None:
    info = ContractInfo(source="server", odcs_id="shop.items", semver="9.9.9", contract_id="c1")

    report = run_test(_Version(load_document(_YAML), info), _items())

    assert report.contract == info


def test_contract_info_overrides_what_the_contract_says() -> None:
    info = ContractInfo(source="lock", odcs_id="locked.id", semver="3.0.0")

    from_text = run_test(_YAML, _items(), contract_info=info)
    from_version = run_test(
        _Version(load_document(_YAML), ContractInfo(source="server")), _items(), contract_info=info
    )
    from_mapping = run_test(load_document(_YAML), _items(), contract_info=info)

    assert from_text.contract == from_version.contract == from_mapping.contract == info


def test_a_contract_of_an_unknown_type_is_a_usage_error() -> None:
    with pytest.raises(UsageError):
        run_test(42, _items())


def test_an_unreadable_contract_file_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError) as raised:
        run_test(tmp_path / "gone.yaml", _items())

    assert "gone.yaml" in str(raised.value)


def test_a_string_that_is_neither_a_file_nor_a_contract_is_a_usage_error() -> None:
    with pytest.raises(UsageError):
        run_test("no-such-file.yaml", _items())


def test_the_object_option_picks_one_schema_object() -> None:
    document = contract_doc()
    document["schema"].append(
        {"name": "lines", "properties": [{"name": "n", "logicalType": "integer"}]}
    )

    with pytest.raises(UsageError) as several:
        run_test(document, _items())
    report = run_test(document, pa.table({"n": [1, 2]}), options=TestOptions(object_name="lines"))

    assert "--object" in (several.value.hint or "")
    assert report.object == "lines"
    assert report.ok


# --- options ---------------------------------------------------------------------------------


def _numbers(rows: int = 20) -> tuple[dict[str, Any], pa.Table]:
    contract = contract_doc(
        schema=[
            {"name": "t", "properties": [{"name": "n", "logicalType": "integer", "required": True}]}
        ]
    )
    return contract, pa.table({"n": list(range(rows))})


def test_limit_tests_only_the_first_rows() -> None:
    contract, table = _numbers()

    report = run_test(contract, table, options=TestOptions(limit=5))

    assert report.dataset.rows_tested == 5
    assert report.dataset.rows_total == 20
    assert report.dataset.limit == 5
    assert not report.dataset.sampled


def test_sample_is_seeded_and_reported() -> None:
    contract, table = _numbers()
    options = TestOptions(sample=0.25, seed=3)

    first = run_test(contract, table, options=options)
    second = run_test(contract, table, options=options)

    assert first.dataset.sampled
    assert first.dataset.rows_tested == 5
    assert first.dataset.rows_total == 20
    assert {c.id: c.sampled for c in first.checks}["t.n.required"] is True
    assert [(c.id, c.status) for c in first.checks] == [(c.id, c.status) for c in second.checks]


def test_a_csv_limit_without_a_known_total_reports_none(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    write_csv(path, [{"n": str(n)} for n in range(50_000)])
    contract, _ = _numbers()

    report = run_test(contract, path, options=TestOptions(limit=3))

    assert report.dataset.rows_tested == 3
    assert report.dataset.rows_total is None


def test_schema_only_runs_only_the_schema_checks() -> None:
    contract, table = _numbers()

    report = run_test(contract, table, options=TestOptions(schema_only=True))

    assert {c.category for c in report.checks} == {"schema"}
    assert report.dataset.schema_only


def test_ignore_case_matches_column_names_case_insensitively(tmp_path: Path) -> None:
    contract, _ = _numbers()
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"N": [1, 2]}), path)

    strict = run_test(contract, path)
    folded = run_test(contract, path, options=TestOptions(ignore_case=True))

    assert not strict.ok
    assert folded.ok


def test_failure_samples_can_be_switched_off_or_capped() -> None:
    contract = contract_doc(
        schema=[
            {"name": "t", "properties": [{"name": "n", "logicalType": "integer", "required": True}]}
        ]
    )
    table = pa.table({"n": pa.array([None] * 10, pa.int64())})

    capped = run_test(contract, table, options=TestOptions(max_failures=2))
    none = run_test(contract, table, options=TestOptions(samples=False))

    assert len({c.id: c.samples for c in capped.checks}["t.n.required"]) == 2
    assert {c.id: c.samples for c in none.checks}["t.n.required"] == ()


# --- lifecycle -------------------------------------------------------------------------------

_DEPRECATED = ContractInfo(
    source="server", odcs_id="shop.items", deprecated=True, sunset_date="2026-12-01"
)


def test_a_live_version_has_no_lifecycle_check() -> None:
    report = run_test(_YAML, _items())

    assert "contract.lifecycle" not in {c.id for c in report.checks}


def test_a_deprecated_version_warns_without_failing() -> None:
    report = run_test(_YAML, _items(), contract_info=_DEPRECATED, today=date(2026, 6, 1))

    check = {c.id: c for c in report.checks}["contract.lifecycle"]
    assert check.status == "warning"
    assert report.ok
    assert report.exit_code() == 0
    assert report.exit_code(strict=True) == 1


def test_fail_on_deprecated_fails_the_run() -> None:
    report = run_test(
        _YAML, _items(), contract_info=_DEPRECATED, options=TestOptions(fail_on_deprecated=True)
    )

    assert not report.ok
    assert report.exit_code() == 1
    assert report.checks[-1].id == "contract.lifecycle"


def test_a_near_sunset_fails_only_inside_the_window() -> None:
    near = run_test(
        _YAML,
        _items(),
        contract_info=_DEPRECATED,
        options=TestOptions(sunset_within_days=30),
        today=date(2026, 11, 20),
    )
    far = run_test(
        _YAML,
        _items(),
        contract_info=_DEPRECATED,
        options=TestOptions(sunset_within_days=30),
        today=date(2026, 6, 1),
    )

    assert near.exit_code() == 1
    assert far.exit_code() == 0


def test_run_test_sends_nothing_over_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("run_test opened a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    report = run_test(contract_doc(), pd.DataFrame(orders_rows(good=False)))

    assert not report.ok
