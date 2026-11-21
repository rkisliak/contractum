"""Helpers of the real-stack tests: three small Parquet datasets and a CLI subprocess runner."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - the tests spawn this repository's own CLI
import sys
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
CONTRACTS = "/api/v1/contracts/contracts"
_NOTES = ["first", None, "third"]


@dataclass(frozen=True)
class Credentials:
    """A local account's username and password."""

    username: str
    password: str


@dataclass(frozen=True)
class World:
    """Everything the tests read: the contract, its folder path and the credentials."""

    url: str
    ca: Path
    odcs_id: str
    contract_id: str
    folder_path: str
    reader: Credentials
    reader_pat: str
    sa_key: str


def _write(path: Path, order_ids: list[str | None], amounts: list[int]) -> Path:
    table = pa.table(
        {
            "order_id": pa.array(order_ids, pa.string()),
            "amount": pa.array(amounts, pa.int64()),
            "note": pa.array(_NOTES, pa.string()),
        }
    )
    pq.write_table(table, path)
    return path


def good_data(directory: Path) -> Path:
    """Passes 1.0.0, 1.1.0 and 2.0.0."""
    return _write(directory / "good.parquet", ["A1", "A2", "A3"], [10, 20, 30])


def bad_data(directory: Path) -> Path:
    """Breaks every version: one order id is missing."""
    return _write(directory / "bad.parquet", ["A1", None, "A3"], [10, 20, 30])


def preview_breaking_data(directory: Path) -> Path:
    """Passes 1.1.0 (Latest) and breaks only 2.0.0 (Preview): one amount is above 1000."""
    return _write(directory / "preview-breaks.parquet", ["A1", "A2", "A3"], [10, 5000, 30])


def cli_env(**overrides: str | None) -> dict[str, str]:
    """The test process environment plus overrides; a ``None`` value removes the variable."""
    env = dict(os.environ)
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def run_cli(
    *args: str, env: dict[str, str], stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m contractum`` in a subprocess with the current working directory.

    The working directory is inherited and every path argument is absolute, so coverage data
    recorded by the child folds onto ``src/contractum`` through ``[tool.coverage.paths]``.
    """
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "contractum", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
