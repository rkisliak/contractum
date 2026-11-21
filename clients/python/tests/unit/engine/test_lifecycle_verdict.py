"""The lifecycle check, the report builder, the 0/1/2 verdict and the summary text."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from contractum.engine.checks import CheckOutcome, Plan, Skipped, Status
from contractum.engine.lifecycle import LIFECYCLE_CHECK_ID, lifecycle_outcome
from contractum.report.model import (
    ContractInfo,
    DatasetInfo,
    Report,
    ToolInfo,
    build_report,
)

TODAY = date(2026, 11, 21)


def _lifecycle(
    *,
    deprecated: bool = True,
    sunset: date | str | None = None,
    fail_on_deprecated: bool = False,
    within: int | None = None,
) -> CheckOutcome | None:
    return lifecycle_outcome(
        deprecated=deprecated,
        sunset_date=sunset,
        today=TODAY,
        fail_on_deprecated=fail_on_deprecated,
        sunset_within_days=within,
    )


def test_a_live_version_has_no_lifecycle_check() -> None:
    assert _lifecycle(deprecated=False, sunset="2026-11-22", fail_on_deprecated=True) is None


def test_a_deprecated_version_is_a_warning_naming_the_sunset() -> None:
    outcome = _lifecycle(sunset=date(2027, 1, 31))

    assert outcome is not None
    assert (outcome.check_id, outcome.category, outcome.status) == (
        LIFECYCLE_CHECK_ID,
        "lifecycle",
        "warning",
    )
    assert outcome.severity == "warning"
    assert outcome.message is not None
    assert "2027-01-31" in outcome.message


def test_deprecated_without_a_sunset_date_says_so() -> None:
    outcome = _lifecycle(sunset=None)

    assert outcome is not None
    assert outcome.status == "warning"
    assert outcome.message is not None
    assert "no sunset date" in outcome.message


def test_fail_on_deprecated_fails_the_check() -> None:
    outcome = _lifecycle(sunset="2027-01-31", fail_on_deprecated=True)

    assert outcome is not None
    assert (outcome.status, outcome.severity) == ("failed", "error")


@pytest.mark.parametrize(
    ("sunset", "within", "status"),
    [
        ("2026-12-01", 30, "failed"),
        ("2026-12-21", 30, "failed"),
        ("2026-12-22", 30, "warning"),
        ("2026-11-01", 5, "failed"),
        ("2026-12-01", None, "warning"),
        (None, 30, "warning"),
        ("not a date", 30, "warning"),
    ],
)
def test_sunset_within_n_days_fails(sunset: str | None, within: int | None, status: str) -> None:
    outcome = _lifecycle(sunset=sunset, within=within)

    assert outcome is not None
    assert outcome.status == status


def _result(check_id: str, status: Status) -> CheckOutcome:
    return CheckOutcome(
        check_id=check_id, category="schema", column=None, rule="r", status=status, message="m"
    )


def _report(statuses: list[Status], **info: str | None) -> Report:
    ids = [f"o.c{index}" for index in range(len(statuses))]
    outcomes = [_result(check_id, status) for check_id, status in zip(ids, statuses, strict=True)]
    plan = Plan(object_name="orders", checks=(), skipped=(), order=tuple(ids))
    return build_report(
        tool=ToolInfo(version="1"),
        contract=ContractInfo(
            source="file", odcs_id=info.get("odcs_id"), semver=info.get("semver")
        ),
        dataset=DatasetInfo(kind="arrow"),
        plan=plan,
        outcomes=outcomes,
        started_at=datetime(2026, 11, 21, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("statuses", "strict", "fail_on_skipped", "expected"),
    [
        (["failed", "error"], False, False, 2),
        (["passed", "error"], False, False, 2),
        (["failed", "warning"], False, False, 1),
        (["warning"], False, False, 0),
        (["warning"], True, False, 1),
        (["skipped"], False, False, 0),
        (["skipped"], False, True, 1),
        (["passed", "skipped"], False, True, 1),
        (["passed", "warning", "skipped"], False, False, 0),
        (["passed"], True, True, 0),
        ([], False, False, 0),
        ([], True, True, 0),
    ],
)
def test_exit_code_precedence(
    statuses: list[Status], strict: bool, fail_on_skipped: bool, expected: int
) -> None:
    report = _report(statuses)

    assert report.exit_code(strict=strict, fail_on_skipped=fail_on_skipped) == expected


def test_ok_ignores_warnings_and_skips() -> None:
    assert _report(["passed", "warning", "skipped"]).ok
    assert not _report(["failed"]).ok
    assert not _report(["error"]).ok


def test_summary_text_header_and_lines_for_non_passed_checks() -> None:
    report = _report(
        ["passed", "failed", "warning", "skipped", "error"], odcs_id="p.o", semver="1.2.3"
    )

    lines = report.summary_text().splitlines()

    assert lines[0] == (
        "contract p.o@1.2.3 object orders: 1 passed, 1 failed, 1 warning, 1 skipped, 1 error"
    )
    assert lines[1:] == [
        "failed o.c1: m",
        "warning o.c2: m",
        "skipped o.c3: m",
        "error o.c4: m",
    ]


def test_summary_text_truncates_with_a_plus_m_more_line() -> None:
    report = _report(["failed"] * 5)

    lines = report.summary_text(max_checks=2).splitlines()

    assert len(lines) == 4
    assert lines[-1] == "+3 more"


def test_zero_evaluated_checks_say_so_and_exit_zero() -> None:
    empty = _report([])
    only_skipped = _report(["skipped"])

    assert "no checks were evaluated" in empty.summary_text()
    assert "no checks were evaluated" in only_skipped.summary_text()
    assert empty.summary_text().startswith("contract -@- object orders: 0 passed")
    assert empty.exit_code() == 0
    assert "no checks were evaluated" not in _report(["passed"]).summary_text()


def test_skipped_entries_report_their_reason_and_never_pass() -> None:
    plan = Plan(
        object_name="orders",
        checks=(),
        skipped=(Skipped("o.sql", "quality", "quality", "sql rules are not evaluated locally"),),
        order=("o.sql",),
    )

    report = build_report(
        tool=ToolInfo(version="1"),
        contract=ContractInfo(source="file"),
        dataset=DatasetInfo(kind="arrow"),
        plan=plan,
        outcomes=[],
    )

    (check,) = report.checks
    assert check.status == "skipped"
    assert check.skipped_reason == "sql rules are not evaluated locally"
    assert report.summary.passed == 0
    assert "skipped o.sql: sql rules are not evaluated locally" in report.summary_text()


def test_a_planned_check_without_an_outcome_is_an_error_not_a_pass() -> None:
    plan = Plan(object_name="orders", checks=(), skipped=(), order=("o.lost",))

    report = build_report(
        tool=ToolInfo(version="1"),
        contract=ContractInfo(source="file"),
        dataset=DatasetInfo(kind="arrow"),
        plan=plan,
        outcomes=[],
    )

    assert report.checks[0].status == "error"
    assert report.exit_code() == 2


def test_order_follows_the_plan_then_unplanned_outcomes_then_lifecycle() -> None:
    plan = Plan(
        object_name="orders",
        checks=(),
        skipped=(Skipped("o.b", "constraint", "r", "reason"),),
        order=("o.c", "o.b", "o.a"),
    )
    lifecycle = lifecycle_outcome(
        deprecated=True,
        sunset_date=None,
        today=TODAY,
        fail_on_deprecated=False,
        sunset_within_days=None,
    )

    report = build_report(
        tool=ToolInfo(version="1"),
        contract=ContractInfo(source="lock", deprecated=True),
        dataset=DatasetInfo(kind="arrow"),
        plan=plan,
        outcomes=[
            _result("o.a", "passed"),
            _result("o.z", "passed"),
            _result("o.c", "failed"),
        ],
        lifecycle=lifecycle,
    )

    assert [check.id for check in report.checks] == [
        "o.c",
        "o.b",
        "o.a",
        "o.z",
        LIFECYCLE_CHECK_ID,
    ]
    assert report.checks[-1].category == "lifecycle"


def test_failed_percent_is_derived_from_the_row_counts() -> None:
    outcome = CheckOutcome(
        check_id="o.x",
        category="schema",
        column="x",
        rule="required",
        status="failed",
        failed_rows=1,
        total_rows=3,
    )
    plan = Plan(object_name="o", checks=(), skipped=(), order=("o.x",))

    report = build_report(
        tool=ToolInfo(version="1"),
        contract=ContractInfo(source="file"),
        dataset=DatasetInfo(kind="arrow"),
        plan=plan,
        outcomes=[outcome],
    )

    assert report.checks[0].failed_percent == 33.3333
