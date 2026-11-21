"""Deterministic datasets for the fixture ``orders`` object in every format (tests only)."""

from __future__ import annotations

import copy
import csv
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from support.odcs import load_fixture_contract

_CREATED = datetime(2026, 11, 21, 9, 30, tzinfo=UTC)
_DAY = date(2026, 11, 21)

SCALAR_COLUMNS = (
    "order_id",
    "customer_email",
    "amount",
    "quantity",
    "status",
    "created_at",
    "order_date",
)

ARROW_SCHEMA = pa.schema(
    [
        pa.field("order_id", pa.string()),
        pa.field("customer_email", pa.string()),
        pa.field("amount", pa.float64()),
        pa.field("quantity", pa.int64()),
        pa.field("status", pa.string()),
        pa.field("created_at", pa.timestamp("us", tz="UTC")),
        pa.field("order_date", pa.date32()),
        pa.field("tags", pa.list_(pa.string())),
        pa.field(
            "shipping", pa.struct([pa.field("city", pa.string()), pa.field("zip", pa.string())])
        ),
    ]
)


def _row(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "order_id": "ORD-000001",
        "customer_email": "a@example.com",
        "amount": 10.5,
        "quantity": 1,
        "status": "new",
        "created_at": _CREATED,
        "order_date": _DAY,
        "tags": ["a"],
        "shipping": {"city": "Oslo", "zip": "01234"},
    }
    base.update(fields)
    return base


def orders_rows(good: bool = True, flat: bool = False) -> list[dict[str, Any]]:
    """Rows of ``orders``: three conforming ones, or four that break the contract.

    The bad rows have a missing id, a duplicate id, a malformed id, a bad e-mail, a negative
    amount, an amount that is not a multiple of 0.01, a quantity out of range and an unknown
    status. ``flat`` drops the array and object columns that CSV cannot hold.
    """
    if good:
        rows = [
            _row(),
            _row(order_id="ORD-000002", amount=20.0, quantity=2, status="paid", tags=[]),
            _row(order_id="ORD-000003", amount=0.99, quantity=3, status="shipped", tags=["b", "c"]),
        ]
    else:
        rows = [
            _row(),
            _row(order_id=None, customer_email="not-an-email", amount=-5.0, quantity=0),
            _row(order_id="BAD-1", amount=12.345, quantity=1000, status="unknown"),
            _row(shipping={"city": None, "zip": "ABCDE"}),
        ]
    if flat:
        for row in rows:
            row.pop("tags")
            row.pop("shipping")
    return rows


def flat_orders_contract() -> dict[str, Any]:
    """The v3.2.0 fixture contract without the array and object properties."""
    document = copy.deepcopy(load_fixture_contract("3.2.0"))
    properties = document["schema"][0]["properties"]
    document["schema"][0]["properties"] = [
        prop for prop in properties if prop["name"] in SCALAR_COLUMNS
    ]
    return document


def arrow_table(rows: list[dict[str, Any]]) -> pa.Table:
    """The rows as an Arrow table with the types the contract describes."""
    schema = pa.schema([field for field in ARROW_SCHEMA if field.name in rows[0]])
    return pa.Table.from_pylist(rows, schema=schema)


def _text(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def _jsonable(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def write_csv(path: Path, rows: list[dict[str, Any]], delimiter: str = ",") -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter=delimiter)
        writer.writeheader()
        writer.writerows([{key: _text(value) for key, value in row.items()} for row in rows])
    return path


def write_json(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps(rows, default=_jsonable), encoding="utf-8")
    return path


def write_ndjson(path: Path, rows: list[dict[str, Any]]) -> Path:
    lines = [json.dumps(row, default=_jsonable) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_delta(path: Path, rows: list[dict[str, Any]]) -> Path:
    from deltalake import write_deltalake

    write_deltalake(str(path), arrow_table(rows))
    return path


def write_all_formats(tmp_path: Path, rows: list[dict[str, Any]]) -> dict[str, Path]:
    """Write the rows as Parquet, CSV, TSV, JSON array and NDJSON files; returns them by format."""
    pq.write_table(arrow_table(rows), tmp_path / "orders.parquet")
    return {
        "parquet": tmp_path / "orders.parquet",
        "csv": write_csv(tmp_path / "orders.csv", rows),
        "tsv": write_csv(tmp_path / "orders.tsv", rows, delimiter="\t"),
        "json": write_json(tmp_path / "orders.json", rows),
        "ndjson": write_ndjson(tmp_path / "orders.ndjson", rows),
    }
