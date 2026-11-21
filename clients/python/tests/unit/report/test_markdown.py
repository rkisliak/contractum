"""The Markdown summary: what it lists, how it escapes and that it stays bounded."""

from __future__ import annotations

from support.reports import all_statuses, check, make_report, sample

from contractum.report.markdown import report_to_markdown


def test_header_names_result_contract_version_and_rows() -> None:
    text = report_to_markdown(all_statuses())

    assert text.startswith("## Contractum: FAILED")
    assert "`payments.orders@1.1.0` (latest)" in text
    assert "4 of 4 rows tested" in text
    assert "1 passed, 1 failed, 1 warning, 1 skipped, 1 error" in text


def test_a_sampled_run_and_a_schema_only_run_say_so() -> None:
    sampled = report_to_markdown(make_report(check("a", "passed"), sampled=True, rows_tested=2))
    schema_only = report_to_markdown(make_report(check("a", "passed"), schema_only=True))

    assert "a sample, not every row" in sampled
    assert "schema only, no rows read" in schema_only


def test_lists_failed_error_and_warning_but_not_passed_and_groups_skips() -> None:
    text = report_to_markdown(all_statuses())

    assert "| FAIL | `orders.id.required`" in text
    assert "| ERROR | `orders.type`" in text
    assert "| WARN | `orders.amount.range`" in text
    assert "orders.id.present" not in text
    assert "<details><summary>1 skipped checks</summary>" in text
    assert "sql rules are not run: 1 checks (`orders.q[0]`)" in text


def test_a_passing_report_says_so_and_has_no_table() -> None:
    text = report_to_markdown(make_report(check("a", "passed")))

    assert text.startswith("## Contractum: PASSED")
    assert "Every evaluated check passed." in text
    assert "| Status |" not in text


def test_samples_sit_in_a_collapsed_block() -> None:
    text = report_to_markdown(all_statuses())

    assert "<details><summary>Failing values</summary>" in text
    assert "row 1: None" in text


def test_pipes_backticks_and_markup_are_escaped_in_cells() -> None:
    report = make_report(
        check("orders.id", "failed", message="a|b `c` <b>&</b>\nsecond line", column="x|y")
    )

    text = report_to_markdown(report)

    row = next(line for line in text.splitlines() if line.startswith("| FAIL"))
    assert "a\\|b \\`c\\` &lt;b&gt;&amp;&lt;/b&gt; second line" in row
    assert "x\\|y" in row
    assert row.count("|") - row.count("\\|") == 7


def test_a_value_with_backticks_cannot_close_the_sample_fence() -> None:
    report = make_report(check("orders.id", "failed", samples=(sample(0, "```` </details> ````"),)))

    text = report_to_markdown(report)

    assert "`````text" in text
    assert text.count("\n`````\n") == 1


def test_only_the_top_fifty_rows_are_listed_then_a_count() -> None:
    report = make_report(*(check(f"c{index}", "failed") for index in range(60)))

    text = report_to_markdown(report)

    assert "`c49`" in text
    assert "`c50`" not in text
    assert "+10 more" in text


def test_five_thousand_failing_checks_stay_under_the_size_limit() -> None:
    long = "x" * 400
    report = make_report(
        *(
            check(
                f"orders.column_{index}.rule",
                "failed",
                message=long,
                samples=tuple(sample(row, long) for row in range(5)),
            )
            for index in range(5000)
        )
    )

    text = report_to_markdown(report)

    assert len(text) < 65000
    assert "+4950 more" in text


def test_a_small_limit_drops_samples_then_rows_and_notes_it() -> None:
    report = make_report(
        *(check(f"c{index}", "failed", samples=(sample(0, "v"),)) for index in range(50))
    )

    full = report_to_markdown(report)
    without_samples = report_to_markdown(report, max_chars=len(full) - 1)
    tiny = report_to_markdown(report, max_chars=700)

    assert "Failing values" in full
    assert "Failing values" not in without_samples
    assert "shortened" in without_samples
    assert len(tiny) <= 700
    assert "shortened" in tiny or "## Contractum" in tiny


def test_an_impossible_limit_cuts_the_text_hard() -> None:
    text = report_to_markdown(all_statuses(), max_chars=40)

    assert len(text) == 40
