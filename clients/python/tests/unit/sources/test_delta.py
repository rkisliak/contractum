"""Delta Lake tables: projection, limit and the optional extra."""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pytest

from contractum.errors import DataLoadError, ExtraNotInstalledError
from contractum.sources import load_dataset
from contractum.sources.delta import is_delta_table, read_delta

deltalake = pytest.importorskip("deltalake")


def _delta(tmp_path: Path, rows: int = 10) -> Path:
    path = tmp_path / "table"
    table = pa.table(
        {"id": list(range(rows)), "name": [f"n{n}" for n in range(rows)], "Extra": [1] * rows}
    )
    deltalake.write_deltalake(str(path), table)
    return path


def test_a_delta_directory_is_recognised_by_its_log(tmp_path: Path) -> None:
    path = _delta(tmp_path)

    assert is_delta_table(path)
    assert not is_delta_table(tmp_path)
    assert load_dataset(path).kind == "delta"


def test_delta_reads_the_wanted_columns_and_knows_the_total(tmp_path: Path) -> None:
    result = read_delta(_delta(tmp_path), columns=["id", "missing"])

    assert result.table.column_names == ["id"]
    assert result.schema.names == ["id", "name", "Extra"]
    assert result.table.num_rows == 10
    assert result.rows_total == 10


def test_delta_limit_returns_the_first_rows(tmp_path: Path) -> None:
    result = read_delta(_delta(tmp_path), limit=4)

    assert result.table.num_rows == 4
    assert result.rows_total == 10


def test_delta_projection_can_ignore_case(tmp_path: Path) -> None:
    path = _delta(tmp_path)

    assert read_delta(path, columns=["extra"]).table.column_names == []
    assert read_delta(path, columns=["extra"], ignore_case=True).table.column_names == ["Extra"]


def test_a_broken_delta_table_is_a_load_error_with_the_path(tmp_path: Path) -> None:
    path = tmp_path / "broken"
    (path / "_delta_log").mkdir(parents=True)

    with pytest.raises(DataLoadError) as raised:
        read_delta(path)

    assert str(path) in str(raised.value)


def test_a_missing_deltalake_names_the_delta_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _delta(tmp_path)
    monkeypatch.setitem(sys.modules, "deltalake", None)

    with pytest.raises(ExtraNotInstalledError) as raised:
        load_dataset(path)

    assert "contractum[delta]" in str(raised.value)
    assert raised.value.extra == "delta"
