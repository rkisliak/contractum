"""SQL DDL schemas: parsed with sqlglot, typed with the vendor map, never executed."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from support.odcs import contract_doc

from contractum.errors import (
    DataLoadError,
    ExtraNotInstalledError,
    UnsupportedDatasetError,
    UsageError,
)
from contractum.sources import ddl as ddl_module
from contractum.sources import load_dataset, schema_of
from contractum.sources.ddl import ddl_to_schema
from contractum.testing import TestOptions, run_test

_ORDERS = (
    "CREATE TABLE orders ("
    "order_id VARCHAR(20) NOT NULL, amount DECIMAL(12,2), quantity INT, "
    "created_at TIMESTAMP, ok BOOLEAN)"
)

_EXPECTED = {
    "order_id": pa.string(),
    "amount": pa.decimal128(12, 2),
    "quantity": pa.int32(),
    "ok": pa.bool_(),
}


@pytest.mark.parametrize("dialect", ["postgres", "snowflake", "bigquery", "duckdb"])
def test_a_create_table_maps_to_arrow_in_every_dialect(dialect: str) -> None:
    schema = ddl_to_schema(_ORDERS, dialect=dialect)

    assert schema.names == ["order_id", "amount", "quantity", "created_at", "ok"]
    for name, expected in _EXPECTED.items():
        assert schema.field(name).type == expected
    assert pa.types.is_timestamp(schema.field("created_at").type)
    assert schema.field("created_at").type.unit == "us"


def test_not_null_and_primary_key_make_a_field_required() -> None:
    schema = ddl_to_schema("CREATE TABLE t (a INT NOT NULL, b INT NULL, c INT, d INT PRIMARY KEY)")

    assert [field.nullable for field in schema] == [False, True, True, False]


@pytest.mark.parametrize(
    ("column_type", "dialect", "expected"),
    [
        ("TIMESTAMP", "duckdb", pa.timestamp("us")),
        ("TIMESTAMPTZ", "postgres", pa.timestamp("us", tz="UTC")),
        ("TIMESTAMP", "bigquery", pa.timestamp("us", tz="UTC")),
        ("TIMESTAMP_NTZ", "snowflake", pa.timestamp("us")),
        ("TIMESTAMP_LTZ", "snowflake", pa.timestamp("us", tz="UTC")),
        ("DOUBLE PRECISION", "postgres", pa.float64()),
        ("REAL", "postgres", pa.float32()),
        ("FLOAT", "snowflake", pa.float64()),
        ("FLOAT8", "postgres", pa.float64()),
        ("BIGINT", "postgres", pa.int64()),
        ("TEXT", "postgres", pa.string()),
        ("BYTEA", "postgres", pa.binary()),
        ("DATE", "snowflake", pa.date32()),
        ("NUMBER(10,2)", "snowflake", pa.decimal128(10, 2)),
    ],
)
def test_dialect_specific_types_use_the_shared_vendor_map(
    column_type: str, dialect: str, expected: pa.DataType
) -> None:
    schema = ddl_to_schema(f"CREATE TABLE t (c {column_type})", dialect=dialect)

    assert schema.field("c").type == expected


def test_schema_qualified_names_and_if_not_exists_are_read() -> None:
    schema = ddl_to_schema("CREATE TABLE IF NOT EXISTS shop.public.orders (id INT)")

    assert schema.names == ["id"]


_SEVERAL = (
    "CREATE TABLE customers (id INT, name TEXT);\n"
    "CREATE TABLE shop.orders (order_id TEXT);\n"
    "CREATE TABLE archive.orders (old_id TEXT);\n"
    "CREATE VIEW v AS SELECT 1;\n"
)


def test_the_table_matching_the_contract_object_is_chosen() -> None:
    assert ddl_to_schema(_SEVERAL, table="CUSTOMERS").names == ["id", "name"]
    assert ddl_to_schema(_SEVERAL, table=["nothing", "archive.orders"]).names == ["old_id"]


def test_several_tables_without_a_name_list_the_tables() -> None:
    with pytest.raises(UsageError) as raised:
        ddl_to_schema(_SEVERAL)

    assert "customers" in str(raised.value)
    assert "shop.orders" in str(raised.value)
    assert "--object" in (raised.value.hint or "")


def test_a_name_without_a_table_lists_the_tables() -> None:
    with pytest.raises(UsageError) as raised:
        ddl_to_schema(_SEVERAL, table="payments")

    assert "no table for the contract object" in str(raised.value)
    assert "customers" in str(raised.value)


def test_a_name_matching_two_tables_is_ambiguous() -> None:
    with pytest.raises(UsageError) as raised:
        ddl_to_schema(_SEVERAL, table="orders")

    assert "several tables" in str(raised.value)


def test_a_single_table_is_taken_whatever_its_name() -> None:
    assert ddl_to_schema("CREATE TABLE other (a INT)", table="orders").names == ["a"]


@pytest.mark.parametrize(
    ("statement", "column", "shown"),
    [
        ("CREATE TABLE t (id INT, place GEOGRAPHY)", "place", "GEOGRAPHY"),
        ("CREATE TABLE t (tags ARRAY<INT>)", "tags", "ARRAY"),
        ("CREATE TABLE t (id INT, loose)", "loose", "(no type)"),
    ],
)
def test_an_unmapped_column_type_names_the_column_and_the_type(
    statement: str, column: str, shown: str
) -> None:
    with pytest.raises(UnsupportedDatasetError) as raised:
        ddl_to_schema(statement)

    assert repr(column) in str(raised.value)
    assert shown in str(raised.value)
    assert raised.value.exit_code == 2


def test_a_file_without_a_create_table_is_refused() -> None:
    statements = (
        "SELECT 1",
        "CREATE VIEW v AS SELECT 1",
        "CREATE TABLE t AS SELECT 1",
        "CREATE TABLE t (PRIMARY KEY (a))",
        "CREATE INDEX i ON t (a)",
        "",
    )
    for text in statements:
        with pytest.raises(UnsupportedDatasetError):
            ddl_to_schema(text)


def test_broken_sql_and_an_unknown_dialect_are_reported_without_the_text() -> None:
    with pytest.raises(DataLoadError) as parse:
        ddl_to_schema("CREATE TABLE secret_table (")
    with pytest.raises(UsageError) as dialect:
        ddl_to_schema(_ORDERS, dialect="nosuchsql")

    assert "secret_table" not in str(parse.value)
    assert "nosuchsql" in str(dialect.value)


def test_other_statements_are_parsed_but_never_run() -> None:
    sql = (
        "DROP TABLE orders; INSERT INTO orders VALUES (1); "
        "CREATE TABLE orders (a INT); DELETE FROM orders"
    )

    assert ddl_to_schema(sql).names == ["a"]


def test_the_module_has_no_way_to_execute_sql() -> None:
    tree = ast.parse(Path(ddl_module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [])
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert imported.isdisjoint({"sqlite3", "duckdb", "subprocess", "os", "sqlalchemy"})
    assert "parse" in called
    assert called.isdisjoint({"execute", "executescript", "run", "system"})


def test_without_sqlglot_the_error_names_the_sql_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sqlglot", None)

    with pytest.raises(ExtraNotInstalledError) as raised:
        ddl_to_schema(_ORDERS)

    assert raised.value.extra == "sql"
    assert "contractum[sql]" in str(raised.value)


def _write(tmp_path: Path, text: str, name: str = "orders.sql") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_sql_file_is_a_schema_only_dataset(tmp_path: Path) -> None:
    path = _write(tmp_path, _ORDERS)

    loaded = load_dataset(path)

    assert loaded.schema_only
    assert loaded.kind == "ddl"
    assert loaded.table.num_rows == 0
    assert loaded.schema.field("quantity").type == pa.int32()
    assert schema_of(path, format="ddl").names == loaded.schema.names


def test_run_test_checks_a_sql_file_against_the_contract_without_the_flag(
    tmp_path: Path,
) -> None:
    document = contract_doc()
    document["schema"][0]["properties"] = [
        prop
        for prop in document["schema"][0]["properties"]
        if prop["name"] in ("order_id", "amount", "quantity")
    ]
    path = _write(tmp_path, _ORDERS)

    report = run_test(document, path, options=TestOptions(dialect="postgres"))
    status: dict[str, str] = {check.id: check.status for check in report.checks}

    assert report.dataset.schema_only
    assert report.dataset.kind == "ddl"
    assert status["orders.order_id.type"] == "passed"
    assert status["orders.amount.type"] == "passed"
    assert status["orders.quantity.type"] == "passed"
    assert status["orders.extra_columns"] == "warning"
    assert status["orders.order_id.required"] == "skipped"


def test_run_test_catches_a_type_the_contract_does_not_allow(tmp_path: Path) -> None:
    document = contract_doc()
    document["schema"][0]["properties"][3]["physicalType"] = "BIGINT"
    path = _write(tmp_path, "CREATE TABLE orders (quantity INT)", "x.ddl")

    report = run_test(document, path, options=TestOptions(schema_only=True))
    status: dict[str, Any] = {check.id: check.status for check in report.checks}

    assert status["orders.quantity.type"] == "passed"
    assert status["orders.quantity.physical_type"] == "failed"
    assert not report.ok


def test_the_object_picks_the_table_of_a_multi_table_file(tmp_path: Path) -> None:
    document = contract_doc()
    path = _write(
        tmp_path,
        "CREATE TABLE customers (id INT); CREATE TABLE orders (order_id TEXT, quantity BIGINT);",
    )

    report = run_test(document, path)
    status: dict[str, str] = {check.id: check.status for check in report.checks}

    assert status["orders.order_id.present"] == "passed"
    assert status["orders.quantity.type"] == "passed"


def test_an_unreadable_sql_file_is_a_load_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.sql"
    path.write_bytes(b"\xff\xfe\x00 not utf-8 \xc3\x28")

    with pytest.raises(DataLoadError):
        schema_of(path)
