"""Nested properties: struct fields, list items and lists of objects (D-47)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from support.engine import prop
from support.odcs import contract_doc

from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.backend import RunOptions
from contractum.engine.checks import CheckOutcome
from contractum.engine.nested import Missing, NestedView, expand_nested, property_paths
from contractum.engine.plan import PlanOptions, plan
from contractum.odcs import SchemaObject, find_object, load_document, schema_objects
from contractum.report.model import ContractInfo, DatasetInfo, ToolInfo, build_report

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "contracts"
_SHIPPING = pa.struct(
    [("city", pa.string()), ("zip", pa.string()), ("geo", pa.struct([("lat", pa.float64())]))]
)
_LINE = pa.struct([("sku", pa.string()), ("qty", pa.int64())])
_CONTACT = pa.struct([("email", pa.string())])


def _object() -> SchemaObject:
    source = (FIXTURES / "orders-nested.yaml").read_bytes()
    return find_object(load_document(source))


def _table(**columns: Any) -> pa.Table:
    defaults: dict[str, Any] = {
        "order_id": pa.array(["a", "b", "c"], pa.string()),
        "shipping": pa.array([None, None, None], _SHIPPING),
        "tags": pa.array([[], [], []], pa.list_(pa.string())),
        "lines": pa.array([[], [], []], pa.list_(_LINE)),
        "contact": pa.array([None, None, None], _CONTACT),
    }
    defaults.update(columns)
    return pa.table(defaults)


def _run(
    table: pa.Table, run: RunOptions | None = None, **plan_options: Any
) -> dict[str, CheckOutcome]:
    the_plan = plan(_object(), PlanOptions(**plan_options))
    outcomes = ArrowBackend(table).evaluate(the_plan.checks, run or RunOptions(max_failures=10))
    return {outcome.check_id: outcome for outcome in outcomes}


def _rows(outcome: CheckOutcome) -> list[int]:
    return [sample.row for sample in outcome.samples]


# --- paths and ids ----------------------------------------------------------------------------


def test_property_paths_walk_nested_and_item_properties_depth_first() -> None:
    paths = property_paths(_object())

    columns = [path.column for path in paths]
    assert columns[:6] == [
        "order_id",
        "shipping",
        "shipping.city",
        "shipping.zip",
        "shipping.geo",
        "shipping.geo.lat",
    ]
    assert "tags[]" in columns
    assert "lines[].sku" in columns
    by_column = {path.column: path for path in paths}
    assert by_column["shipping.geo.lat"].steps == ("shipping", "geo", "lat")
    assert by_column["shipping.geo.lat"].depth == 3
    assert by_column["tags[]"].steps == ("tags", None)
    assert by_column["tags[]"].is_items and by_column["tags[]"].in_list
    assert by_column["lines[].sku"].steps == ("lines", None, "sku")
    assert not by_column["shipping.city"].in_list
    assert not by_column["order_id"].is_items


def test_a_sensitive_parent_classification_applies_to_its_children() -> None:
    by_column = {path.column: path for path in property_paths(_object())}

    assert by_column["contact"].classification == "restricted"
    assert by_column["contact.email"].classification == "restricted"
    assert by_column["shipping.city"].classification is None


def test_check_ids_use_dotted_and_bracket_paths() -> None:
    ids = set(plan(_object()).order)

    assert "orders.shipping.city.required" in ids
    assert "orders.shipping.city.present" in ids
    assert "orders.shipping.geo.lat.range" in ids
    assert "orders.tags[].pattern" in ids
    assert "orders.tags[].type" in ids
    assert "orders.tags[].present" not in ids
    assert "orders.lines[].sku.required" in ids
    assert "orders.lines[].qty.range" in ids


def test_nested_checks_carry_their_dataset_path() -> None:
    checks = {check.check_id: check for check in plan(_object()).checks}

    assert checks["orders.shipping.city.required"].steps == ("shipping", "city")
    assert checks["orders.tags[].pattern"].steps == ("tags", None)
    assert checks["orders.order_id.required"].steps == ()
    assert checks["orders.contact.email.required"].masked is True


# --- struct fields ----------------------------------------------------------------------------


def test_a_required_child_fails_only_where_the_parent_is_present() -> None:
    shipping = pa.array(
        [
            {"city": "Oslo", "zip": "01234", "geo": None},
            {"city": None, "zip": "1", "geo": None},
            None,
        ],
        _SHIPPING,
    )

    outcomes = _run(_table(shipping=shipping))

    required = outcomes["orders.shipping.city.required"]
    assert (required.status, required.failed_rows, required.total_rows) == ("failed", 1, 2)
    assert _rows(required) == [1]
    pattern = outcomes["orders.shipping.zip.pattern"]
    assert (pattern.status, pattern.failed_rows) == ("failed", 1)
    assert [(s.row, s.value) for s in pattern.samples] == [(1, "1")]
    assert outcomes["orders.shipping.city.present"].status == "passed"
    assert outcomes["orders.shipping.city.type"].status == "passed"


def test_a_null_parent_is_ignored_by_every_child_check() -> None:
    outcomes = _run(_table())

    assert outcomes["orders.shipping.city.required"].status == "passed"
    assert outcomes["orders.shipping.city.required"].total_rows == 0
    assert outcomes["orders.shipping.zip.pattern"].status == "passed"
    assert outcomes["orders.shipping.geo.lat.range"].status == "passed"


def test_deeper_struct_fields_are_reached_through_every_level() -> None:
    geo = {"lat": 95.0}
    shipping = pa.array(
        [{"city": "a", "zip": None, "geo": geo}, {"city": "b", "zip": None, "geo": {"lat": 1.0}}],
        _SHIPPING,
    )
    table = _table(
        order_id=pa.array(["a", "b"]),
        shipping=shipping,
        tags=pa.array([[], []], pa.list_(pa.string())),
        lines=pa.array([[], []], pa.list_(_LINE)),
        contact=pa.array([None, None], _CONTACT),
    )

    outcome = _run(table)["orders.shipping.geo.lat.range"]

    assert (outcome.status, _rows(outcome)) == ("failed", [0])


def test_a_missing_field_fails_presence_and_a_missing_parent_skips_it() -> None:
    narrow = pa.array([{"city": "Oslo"}], pa.struct([("city", pa.string())]))
    outcomes = _run(
        pa.table(
            {
                "order_id": ["a"],
                "shipping": narrow,
                "tags": pa.array([[]], pa.list_(pa.string())),
                "lines": pa.array([[]], pa.list_(_LINE)),
            }
        )
    )

    assert outcomes["orders.shipping.zip.present"].status == "failed"
    assert "'zip' is missing from the object" in str(
        outcomes["orders.shipping.zip.present"].message
    )
    assert outcomes["orders.shipping.zip.pattern"].status == "skipped"
    assert outcomes["orders.shipping.geo.lat.present"].status == "skipped"
    assert outcomes["orders.contact.email.present"].status == "skipped"
    assert "parent column 'contact' is missing" in str(
        outcomes["orders.contact.email.present"].skipped_reason
    )


def test_a_parent_that_is_not_an_object_skips_its_children() -> None:
    outcomes = _run(_table(shipping=pa.array(["x", "y", "z"], pa.string())))

    assert outcomes["orders.shipping.type"].status == "failed"
    assert outcomes["orders.shipping.city.present"].status == "skipped"
    assert "not an object" in str(outcomes["orders.shipping.city.present"].skipped_reason)


def test_nested_field_names_follow_the_ignore_case_option() -> None:
    shipping = pa.array(
        [{"CITY": None, "Zip": "1"}], pa.struct([("CITY", pa.string()), ("Zip", pa.string())])
    )
    table = pa.table(
        {
            "ORDER_ID": ["a"],
            "SHIPPING": shipping,
            "tags": pa.array([[]], pa.list_(pa.string())),
            "lines": pa.array([[]], pa.list_(_LINE)),
            "contact": pa.array([None], _CONTACT),
        }
    )

    strict = _run(table)
    folded = _run(table, RunOptions(ignore_case=True))

    assert strict["orders.shipping.city.present"].status == "skipped"
    assert folded["orders.shipping.city.present"].status == "passed"
    assert folded["orders.shipping.city.required"].status == "failed"
    assert folded["orders.shipping.zip.pattern"].status == "failed"


# --- arrays -----------------------------------------------------------------------------------


def test_list_items_are_judged_flattened_and_samples_cite_the_parent_row() -> None:
    tags = pa.array([["ab"], ["CD", "ef", "G1"], [], None], pa.list_(pa.string()))
    table = _table(
        order_id=pa.array(["a", "b", "c", "d"]),
        shipping=pa.array([None] * 4, _SHIPPING),
        tags=tags,
        lines=pa.array([[]] * 4, pa.list_(_LINE)),
        contact=pa.array([None] * 4, _CONTACT),
    )

    outcome = _run(table)["orders.tags[].pattern"]

    assert (outcome.status, outcome.failed_rows, outcome.total_rows) == ("failed", 2, 4)
    assert [(s.row, s.value) for s in outcome.samples] == [(1, "CD"), (1, "G1")]


@pytest.mark.parametrize(
    "list_type",
    [pa.list_(pa.string()), pa.large_list(pa.string()), pa.list_(pa.string(), 2)],
)
def test_every_list_spelling_flattens_the_same_way(list_type: pa.DataType) -> None:
    values = [["ab", "CD"], ["ef", "gh"]]
    table = _table(
        order_id=pa.array(["a", "b"]),
        shipping=pa.array([None, None], _SHIPPING),
        tags=pa.array(values, list_type),
        lines=pa.array([[], []], pa.list_(_LINE)),
        contact=pa.array([None, None], _CONTACT),
    )

    outcome = _run(table)["orders.tags[].pattern"]

    assert (outcome.status, _rows(outcome)) == ("failed", [0])


def test_a_list_of_objects_recurses_into_the_flattened_struct_fields() -> None:
    lines = pa.array(
        [
            [{"sku": "SKU-1", "qty": 1}],
            [{"sku": None, "qty": 0}, {"sku": "BAD-2", "qty": 5}],
            None,
            [None],
        ],
        pa.list_(_LINE),
    )
    table = _table(
        order_id=pa.array(["a", "b", "c", "d"]),
        shipping=pa.array([None] * 4, _SHIPPING),
        tags=pa.array([[]] * 4, pa.list_(pa.string())),
        lines=lines,
        contact=pa.array([None] * 4, _CONTACT),
    )

    outcomes = _run(table)

    required = outcomes["orders.lines[].sku.required"]
    assert (required.status, required.failed_rows, required.total_rows) == ("failed", 1, 3)
    assert _rows(required) == [1]
    pattern = outcomes["orders.lines[].sku.pattern"]
    assert [(s.row, s.value) for s in pattern.samples] == [(1, "BAD-2")]
    quantity = outcomes["orders.lines[].qty.range"]
    assert _rows(quantity) == [1]
    assert outcomes["orders.lines[].type"].status == "passed"
    assert outcomes["orders.lines[].sku.present"].status == "passed"
    assert outcomes["orders.lines.items"].status == "passed"


def test_unique_inside_array_items_is_skipped() -> None:
    properties = [
        prop("tags", "array", items={"logicalType": "string", "unique": True}),
    ]
    document = contract_doc(schema=[{"name": "t", "properties": properties}])

    result = plan(find_object(document))

    skipped = {entry.check_id: entry.reason for entry in result.skipped}
    assert skipped["t.tags[].unique"] == "unique is not evaluated inside array items"


# --- depth ------------------------------------------------------------------------------------


def _chain(levels: int) -> dict[str, Any]:
    node: dict[str, Any] = {"name": f"n{levels}", "logicalType": "string", "required": True}
    for level in range(levels - 1, 0, -1):
        node = {"name": f"n{level}", "logicalType": "object", "properties": [node]}
    return node


def test_nesting_deeper_than_ten_levels_is_skipped_once_per_cut_off_path() -> None:
    document = contract_doc(schema=[{"name": "t", "properties": [_chain(12)]}])

    result = plan(find_object(document))

    nesting = [entry for entry in result.skipped if entry.rule == "nesting"]
    assert len(nesting) == 1
    assert nesting[0].reason == "nesting deeper than 10 levels is not evaluated"
    assert nesting[0].column == "n1.n2.n3.n4.n5.n6.n7.n8.n9.n10.n11"
    evaluated = {check.column for check in result.checks}
    assert "n1.n2.n3.n4.n5.n6.n7.n8.n9.n10" in evaluated
    assert nesting[0].column not in evaluated


def test_two_cut_off_branches_give_two_skips() -> None:
    node: dict[str, Any] = {
        "name": "n10",
        "logicalType": "object",
        "properties": [
            {"name": "x", "logicalType": "string"},
            {"name": "y", "logicalType": "string"},
        ],
    }
    for level in range(9, 0, -1):
        node = {"name": f"n{level}", "logicalType": "object", "properties": [node]}
    document = contract_doc(schema=[{"name": "t", "properties": [node]}])

    result = plan(find_object(document))

    assert sorted(e.column for e in result.skipped if e.rule == "nesting") == [
        "n1.n2.n3.n4.n5.n6.n7.n8.n9.n10.x",
        "n1.n2.n3.n4.n5.n6.n7.n8.n9.n10.y",
    ]


def test_the_depth_limit_is_an_option() -> None:
    result = plan(_object(), PlanOptions(max_depth=1))

    assert {e.column for e in result.skipped if e.rule == "nesting"} >= {"shipping.city"}
    assert not any(check.steps for check in result.checks)


# --- masking, quality and views ----------------------------------------------------------------


def test_samples_of_a_child_of_a_restricted_parent_are_masked() -> None:
    contact = pa.array([{"email": "not-an-email"}, {"email": None}], _CONTACT)
    table = _table(
        order_id=pa.array(["a", "b"]),
        shipping=pa.array([None, None], _SHIPPING),
        tags=pa.array([[], []], pa.list_(pa.string())),
        lines=pa.array([[], []], pa.list_(_LINE)),
        contact=contact,
    )

    outcomes = _run(table)

    fmt = outcomes["orders.contact.email.format"]
    assert [s.value for s in fmt.samples] == ["<masked large_string len=12>"]
    assert outcomes["orders.contact.email.required"].samples[0].value is None
    assert (
        _run(table, mask="none")["orders.contact.email.format"].samples[0].value == "not-an-email"
    )


def test_nested_quality_rules_count_only_present_parents() -> None:
    properties = [
        prop(
            "shipping",
            "object",
            properties=[
                prop(
                    "zip",
                    "string",
                    quality=[
                        {"type": "library", "metric": "nullValues", "unit": "percent", "mustBe": 50}
                    ],
                )
            ],
        )
    ]
    document = contract_doc(schema=[{"name": "t", "properties": properties}])
    shipping = pa.array([{"zip": None}, {"zip": "1"}, None], pa.struct([("zip", pa.string())]))
    the_plan = plan(find_object(document))

    outcomes = ArrowBackend(pa.table({"shipping": shipping})).evaluate(
        the_plan.checks, RunOptions()
    )
    quality = next(o for o in outcomes if o.category == "quality")

    assert (quality.status, quality.observed, quality.total_rows) == ("passed", 50.0, 2)


def test_dictionary_and_view_children_are_made_kernel_safe() -> None:
    zips = pa.array(["01234", "bad", "01234"]).dictionary_encode()
    shipping = pa.StructArray.from_arrays(
        [pa.array(["a", "b", "c"]), zips, pa.nulls(3, pa.struct([("lat", pa.float64())]))],
        names=["city", "zip", "geo"],
    )
    table = _table(order_id=pa.array(["a", "b", "c"]), shipping=shipping)

    outcome = _run(table)["orders.shipping.zip.pattern"]

    assert (outcome.status, _rows(outcome)) == ("failed", [1])


def test_nested_rows_follow_the_sampling_row_index() -> None:
    tags = pa.array([["ok"], ["BAD"]], pa.list_(pa.string()))
    table = _table(
        order_id=pa.array(["a", "b"]),
        shipping=pa.array([None, None], _SHIPPING),
        tags=tags,
        lines=pa.array([[], []], pa.list_(_LINE)),
        contact=pa.array([None, None], _CONTACT),
    )
    the_plan = plan(_object())

    outcomes = ArrowBackend(table, row_index=[40, 70], sampled=True).evaluate(
        the_plan.checks, RunOptions()
    )
    pattern = next(o for o in outcomes if o.check_id == "orders.tags[].pattern")

    assert _rows(pattern) == [70]


def test_a_chunked_table_resolves_like_a_single_chunk() -> None:
    first = _table(tags=pa.array([["ok"], ["BAD"], []], pa.list_(pa.string())))
    table = pa.concat_tables([first, first])

    outcome = _run(table)["orders.tags[].pattern"]

    assert (outcome.failed_rows, _rows(outcome)) == (2, [1, 4])


def test_a_schema_only_plan_checks_nested_presence_on_an_empty_table() -> None:
    schema = _table().schema
    the_plan = plan(_object(), PlanOptions(schema_only=True))

    outcomes = ArrowBackend.from_schema(schema).evaluate(the_plan.checks, RunOptions())
    by_id = {o.check_id: o for o in outcomes}

    assert by_id["orders.shipping.city.present"].status == "passed"
    assert by_id["orders.shipping.city.type"].status == "passed"
    assert not any(check.rule == "required" for check in the_plan.checks)


def test_a_report_lists_nested_checks_in_contract_order() -> None:
    the_plan = plan(_object())
    outcomes = ArrowBackend(_table()).evaluate(the_plan.checks, RunOptions())

    report = build_report(
        tool=ToolInfo(version="0"),
        contract=ContractInfo(source="file"),
        dataset=DatasetInfo(kind="arrow"),
        plan=the_plan,
        outcomes=outcomes,
    )
    ids = [check.id for check in report.checks]

    assert ids.index("orders.shipping.present") < ids.index("orders.shipping.city.present")
    assert ids.index("orders.shipping.zip.pattern") < ids.index("orders.tags.present")
    assert report.summary.error == 0


def test_expand_nested_resolves_every_nested_path_of_an_object() -> None:
    view = expand_nested(_table(), _object())

    resolved = {steps for (steps, folded) in view.columns if not folded}
    assert ("shipping", "city") in resolved
    assert ("tags", None) in resolved
    assert ("lines", None, "sku") in resolved
    assert ("order_id",) not in resolved
    assert all(not isinstance(column, Missing) for column in view.columns.values())


def test_a_view_refuses_paths_that_start_with_items_and_unknown_roots() -> None:
    view = NestedView(_table())

    assert isinstance(view.column((None,)), Missing)
    assert isinstance(view.column(("nope", "x")), Missing)
    assert isinstance(view.column(("tags", "x")), Missing)
    assert isinstance(view.column(("order_id", None)), Missing)


def test_all_objects_of_a_contract_keep_their_own_paths() -> None:
    document = load_document((FIXTURES / "orders-nested.yaml").read_bytes())

    (only,) = schema_objects(document)

    assert only.name == "orders"
