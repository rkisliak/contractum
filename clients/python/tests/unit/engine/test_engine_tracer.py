"""Tracer: an ODCS document goes through plan and ArrowBackend into a typed Report."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.backend import RunOptions
from contractum.engine.plan import PlanOptions, plan
from contractum.odcs import find_object, load_document
from contractum.report.model import ContractInfo, DatasetInfo, Report, ToolInfo, build_report

DOCUMENT = """
apiVersion: v3.2.0
kind: DataContract
id: payments.orders
version: 1.0.0
schema:
  - name: orders
    properties:
      - name: order_id
        logicalType: string
        required: true
      - name: quantity
        logicalType: integer
      - name: amount
        logicalType: number
"""


def _run(table: pa.Table, **plan_options: Any) -> Report:
    obj = find_object(load_document(DOCUMENT))
    the_plan = plan(obj, PlanOptions(**plan_options))
    outcomes = ArrowBackend(table).evaluate(the_plan.checks, RunOptions())
    return build_report(
        tool=ToolInfo(version="0"),
        contract=ContractInfo(source="file", odcs_id="payments.orders", semver="1.0.0"),
        dataset=DatasetInfo(kind="arrow", rows_tested=table.num_rows),
        plan=the_plan,
        outcomes=outcomes,
    )


def test_conforming_table_passes_every_check() -> None:
    table = pa.table({"order_id": ["a", "b"], "quantity": [1, 2], "amount": [1.5, 2]})

    report = _run(table)

    assert [(check.id, check.status) for check in report.checks] == [
        ("orders.order_id.present", "passed"),
        ("orders.order_id.type", "passed"),
        ("orders.order_id.required", "passed"),
        ("orders.quantity.present", "passed"),
        ("orders.quantity.type", "passed"),
        ("orders.amount.present", "passed"),
        ("orders.amount.type", "passed"),
        ("orders.extra_columns", "passed"),
    ]
    assert report.ok


def test_missing_column_and_null_value_fail_with_sample_row() -> None:
    table = pa.table({"order_id": ["a", None, "c"], "amount": [1.0, 2.0, 3.0]})

    report = _run(table)
    by_id = {check.id: check for check in report.checks}

    assert by_id["orders.quantity.present"].status == "failed"
    assert by_id["orders.quantity.type"].status == "skipped"
    required = by_id["orders.order_id.required"]
    assert required.status == "failed"
    assert required.failed_rows == 1
    assert required.total_rows == 3
    assert [(sample.row, sample.value) for sample in required.samples] == [(1, None)]
    assert not report.ok


def test_report_survives_a_json_round_trip() -> None:
    report = _run(pa.table({"order_id": [None], "quantity": ["x"], "amount": [1.0]}))

    again = Report.model_validate_json(report.model_dump_json())

    assert again == report
    assert again.schema_version == "1"
