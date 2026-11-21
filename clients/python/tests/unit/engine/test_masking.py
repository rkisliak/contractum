"""Sample masking by classification, value rendering and the canary proof."""

from __future__ import annotations

import pyarrow as pa
import pytest
from support.engine import prop, run_properties
from support.odcs import contract_doc

from contractum.engine.arrow_backend import ArrowBackend
from contractum.engine.backend import RunOptions
from contractum.engine.checks import CheckOutcome, NotNull
from contractum.engine.masking import MAX_VALUE_CHARS, is_sensitive, render_value, should_mask
from contractum.engine.plan import PlanOptions, plan
from contractum.odcs import find_object
from contractum.report.model import ContractInfo, DatasetInfo, Report, ToolInfo, build_report

CANARY = "CANARY-7f3a9c-bad-address"


@pytest.mark.parametrize(
    ("classification", "mode", "expected"),
    [
        ("restricted", "auto", True),
        ("PII", "auto", True),
        (" Confidential ", "auto", True),
        ("sensitive", "auto", True),
        ("secret", "auto", True),
        ("3", "auto", True),
        ("4", "auto", True),
        ("5", "auto", True),
        ("2", "auto", False),
        ("public", "auto", False),
        (None, "auto", False),
        ("public", "all", True),
        (None, "all", True),
        ("restricted", "none", False),
        (None, "none", False),
    ],
)
def test_should_mask_follows_classification_and_mode(
    classification: str | None, mode: str, expected: bool
) -> None:
    assert should_mask(classification, mode) is expected  # type: ignore[arg-type]


def test_is_sensitive_ignores_missing_classifications() -> None:
    assert is_sensitive(None) is False
    assert is_sensitive("Restricted") is True


def test_a_masked_value_shows_only_its_type_and_length() -> None:
    assert render_value("secret-value!", pa.large_string(), masked=True) == (
        "<masked large_string len=13>"
    )
    assert render_value(12345, pa.int64(), masked=True) == "<masked int64 len=5>"


def test_a_null_is_shown_as_null_and_plain_values_as_text() -> None:
    assert render_value(None, pa.string(), masked=True) == "null"
    assert render_value(42, pa.int64(), masked=False) == "42"
    assert render_value("plain", pa.string(), masked=False) == "plain"


def test_long_values_are_cut_to_eighty_characters() -> None:
    shown = render_value("x" * 500, pa.string(), masked=False)

    assert shown == "x" * MAX_VALUE_CHARS + "..."
    assert render_value("y" * MAX_VALUE_CHARS, pa.string(), masked=False) == "y" * 80


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\x00b", "a\\x00b"),
        ("\u202eevil", "\\u202eevil"),
        ("line\nbreak", "line\\x0abreak"),
        ("zero\u200bwidth", "zero\\u200bwidth"),
        ("tag\U000e0001", "tag\\U000e0001"),
        ("café", "café"),
    ],
)
def test_control_and_bidirectional_characters_are_escaped(raw: str, expected: str) -> None:
    assert render_value(raw, pa.string(), masked=False) == expected


def test_escapes_never_split_at_the_cut() -> None:
    shown = render_value("a" * 78 + "\u202e", pa.string(), masked=False)

    assert shown == "a" * 78 + "..."


def test_max_failures_zero_keeps_no_samples_but_still_counts() -> None:
    table = pa.table({"x": pa.array([None, 1, None], pa.int64())})
    the_plan = plan(
        find_object(contract_doc(schema=[_object(prop("x", "integer", required=True))]))
    )

    (outcome,) = (
        o
        for o in ArrowBackend(table).evaluate(the_plan.checks, RunOptions(max_failures=0))
        if o.rule == "required"
    )

    assert (outcome.status, outcome.failed_rows, outcome.samples) == ("failed", 2, ())


def _object(*properties: dict[str, object], quality: list[dict[str, object]] | None = None) -> dict:
    result: dict[str, object] = {"name": "t", "properties": list(properties)}
    if quality is not None:
        result["quality"] = quality
    return result


def _report(table: pa.Table, **plan_options: object) -> tuple[Report, list[CheckOutcome]]:
    document = contract_doc(
        schema=[
            _object(
                prop("id", "string", required=True, unique=True),
                prop(
                    "email",
                    "string",
                    classification="restricted",
                    required=True,
                    enum=[{"value": "ok@example.com"}],
                    logicalTypeOptions={"format": "email", "minLength": 30, "maxLength": 40},
                    quality=[
                        {
                            "type": "library",
                            "metric": "invalidValues",
                            "mustBe": 0,
                            "arguments": {"pattern": "^ok@", "validValues": ["ok@example.com"]},
                        },
                        {"type": "library", "metric": "duplicateValues", "mustBe": 0},
                    ],
                ),
                quality=[
                    {
                        "type": "library",
                        "metric": "duplicateValues",
                        "mustBe": 0,
                        "arguments": {"properties": ["id", "email"]},
                    }
                ],
            )
        ]
    )
    the_plan = plan(find_object(document), PlanOptions(**plan_options))  # type: ignore[arg-type]
    outcomes = ArrowBackend(table).evaluate(the_plan.checks, RunOptions(max_failures=10))
    report = build_report(
        tool=ToolInfo(version="0"),
        contract=ContractInfo(source="file", odcs_id="x", semver="1.0.0"),
        dataset=DatasetInfo(kind="arrow", rows_tested=table.num_rows),
        plan=the_plan,
        outcomes=outcomes,
    )
    return report, outcomes


def _leaks(report: Report, outcomes: list[CheckOutcome]) -> list[str]:
    where = {
        "json": report.model_dump_json(),
        "summary": report.summary_text(max_checks=1000),
        "messages": " ".join(o.message or "" for o in outcomes),
        "reasons": " ".join(o.skipped_reason or "" for o in outcomes),
        "samples": " ".join(repr(o.samples) for o in outcomes),
    }
    return [name for name, text in where.items() if CANARY in text]


def test_a_canary_in_a_restricted_column_never_reaches_a_report_or_a_message() -> None:
    table = pa.table({"id": ["1", "2", "3"], "email": [CANARY, CANARY, None]})

    report, outcomes = _report(table)

    assert _leaks(report, outcomes) == []
    failing = [check for check in report.checks if check.status in ("failed", "warning")]
    assert {"format", "enum", "length", "required", "quality"} <= {c.rule for c in failing}
    shown = [
        sample.value for check in failing for sample in check.samples if check.column == "email"
    ]
    assert shown
    assert all(
        value is None or value.startswith("<masked") or isinstance(value, list) for value in shown
    )


def test_the_canary_is_visible_only_when_masking_is_switched_off() -> None:
    table = pa.table({"id": ["1", "2", "3"], "email": [CANARY, CANARY, None]})

    report, outcomes = _report(table, mask="none")

    assert _leaks(report, outcomes) == ["json", "samples"]


def test_a_kernel_failure_message_carries_the_exception_type_never_its_text() -> None:
    class Boom(ArrowBackend):
        def _not_null(self, spec: NotNull, opts: RunOptions) -> CheckOutcome:
            raise ValueError(CANARY)

    the_plan = plan(
        find_object(contract_doc(schema=[_object(prop("x", "integer", required=True))]))
    )

    outcomes = Boom(pa.table({"x": [1]})).evaluate(the_plan.checks, RunOptions())
    errored = [o for o in outcomes if o.status == "error"]

    assert errored
    assert all(CANARY not in (o.message or "") and CANARY not in repr(o) for o in errored)


def test_numeric_restricted_values_are_masked_too() -> None:
    outcomes = run_properties(
        [prop("pin", "integer", classification="4", required=True)],
        pa.table({"pin": pa.array([None, 1234], pa.int64())}),
    )

    assert [s.value for s in outcomes["t.pin.required"].samples] == [None]
    unique = run_properties(
        [prop("pin", "integer", classification="4", unique=True)],
        pa.table({"pin": pa.array([1234, 1234], pa.int64())}),
    )
    assert [s.value for s in unique["t.pin.unique"].samples] == ["<masked int64 len=4>"] * 2
