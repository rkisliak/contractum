"""A fake server with the ``payments.orders`` contract in three versions (tests only)."""

from __future__ import annotations

from typing import Any

import yaml
from support.fake_server import FakeContractum, FakeVersion
from support.odcs import contract_doc

BASE = "https://contractum.example"


def contract_bytes(version: str, **overrides: Any) -> bytes:
    """The fixture contract at ``version``; the range of ``quantity`` is capped by 3 when asked."""
    document = contract_doc(version=version, **overrides)
    return yaml.safe_dump(document, sort_keys=False).encode("utf-8")


def preview_bytes() -> bytes:
    """Version 2.0.0: quantity must now be below 3, which data valid for 1.1.0 breaks."""
    document = contract_doc(version="2.0.0")
    for prop in document["schema"][0]["properties"]:
        if prop["name"] == "quantity":
            prop["logicalTypeOptions"] = {"minimum": 1, "exclusiveMaximum": 3}
    return yaml.safe_dump(document, sort_keys=False).encode("utf-8")


def orders_server(**latest: Any) -> FakeContractum:
    """Latest 1.1.0, Preview 2.0.0 and a deprecated older 1.0.0 of ``payments.orders``."""
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [
            FakeVersion("1.1.0", contract_bytes("1.1.0"), status="latest", **latest),
            FakeVersion("2.0.0", preview_bytes(), status="preview"),
            FakeVersion(
                "1.0.0",
                contract_bytes("1.0.0"),
                status="older",
                deprecated=True,
                sunset_date="2027-01-31",
            ),
        ],
        name="Orders",
    )
    return server
