"""Builders over the shared ODCS fixture contract (tests only)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import pyarrow as pa

from contractum.odcs import load_document

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contracts"


@cache
def _cached_fixture(version: str) -> dict[str, Any]:
    return load_document((_FIXTURES / f"orders-v{version}.yaml").read_bytes())


def load_fixture_contract(version: str = "3.2.0") -> dict[str, Any]:
    """A fresh copy of the fixture contract for an ODCS version."""
    return copy.deepcopy(_cached_fixture(version))


def contract_doc(**overrides: Any) -> dict[str, Any]:
    """The v3.2.0 fixture contract with top-level keys replaced by the given values."""
    document = load_fixture_contract("3.2.0")
    document.update(overrides)
    return document


def orders_table(good: bool = True) -> pa.Table:
    """Three rows of ``orders``: conforming when ``good``, else with a null id and a bad type."""
    created = [datetime(2026, 11, 21, 9, 30, tzinfo=UTC)] * 3
    table: dict[str, Any] = {
        "order_id": ["ORD-000001", "ORD-000002", "ORD-000003"],
        "customer_email": ["a@example.com", "b@example.com", "c@example.com"],
        "amount": [Decimal("10.50"), Decimal("20.00"), Decimal("0.99")],
        "quantity": [1, 2, 3],
        "status": ["new", "paid", "shipped"],
        "created_at": pa.array(created, pa.timestamp("us", tz="UTC")),
        "order_date": pa.array([datetime(2026, 11, 21).date()] * 3, pa.date32()),
        "tags": [["a"], [], ["b", "c"]],
        "shipping": [{"city": "Oslo", "zip": "01234"}] * 3,
    }
    if not good:
        table["order_id"] = ["ORD-000001", None, "ORD-000003"]
        table["quantity"] = ["1", "2", "3"]
    return pa.table(table)
