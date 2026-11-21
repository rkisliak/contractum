"""The schema of a SQL DDL file (the optional ``sql`` extra).

A ``CREATE TABLE`` statement is parsed into a syntax tree with sqlglot and read for its column
names, types and NOT NULL markers. The text is never executed: nothing here can reach a database
or run a statement, whatever the file contains. Column types go through the same vendor type map
as a contract's ``physicalType`` (``engine.types.vendor_to_arrow``), so one table decides what
``VARCHAR(20)`` or ``TIMESTAMP_NTZ`` means.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pyarrow as pa

from contractum.engine.types import vendor_to_arrow
from contractum.errors import (
    DataLoadError,
    ExtraNotInstalledError,
    UnsupportedDatasetError,
    UsageError,
)

# sqlglot spells timestamps without an underscore, where the vendor type map follows Snowflake,
# and its FLOAT is the 32-bit type (REAL) in every dialect that has two float widths.
_TYPE_SPELLINGS = {
    "TIMESTAMPNTZ": "TIMESTAMP_NTZ",
    "TIMESTAMPLTZ": "TIMESTAMP_LTZ",
    "FLOAT": "REAL",
}

_NO_TYPE = "(no type)"


def ddl_to_schema(
    sql: str, *, dialect: str | None = None, table: str | Sequence[str] | None = None
) -> pa.Schema:
    """The Arrow schema of one ``CREATE TABLE`` statement in ``sql``.

    ``dialect`` is a sqlglot dialect name (``postgres``, ``snowflake``, ``bigquery``,
    ``duckdb`` ...). With several tables ``table`` (one name or several candidates, matched
    without regard to case, with or without a schema prefix) picks one; otherwise a
    ``UsageError`` lists them. A column type with no Arrow mapping raises an
    ``UnsupportedDatasetError`` that names the column and the type.
    """
    sqlglot, exp = _sqlglot()
    tables = _create_tables(sqlglot, exp, sql, dialect)
    chosen = _choose(tables, table)
    return pa.schema([_field(exp, column, dialect) for column in chosen.columns])


class _Table:
    """A table the DDL creates: its qualified name and its column definitions."""

    def __init__(self, parts: tuple[str, ...], columns: list[Any]) -> None:
        self.parts = parts
        self.columns = columns

    @property
    def name(self) -> str:
        return self.parts[-1]

    @property
    def qualified(self) -> str:
        return ".".join(self.parts)


def _sqlglot() -> tuple[Any, Any]:
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        raise ExtraNotInstalledError("sql", "reading a SQL DDL schema") from None
    return sqlglot, exp


def _create_tables(sqlglot: Any, exp: Any, sql: str, dialect: str | None) -> list[_Table]:
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.SqlglotError as exc:
        raise DataLoadError(
            f"cannot parse the SQL DDL: {type(exc).__name__}",
            hint="check the syntax and pass --dialect for a non-standard dialect",
        ) from None
    except ValueError:
        raise UsageError(f"unknown SQL dialect {dialect!r}") from None
    tables = [table for statement in statements if (table := _table_of(exp, statement)) is not None]
    if not tables:
        raise UnsupportedDatasetError("the SQL DDL has no CREATE TABLE statement with columns")
    return tables


def _table_of(exp: Any, statement: Any) -> _Table | None:
    """The table a CREATE TABLE statement defines; None for any other statement."""
    schema = statement.this if isinstance(statement, exp.Create) else None
    if not isinstance(schema, exp.Schema) or not isinstance(schema.this, exp.Table):
        return None
    target = schema.this
    # A column written without a type is a bare identifier in the tree: keep it, so it is refused.
    columns = [
        item for item in schema.expressions if isinstance(item, (exp.ColumnDef, exp.Identifier))
    ]
    if not columns:
        return None
    parts = tuple(part.name for part in target.parts)
    return _Table(parts, columns)


def _choose(tables: list[_Table], wanted: str | Sequence[str] | None) -> _Table:
    if len(tables) == 1:
        return tables[0]
    names = ", ".join(table.qualified for table in tables)
    candidates = _candidates(wanted)
    if not candidates:
        raise UsageError(
            f"the SQL DDL creates several tables ({names})", hint="choose one with --object NAME"
        )
    matches = [
        table
        for table in tables
        if table.name.lower() in candidates or table.qualified.lower() in candidates
    ]
    if len(matches) == 1:
        return matches[0]
    problem = "matches several tables" if matches else "has no table for the contract object"
    raise UsageError(
        f"the SQL DDL {problem} (tables: {names})", hint="choose one with --object NAME"
    )


def _candidates(wanted: str | Sequence[str] | None) -> frozenset[str]:
    if wanted is None:
        return frozenset()
    names = [wanted] if isinstance(wanted, str) else wanted
    return frozenset(name.lower() for name in names if name)


def _field(exp: Any, column: Any, dialect: str | None) -> pa.Field:
    kind = column.args.get("kind")
    arrow = vendor_to_arrow(_type_text(kind)) if kind is not None else None
    if arrow is None:
        shown = kind.sql(dialect=dialect) if kind is not None else _NO_TYPE
        raise UnsupportedDatasetError(
            f"column {column.name!r} has type {shown} that has no Arrow mapping",
            hint="use a mapped type (VARCHAR, INT, DECIMAL, TIMESTAMP ...) or test a data file",
        )
    return pa.field(column.name, arrow, nullable=not _not_null(exp, column))


def _type_text(kind: Any) -> str:
    """The generic spelling of a column type, with sqlglot's timestamp names translated."""
    text: str = kind.sql()
    name, bracket, rest = text.partition("(")
    return _TYPE_SPELLINGS.get(name, name) + bracket + rest


def _not_null(exp: Any, column: Any) -> bool:
    for constraint in column.args.get("constraints") or ():
        kind = constraint.args.get("kind")
        if isinstance(kind, exp.PrimaryKeyColumnConstraint):
            return True
        if isinstance(kind, exp.NotNullColumnConstraint) and not kind.args.get("allow_null"):
            return True
    return False
