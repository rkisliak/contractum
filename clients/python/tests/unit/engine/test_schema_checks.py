"""Schema checks end to end: normalisation, physical type, unique, primary key, extra columns."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest
from support.engine import run_properties
from support.odcs import contract_doc, load_fixture_contract, orders_table

from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.backend import RunOptions
from contractum.engine.checks import CheckOutcome, PrimaryKey
from contractum.engine.normalise import normalise
from contractum.engine.plan import PlanOptions, plan
from contractum.odcs import find_object

OPTS = RunOptions()


def _object(properties: list[dict[str, Any]]) -> Any:
    return find_object(contract_doc(schema=[{"name": "t", "properties": properties}]))


def _run(
    properties: list[dict[str, Any]], table: pa.Table, **options: Any
) -> dict[str, CheckOutcome]:
    return run_properties(properties, table, **options)


# --- normalisation ----------------------------------------------------------------------------

_STRING_COLUMNS: dict[str, Callable[[list[str | None]], pa.Array]] = {
    "string": lambda v: pa.array(v, pa.string()),
    "large_string": lambda v: pa.array(v, pa.large_string()),
    "string_view": lambda v: pa.array(v, pa.string_view()),
    "dictionary": lambda v: pa.array(v, pa.string()).dictionary_encode(),
}


@pytest.mark.parametrize("kind", sorted(_STRING_COLUMNS))
def test_every_string_representation_gives_the_same_schema_verdicts(kind: str) -> None:
    properties = [
        {
            "name": "code",
            "logicalType": "string",
            "physicalType": "string",
            "required": True,
            "unique": True,
        }
    ]
    table = pa.table({"code": _STRING_COLUMNS[kind](["a", "b", "a", None])})

    outcomes = _run(properties, table)

    assert outcomes["t.code.type"].status == "passed"
    assert outcomes["t.code.physical_type"].status == "passed"
    assert outcomes["t.code.required"].failed_rows == 1
    unique = outcomes["t.code.unique"]
    assert (unique.status, unique.failed_rows) == ("failed", 2)
    assert outcomes["t.code.type"].observed == str(table.schema.field("code").type)


def test_normalise_casts_views_decodes_dictionaries_and_leaves_the_rest() -> None:
    table = pa.table(
        {
            "s": pa.array(["a"], pa.string_view()),
            "b": pa.array([b"x"], pa.binary_view()),
            "d": pa.array(["a"], pa.string()).dictionary_encode(),
            "l": pa.array([["a"]], pa.list_(pa.string_view())),
            "n": pa.array([1], pa.int32()),
        }
    )

    result = normalise(table)

    assert [str(field.type) for field in result.schema] == [
        "large_string",
        "large_binary",
        "large_string",
        "list<item: large_string>",
        "int32",
    ]
    assert result["l"].to_pylist() == [["a"]]
    assert result["d"].to_pylist() == ["a"]


def test_normalise_returns_the_same_table_when_nothing_changes() -> None:
    table = pa.table({"n": [1, 2], "f": [0.5, 1.5]})

    assert normalise(table) is table


@pytest.mark.parametrize(
    ("list_type", "expected"),
    [
        (pa.large_list(pa.string_view()), "large_list<item: large_string>"),
        (pa.list_(pa.string_view(), 2), "fixed_size_list<item: large_string>[2]"),
    ],
)
def test_normalise_keeps_the_list_flavour(list_type: pa.DataType, expected: str) -> None:
    column = pa.array([["a", "b"]], list_type)

    assert str(normalise(pa.table({"x": column}))["x"].type) == expected


# --- physical type ----------------------------------------------------------------------------


def test_physical_type_int64_fails_an_int32_column() -> None:
    properties = [{"name": "n", "logicalType": "integer", "physicalType": "int64"}]

    wrong = _run(properties, pa.table({"n": pa.array([1], pa.int32())}))["t.n.physical_type"]
    right = _run(properties, pa.table({"n": pa.array([1], pa.int64())}))["t.n.physical_type"]

    assert wrong.status == "failed"
    assert (wrong.observed, wrong.threshold) == ("int32", "int64")
    assert right.status == "passed"


@pytest.mark.parametrize(
    ("physical", "column", "status"),
    [
        ("Int64", pa.array([1], pa.int64()), "passed"),
        ("double", pa.array([1.0], pa.float64()), "passed"),
        ("float", pa.array([1.0], pa.float32()), "passed"),
        ("float", pa.array([1.0], pa.float64()), "failed"),
        ("utf8", pa.array(["a"], pa.large_string()), "passed"),
        ("Utf8", pa.array(["a"], pa.string_view()), "passed"),
        ("large_string", pa.array(["a"], pa.string()), "passed"),
        ("binary", pa.array([b"a"], pa.binary_view()), "passed"),
        ("bool", pa.array([True], pa.bool_()), "passed"),
        ("date32", pa.array([datetime(2026, 1, 1).date()], pa.date32()), "passed"),
        ("date32", pa.array([datetime(2026, 1, 1).date()], pa.date64()), "failed"),
        ("decimal128(10, 2)", pa.array([Decimal("1.00")], pa.decimal128(10, 2)), "passed"),
        ("decimal128(10, 2)", pa.array([Decimal("1.00")], pa.decimal128(12, 2)), "failed"),
        ("timestamp[us]", pa.array([datetime(2026, 1, 1)], pa.timestamp("us")), "passed"),
        ("timestamp[us]", pa.array([datetime(2026, 1, 1)], pa.timestamp("ms")), "failed"),
        (
            "timestamp[us]",
            pa.array([datetime(2026, 1, 1, tzinfo=UTC)], pa.timestamp("us", tz="UTC")),
            "passed",
        ),
        (
            "timestamp[us, tz=UTC]",
            pa.array([datetime(2026, 1, 1)], pa.timestamp("us")),
            "failed",
        ),
    ],
)
def test_physical_type_names_are_compared_with_the_arrow_type(
    physical: str, column: pa.Array, status: str
) -> None:
    properties = [{"name": "c", "physicalType": physical}]

    outcome = _run(properties, pa.table({"c": column}))["t.c.physical_type"]

    assert outcome.status == status


def test_an_unmappable_physical_type_is_skipped_by_the_plan_with_a_reason() -> None:
    the_plan = plan(_object([{"name": "c", "physicalType": "GEOGRAPHY"}]))

    assert [s.check_id for s in the_plan.skipped] == ["t.c.physical_type"]
    assert the_plan.skipped[0].reason == "physicalType 'GEOGRAPHY' has no Arrow mapping"
    assert not any(c.rule == "physical_type" for c in the_plan.checks)


def test_physical_type_of_a_missing_column_is_skipped_and_presence_fails() -> None:
    outcomes = _run([{"name": "c", "physicalType": "int64"}], pa.table({"x": [1]}))

    assert outcomes["t.c.present"].status == "failed"
    assert outcomes["t.c.physical_type"].status == "skipped"


# --- unique -----------------------------------------------------------------------------------


def test_unique_ignores_nulls_and_fails_every_row_of_a_duplicate_group() -> None:
    table = pa.table({"k": pa.array(["a", None, "b", None, "a", "a"])})

    outcome = _run([{"name": "k", "unique": True}], table)["t.k.unique"]

    assert outcome.status == "failed"
    assert (outcome.failed_rows, outcome.total_rows) == (3, 6)
    assert [(s.row, s.value) for s in outcome.samples] == [(0, "a"), (4, "a"), (5, "a")]


def test_unique_passes_when_only_nulls_repeat_and_when_the_table_is_empty() -> None:
    properties = [{"name": "k", "unique": True}]

    nulls = _run(properties, pa.table({"k": pa.array([None, None, 1])}))["t.k.unique"]
    empty = _run(properties, pa.table({"k": pa.array([], pa.int64())}))["t.k.unique"]

    assert nulls.status == "passed"
    assert (empty.status, empty.total_rows) == ("passed", 0)


def test_unique_treats_nan_as_a_missing_value() -> None:
    nan = float("nan")
    table = pa.table({"k": pa.array([nan, nan, 1.0, 2.0])})

    assert _run([{"name": "k", "unique": True}], table)["t.k.unique"].status == "passed"


def test_unique_on_a_nested_column_is_skipped_not_passed() -> None:
    table = pa.table({"k": pa.array([[1], [1]])})

    outcome = _run([{"name": "k", "unique": True}], table)["t.k.unique"]

    assert outcome.status == "skipped"
    assert outcome.skipped_reason is not None


def test_masked_unique_samples_hide_the_values() -> None:
    properties = [{"name": "k", "unique": True, "classification": "restricted"}]

    outcome = _run(properties, pa.table({"k": ["secret", "secret"]}))["t.k.unique"]

    assert [s.value for s in outcome.samples] == ["<masked large_string len=6>"] * 2


# --- primary key ------------------------------------------------------------------------------

_COMPOSITE = [
    {"name": "b", "logicalType": "integer", "primaryKey": True, "primaryKeyPosition": 2},
    {"name": "a", "logicalType": "string", "primaryKey": True, "primaryKeyPosition": 1},
    {"name": "c", "logicalType": "string"},
]


def test_the_primary_key_is_one_object_check_ordered_by_position() -> None:
    the_plan = plan(_object(_COMPOSITE))
    (key,) = (c for c in the_plan.checks if isinstance(c, PrimaryKey))

    assert key.check_id == "t.primary_key"
    assert key.columns == ("a", "b")
    assert the_plan.order.index("t.primary_key") == the_plan.order.index("t.extra_columns") - 1


def test_a_composite_primary_key_passes_when_pairs_are_distinct() -> None:
    table = pa.table({"a": ["x", "x", "y"], "b": [1, 2, 1], "c": ["p", "q", "r"]})

    outcome = _run(_COMPOSITE, table)["t.primary_key"]

    assert (outcome.status, outcome.failed_rows, outcome.total_rows) == ("passed", 0, 3)


def test_a_composite_primary_key_fails_on_a_repeated_pair() -> None:
    table = pa.table({"a": ["x", "y", "x", "x"], "b": [1, 1, 1, 2], "c": ["p"] * 4})

    outcome = _run(_COMPOSITE, table)["t.primary_key"]

    assert outcome.status == "failed"
    assert outcome.failed_rows == 2
    assert [(s.row, s.value) for s in outcome.samples] == [(0, ["x", "1"]), (2, ["x", "1"])]
    assert outcome.message is not None
    assert "2 repeated keys" in outcome.message


def test_a_composite_primary_key_fails_on_any_null_key_column() -> None:
    table = pa.table(
        {"a": ["x", None, "y"], "b": pa.array([1, 2, None], pa.int64()), "c": ["p"] * 3}
    )

    outcome = _run(_COMPOSITE, table)["t.primary_key"]

    assert (outcome.status, outcome.failed_rows) == ("failed", 2)
    assert [s.row for s in outcome.samples] == [1, 2]
    assert outcome.message is not None
    assert "2 null keys" in outcome.message


def test_a_single_column_primary_key_fails_on_null_and_nan() -> None:
    properties = [{"name": "id", "primaryKey": True}]
    table = pa.table({"id": pa.array([1.0, float("nan"), None, 2.0])})

    outcome = _run(properties, table)["t.primary_key"]

    assert (outcome.status, outcome.failed_rows) == ("failed", 2)


def test_a_missing_key_column_skips_the_primary_key_and_presence_fails() -> None:
    outcomes = _run(_COMPOSITE, pa.table({"a": ["x"], "c": ["p"]}))

    assert outcomes["t.b.present"].status == "failed"
    assert outcomes["t.primary_key"].status == "skipped"
    assert outcomes["t.primary_key"].skipped_reason == "key column 'b' is missing from the dataset"


def test_the_primary_key_of_an_empty_table_passes() -> None:
    table = pa.table(
        {
            "a": pa.array([], pa.string()),
            "b": pa.array([], pa.int64()),
            "c": pa.array([], pa.string()),
        }
    )

    outcome = _run(_COMPOSITE, table)["t.primary_key"]

    assert (outcome.status, outcome.total_rows) == ("passed", 0)


def test_a_primary_key_on_a_nested_column_is_skipped() -> None:
    properties = [{"name": "id", "primaryKey": True}]

    outcome = _run(properties, pa.table({"id": pa.array([[1]])}))["t.primary_key"]

    assert outcome.status == "skipped"


def test_primary_key_columns_follow_ignore_case() -> None:
    table = pa.table({"A": ["x", "x"], "B": [1, 1], "C": ["p", "q"]})

    the_plan = plan(_object(_COMPOSITE))
    (outcome,) = ArrowBackend(table).evaluate(
        [c for c in the_plan.checks if isinstance(c, PrimaryKey)], RunOptions(ignore_case=True)
    )

    assert outcome.status == "failed"


def test_primary_key_is_not_planned_for_schema_only_runs() -> None:
    the_plan = plan(_object(_COMPOSITE), PlanOptions(schema_only=True))

    assert "t.primary_key" not in the_plan.order


def test_an_unpositioned_primary_key_keeps_contract_order() -> None:
    properties = [
        {"name": "z", "primaryKey": True},
        {"name": "y", "primaryKey": True, "primaryKeyPosition": -1},
        {"name": "x", "primaryKey": True, "primaryKeyPosition": 1},
    ]

    (key,) = (c for c in plan(_object(properties)).checks if isinstance(c, PrimaryKey))

    assert key.columns == ("x", "z", "y")


# --- extra columns ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "status"), [("allow", None), ("warn", "warning"), ("fail", "failed")]
)
def test_extra_columns_follow_the_policy(policy: str, status: str | None) -> None:
    properties = [{"name": "a", "logicalType": "integer"}]
    table = pa.table({"a": [1], "extra_one": [1], "extra_two": [2]})

    outcomes = _run(properties, table, extra=policy)

    if status is None:
        assert "t.extra_columns" not in outcomes
        return
    extra = outcomes["t.extra_columns"]
    assert extra.status == status
    assert extra.message is not None
    assert "extra_one, extra_two" in extra.message


def test_extra_columns_pass_when_the_dataset_matches() -> None:
    outcomes = _run([{"name": "a"}], pa.table({"a": [1]}), extra="fail")

    assert outcomes["t.extra_columns"].status == "passed"


# --- ODCS version parity ----------------------------------------------------------------------


def _fixture_plan(version: str) -> Any:
    return plan(find_object(load_fixture_contract(version)), PlanOptions(strict_types=True))


@pytest.mark.parametrize("version", ["3.0.2", "3.1.0"])
def test_older_odcs_shapes_plan_identically_to_v3_2_0(version: str) -> None:
    assert _fixture_plan(version) == _fixture_plan("3.2.0")


@pytest.mark.parametrize("version", ["3.0.2", "3.1.0", "3.2.0"])
@pytest.mark.parametrize("good", [True, False])
def test_every_odcs_shape_gives_the_same_verdicts(version: str, good: bool) -> None:
    table = orders_table(good=good)
    reference = _fixture_plan("3.2.0")
    the_plan = _fixture_plan(version)

    got = ArrowBackend(table).evaluate(the_plan.checks, OPTS)
    want = ArrowBackend(table).evaluate(reference.checks, OPTS)

    assert got == want
    assert (len(the_plan.checks), len(the_plan.skipped)) == (
        len(reference.checks),
        len(reference.skipped),
    )
    assert {o.rule for o in got} >= {"enum", "unique", "primary_key"}


def test_the_older_shapes_really_differ_from_v3_2_0_in_the_document() -> None:
    older = load_fixture_contract("3.0.2")
    middle = load_fixture_contract("3.1.0")
    newest = load_fixture_contract("3.2.0")

    def status_of(doc: dict[str, Any]) -> Any:
        return doc["schema"][0]["properties"][4]["enum"]

    assert status_of(older) == status_of(middle) == ["new", "paid", "shipped"]
    assert status_of(newest) == [{"value": "new"}, {"value": "paid"}, {"value": "shipped"}]
    assert isinstance(older["team"], list)
    assert isinstance(middle["team"], dict)
    assert "team" not in newest
    assert older["schema"][0]["properties"][2]["quality"][0]["rule"] == "nullValues"
