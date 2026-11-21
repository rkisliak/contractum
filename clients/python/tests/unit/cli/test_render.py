"""The terminal view: all five status words, masked samples, plain mode and markup safety."""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from support.reports import all_statuses, check, make_report, sample

from contractum.cli import render
from contractum.cli.context import GlobalOptions
from contractum.cli.render import render_report

OPTIONS = GlobalOptions()


def _plain(report: object, capsys: pytest.CaptureFixture[str], max_failures: int = 5) -> str:
    render_report(report, OPTIONS, max_failures=max_failures)  # type: ignore[arg-type]
    return capsys.readouterr().out


def _rich(report: object, max_failures: int = 5) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, width=200, color_system=None, highlight=False)
    render_report(report, OPTIONS, max_failures=max_failures, console=console)  # type: ignore[arg-type]
    return buffer.getvalue()


def test_plain_output_has_every_status_word_and_a_footer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = _plain(all_statuses(), capsys)

    for word in ("PASS", "FAIL", "WARN", "SKIP", "ERROR"):
        assert word in text
    assert "contract payments.orders@1.1.0 (latest), object orders" in text
    assert "1 passed, 1 failed, 1 warning, 1 skipped, 1 error" in text
    assert "1 / 4 (25 %)" in text
    assert "sql rules are not run" in text


def test_the_rich_table_has_the_same_cells() -> None:
    text = _rich(all_statuses())

    for word in ("Status", "Check", "Column", "Rule", "Failing", "Details"):
        assert word in text
    for word in ("PASS", "FAIL", "WARN", "SKIP", "ERROR"):
        assert word in text
    assert "orders.id.required" in text
    assert "row 1: None" in text
    assert "contract payments.orders@1.1.0 (latest)" in text


def test_a_terminal_gets_the_rich_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(render, "_is_tty", lambda: True)
    monkeypatch.setenv("COLUMNS", "200")

    text = _plain(all_statuses(), capsys)

    assert "orders.id.required" in text
    assert "│" in text or "|" in text


def test_masked_samples_show_the_mask_not_the_value(capsys: pytest.CaptureFixture[str]) -> None:
    report = make_report(
        check(
            "orders.email.format",
            "failed",
            column="email",
            samples=(sample(1, "<masked string len=12>"),),
        )
    )

    text = _plain(report, capsys)

    assert "row 1: <masked string len=12>" in text


def test_samples_are_limited_to_max_failures_with_a_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = make_report(
        check("c", "failed", samples=tuple(sample(row, f"v{row}") for row in range(4)))
    )

    shown = _plain(report, capsys, max_failures=2)
    none = _plain(report, capsys, max_failures=0)

    assert "row 1: v1" in shown
    assert "row 2: v2" not in shown
    assert "+2 more samples" in shown
    assert "row 0:" not in none


def test_markup_in_a_value_is_shown_as_text_not_interpreted() -> None:
    report = make_report(
        check("c", "failed", message="[bold red]boom[/bold red]", samples=(sample(0, "[link=x]"),))
    )

    text = _rich(report)

    assert "[bold red]boom[/bold red]" in text
    assert "row 0: [link=x]" in text


def test_a_report_without_checks_prints_only_the_footer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = _plain(make_report(), capsys)

    assert "no checks were evaluated" in text
    assert "Status" not in text


def test_a_report_with_only_skips_says_nothing_was_evaluated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = _plain(make_report(check("a", "skipped", skipped_reason="r")), capsys)

    assert "SKIP" in text
    assert "no checks were evaluated" in text
    assert "PASS" not in text


def test_footer_handles_an_unknown_contract_name() -> None:
    report = make_report(check("a", "passed"), status=None)

    lines = render.footer_lines(report)

    assert lines[0] == "contract payments.orders@1.1.0, object orders"
