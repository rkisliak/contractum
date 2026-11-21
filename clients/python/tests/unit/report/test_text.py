"""The shared text helpers of the report writers."""

from __future__ import annotations

from support.reports import check, sample

from contractum.report.text import detail_lines, failing_text, sample_lines, strip_xml_invalid


def test_failing_text_covers_every_shape_of_counts() -> None:
    assert failing_text(check("a", "passed")) == "-"
    assert failing_text(check("a", "failed", failed_rows=3)) == "3"
    assert failing_text(check("a", "failed", failed_rows=1, total_rows=4)) == "1 / 4 (0 %)"
    full = check("a", "failed", failed_rows=1, total_rows=4, failed_percent=25.0)
    assert failing_text(full) == "1 / 4 (25 %)"


def test_detail_lines_hold_reason_counts_observed_threshold_and_samples() -> None:
    result = check(
        "a",
        "failed",
        message="too many nulls",
        failed_rows=2,
        total_rows=10,
        failed_percent=20.0,
        observed=0.2,
        threshold=0.1,
        samples=(sample(3, "x"), sample(4, "y")),
    )

    assert detail_lines(result) == [
        "too many nulls",
        "failing rows: 2 / 10 (20 %)",
        "observed: 0.2",
        "threshold: 0.1",
        "row 3: x",
        "row 4: y",
    ]
    assert detail_lines(result, sample_limit=1)[-1] == "row 3: x"
    assert sample_lines(result, 0) == []


def test_detail_lines_of_a_bare_check_are_empty() -> None:
    assert detail_lines(check("a", "passed")) == []
    assert detail_lines(check("a", "skipped", skipped_reason="why")) == ["why"]


def test_strip_xml_invalid_keeps_tabs_newlines_and_text() -> None:
    assert strip_xml_invalid("a\tb\nc\r\x00\x08￾\ud800d") == "a\tb\nc\rd"
