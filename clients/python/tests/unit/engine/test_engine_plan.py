"""plan(): ordering, masking, schema-only runs and honest skips."""

from __future__ import annotations

import pyarrow as pa
import pytest
from support.engine import options, plan_properties
from support.odcs import contract_doc, orders_table

from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.backend import RunOptions
from contractum.engine.checks import NotNull, QualityMetric, Range, RowCount, TypeIs
from contractum.engine.masking import is_sensitive
from contractum.engine.plan import PlanOptions, plan
from contractum.engine.quality import severity_of
from contractum.odcs import QualityRule, find_object, schema_objects


def _plan(options: PlanOptions | None = None):  # type: ignore[no-untyped-def]
    return plan(find_object(contract_doc()), options)


def test_fixture_plan_orders_properties_then_object_checks_then_quality() -> None:
    order = _plan().order

    assert order[0] == "orders.order_id.present"
    assert order.index("orders.order_id.present") < order.index("orders.order_id.type")
    assert order.index("orders.order_id.type") < order.index("orders.order_id.required")
    assert order.index("orders.order_id.required") < order.index("orders.order_id.unique")
    assert order.index("orders.order_id.pattern") < order.index("orders.customer_email.present")
    assert order.index("orders.shipping.type") < order.index("orders.extra_columns")
    assert order.index("orders.extra_columns") < order.index("orders.quality[0]")
    assert len(order) == len(set(order))
    assert set(order) == {c.check_id for c in _plan().checks} | {
        s.check_id for s in _plan().skipped
    }


def test_library_quality_rules_of_the_fixture_become_checks_and_the_rest_are_skipped() -> None:
    result = _plan()
    checks = {check.check_id: check for check in result.checks}
    skipped = {entry.check_id: entry for entry in result.skipped}

    assert isinstance(checks["orders.amount.quality[0]"], QualityMetric)
    assert checks["orders.amount.quality[0]"].severity == "warning"
    assert checks["orders.amount.quality[0]"].category == "quality"
    assert isinstance(checks["orders.quality[0]"], RowCount)
    duplicates = checks["orders.quality[1]"]
    assert isinstance(duplicates, QualityMetric)
    assert duplicates.arguments["properties"] == ("order_id",)
    assert skipped["orders.quality[2]"].reason == "sql rules are not evaluated locally"
    assert skipped["orders.quality[3]"].reason == "text rule: documentation only"


def test_the_conforming_fixture_table_passes_every_evaluated_check() -> None:
    result = _plan()
    outcomes = ArrowBackend(orders_table(good=True)).evaluate(result.checks, RunOptions())

    assert {o.status for o in outcomes} == {"passed"}


def test_the_bad_fixture_table_fails_type_and_required() -> None:
    result = _plan()
    outcomes = {
        o.check_id: o
        for o in ArrowBackend(orders_table(good=False)).evaluate(result.checks, RunOptions())
    }

    assert outcomes["orders.order_id.required"].status == "failed"
    assert outcomes["orders.quantity.type"].status == "failed"


def test_ids_and_referenced_columns_are_deterministic_and_unique() -> None:
    first, second = _plan(), _plan()

    assert first == second
    assert first.referenced_columns[:3] == ("order_id", "customer_email", "amount")
    assert first.object_name == "orders"


def test_check_kinds_are_planned_per_declared_property_fields() -> None:
    checks = {c.check_id: c for c in _plan().checks}

    assert isinstance(checks["orders.order_id.required"], NotNull)
    assert "orders.amount.required" not in checks
    quantity = checks["orders.quantity.type"]
    assert isinstance(quantity, TypeIs)
    assert quantity.logical_type == "integer"
    assert not quantity.strict


def test_options_shape_the_plan() -> None:
    strict = {c.check_id: c for c in _plan(PlanOptions(strict_types=True)).checks}
    allow = _plan(PlanOptions(extra="allow"))
    fail = {c.check_id: c for c in _plan(PlanOptions(extra="fail")).checks}
    warn = {c.check_id: c for c in _plan().checks}

    assert strict["orders.amount.type"].strict is True  # type: ignore[union-attr]
    assert "orders.extra_columns" not in allow.order
    assert fail["orders.extra_columns"].severity == "error"
    assert warn["orders.extra_columns"].severity == "warning"


def test_schema_only_keeps_presence_and_type_checks_only() -> None:
    result = _plan(PlanOptions(schema_only=True))

    rules = {c.rule for c in result.checks} | {s.rule for s in result.skipped}

    assert rules <= {"present", "type", "physical_type", "extra_columns"}
    assert "present" in rules
    assert not any(s.category == "quality" for s in result.skipped)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("restricted", True),
        ("PII", True),
        (" Confidential ", True),
        ("sensitive", True),
        ("secret", True),
        ("3", True),
        ("5", True),
        ("1", False),
        ("public", False),
        ("internal", False),
        (None, False),
    ],
)
def test_sensitive_classifications(label: str | None, expected: bool) -> None:
    assert is_sensitive(label) is expected


def test_masking_follows_the_mask_option() -> None:
    def masked(option: str) -> dict[str, bool]:
        result = _plan(PlanOptions(mask=option))  # type: ignore[arg-type]
        return {c.check_id: c.masked for c in result.checks if c.column is not None}

    auto, everything, nothing = masked("auto"), masked("all"), masked("none")

    assert auto["orders.customer_email.type"] is True
    assert auto["orders.order_id.type"] is False
    assert all(everything.values())
    assert not any(nothing.values())


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("warning", "warning"),
        ("WARN", "warning"),
        ("info", "warning"),
        ("error", "error"),
        (None, "error"),
        ("x", "error"),
    ],
)
def test_quality_severity_mapping(label: str | None, expected: str) -> None:
    assert severity_of(QualityRule(index=0, severity=label)) == expected


def test_unknown_logical_types_are_skipped_and_undeclared_types_are_not_checked() -> None:
    document = {
        "schema": [
            {
                "name": "t",
                "properties": [
                    {"name": "a", "logicalType": "hologram"},
                    {"name": "b"},
                    {"name": "c", "logicalType": "STRING"},
                ],
            }
        ]
    }

    result = plan(find_object(document))

    assert [s.check_id for s in result.skipped] == ["t.a.type"]
    assert "hologram" in result.skipped[0].reason
    assert "t.b.type" not in result.order
    assert "t.c.type" in {c.check_id for c in result.checks}


def test_quality_ids_use_the_rule_id_and_stay_unique() -> None:
    document = {
        "schema": [
            {
                "name": "t",
                "properties": [
                    {
                        "name": "a",
                        "quality": [{"type": "library", "metric": "nullValues", "id": "dup"}],
                    },
                    {
                        "name": "a",
                        "quality": [{"type": "library", "metric": "nullValues", "id": "dup"}],
                    },
                ],
                "quality": [
                    {"type": "custom", "engine": "soda"},
                    {"type": "weird"},
                    {"metric": "rowCount"},
                ],
            }
        ]
    }

    result = plan(find_object(document))

    assert result.order == (
        "t.a.present",
        "dup",
        "t.a.present~2",
        "dup~2",
        "t.extra_columns",
        "t.quality[0]",
        "t.quality[1]",
        "t.quality[2]",
    )
    reasons = {s.check_id: s.reason for s in result.skipped}
    assert reasons["t.quality[0]"] == "custom engine rule: soda"
    assert "not recognised" in reasons["t.quality[1]"]
    assert reasons["t.quality[2]"] == "library rule has no threshold operator"


def test_physical_names_are_carried_into_the_checks() -> None:
    document = {
        "schema": [
            {
                "name": "t",
                "properties": [
                    {"name": "a", "physicalName": "A", "logicalType": "string", "required": True}
                ],
            }
        ]
    }

    result = plan(find_object(document))
    outcomes = ArrowBackend(pa.table({"A": ["x"]})).evaluate(result.checks, RunOptions())

    assert result.referenced_columns == ("A",)
    assert {o.status for o in outcomes} == {"passed"}
    assert schema_objects(document)[0].properties[0].source_name == "A"


@pytest.mark.parametrize(
    ("logical", "fmt"),
    [("integer", f"{kind}{bits}") for kind in "iu" for bits in (8, 16, 32, 64, 128)]
    + [("number", "f32")],
)
@pytest.mark.parametrize(("strict", "severity"), [(False, "warning"), (True, "error")])
def test_format_widths_plan_a_range_that_warns_unless_types_are_strict(
    logical: str, fmt: str, strict: bool, severity: str
) -> None:
    the_plan = plan_properties([options("n", logical, format=fmt)], strict_types=strict)

    (check,) = [c for c in the_plan.checks if c.check_id == "t.n.format"]
    assert isinstance(check, Range)
    assert check.severity == severity
    assert check.rule == "format"
    assert the_plan.skipped == ()


@pytest.mark.parametrize("strict", [False, True])
def test_f64_plans_no_format_check_and_unknown_widths_are_skipped(strict: bool) -> None:
    f64 = plan_properties([options("x", "number", format="f64")], strict_types=strict)
    odd = plan_properties([options("n", "integer", format="i7")], strict_types=strict)
    half = plan_properties([options("x", "number", format="f16")], strict_types=strict)

    assert "t.x.format" not in f64.order
    assert [s.check_id for s in odd.skipped] == ["t.n.format"]
    assert odd.skipped[0].reason == "integer format 'i7' has no value range to enforce"
    assert half.skipped[0].reason == "number format 'f16' has no value range to enforce"
