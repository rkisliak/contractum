"""A pinned project on disk for the pytest plugin tests: contract file, lock and datasets."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
import yaml
from support.datasets import arrow_table, orders_rows
from support.fake_server import FakeContractum, FakeVersion, sha256
from support.odcs import contract_doc

from contractum.lock import LockEntry, LockFile, render_lock

SERVER = "https://contractum.example"
CONTRACT_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "pytest-project/payments.orders"))


@dataclass(frozen=True)
class Project:
    """Paths of a project pinned to ``payments.orders`` 1.0.0."""

    root: Path
    lock: Path
    contract_file: Path
    good: Path
    bad: Path


def contract_content() -> bytes:
    """The bytes of ``payments.orders`` 1.0.0 as pulled and as served."""
    return yaml.safe_dump(contract_doc(), sort_keys=False).encode("utf-8")


def project_server(*, deprecated: bool = False) -> FakeContractum:
    """A fake server holding the pinned version as Latest, with the project's contract id."""
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [
            FakeVersion(
                "1.0.0",
                contract_content(),
                status="latest",
                deprecated=deprecated,
                sunset_date="2027-01-31" if deprecated else None,
            )
        ],
        contract_id=CONTRACT_ID,
    )
    return server


def write_project(root: Path, *, tamper: bool = False) -> Project:
    """Write ``contractum.lock``, the pulled file and a good and a bad Parquet dataset.

    ``tamper`` changes the pulled file after the lock recorded its hash.
    """
    root.mkdir(parents=True, exist_ok=True)
    content = contract_content()
    pulled = root / "contracts" / "payments.orders-1.0.0.yaml"
    pulled.parent.mkdir(exist_ok=True)
    pulled.write_bytes(content + b"# tampered\n" if tamper else content)
    entry = LockEntry(
        ref="payments.orders",
        requested="latest",
        contract_id=uuid.UUID(CONTRACT_ID),
        odcs_id="payments.orders",
        semver="1.0.0",
        content_sha256=sha256(content),
        media_type="application/yaml",
        file="contracts/payments.orders-1.0.0.yaml",
    )
    lock = root / "contractum.lock"
    lock.write_bytes(render_lock(LockFile(server=SERVER, contracts=[entry])))
    good, bad = root / "good.parquet", root / "bad.parquet"
    pq.write_table(arrow_table(orders_rows(good=True)), good)
    pq.write_table(arrow_table(orders_rows(good=False)), bad)
    return Project(root, lock, pulled, good, bad)
