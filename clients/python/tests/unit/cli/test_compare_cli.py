"""``contractum compare``: the delta table, the exit codes and the JSON and Markdown outputs."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from rich.console import Console
from support.auth import Outcome, run_main
from support.datasets import arrow_table, orders_rows
from support.fake_server import FakeContractum, FakeVersion
from support.reports import check, make_report
from support.runs import BASE, contract_bytes, orders_server

from contractum import _transport
from contractum.cli import compare as compare_cli
from contractum.cli.context import GlobalOptions
from contractum.comparison import CompareResult, compare_reports
from contractum.report.model import Report

CANARY = "CANARY-secret-value"


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, canary_token: str) -> None:
    monkeypatch.setenv("CONTRACTUM_URL", BASE)
    monkeypatch.setenv("CONTRACTUM_TOKEN", canary_token)


def _run(
    argv: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Outcome:
    return run_main(["compare", *argv], capsys=capsys, monkeypatch=monkeypatch)


def _parquet(tmp_path: Path, *, quantity_cap: int | None = None, extra: bool = False) -> Path:
    rows = orders_rows(good=True)
    for row in rows:
        if quantity_cap is not None:
            row["quantity"] = min(row["quantity"], quantity_cap)
    table = arrow_table(rows)
    if extra:
        table = table.append_column("unlisted", table["quantity"])
    path = tmp_path / "orders.parquet"
    pq.write_table(table, path)
    return path


def _report_for(left: str, right: str) -> Report:
    return make_report(check(left, "passed"), check(right, "failed"))


def test_a_preview_regression_prints_the_delta_table_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with orders_server():
        outcome = _run(
            [str(_parquet(tmp_path)), "--contract", "payments.orders"], capsys, monkeypatch
        )

    assert outcome.code == 1
    lines = outcome.stdout.splitlines()
    assert lines[0].split() == ["Check", "Column", "Rule", "1.1.0", "2.0.0", "Change"]
    row = next(line for line in lines if line.startswith("orders.quantity.range"))
    assert row.split() == [
        "orders.quantity.range",
        "quantity",
        "range",
        "PASS",
        "FAIL",
        "changed,",
        "newly",
        "failing",
    ]
    assert "1 check newly failing in 2.0.0" in lines
    assert "contract diff: 2 changes (1 breaking, 1 info), required version bump: major" in lines
    assert "  [info] description.changed orders" in lines
    assert "payments.orders: 1.1.0 -> 2.0.0: 1 newly failing, 1 differ" in outcome.stderr
    assert outcome.stderr.rstrip().endswith("-> exit 1")


def test_a_preview_that_passes_exits_zero_and_says_nothing_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = [str(_parquet(tmp_path, quantity_cap=2)), "--contract", "payments.orders"]

    with orders_server():
        outcome = _run(argv, capsys, monkeypatch)

    assert outcome.code == 0
    assert "no check changed status between 1.1.0 and 2.0.0" in outcome.stdout
    assert "0 checks newly failing in 2.0.0" in outcome.stdout


def test_two_exact_versions_can_be_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = [str(_parquet(tmp_path)), "--contract", "payments.orders"]
    argv += ["--from", "1.0.0", "--to", "1.1.0"]

    with orders_server():
        outcome = _run(argv, capsys, monkeypatch)

    assert outcome.code == 0
    assert "0 checks newly failing in 1.1.0" in outcome.stdout
    assert "warning: payments.orders@1.0.0 is deprecated" in outcome.stderr


def test_a_missing_preview_exits_two_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeContractum()
    server.add_contract(
        "payments.orders", [FakeVersion("1.1.0", contract_bytes("1.1.0"), status="latest")]
    )

    with server:
        outcome = _run(
            [str(_parquet(tmp_path)), "--contract", "payments.orders"], capsys, monkeypatch
        )

    assert outcome.code == 2
    assert "no Preview exists" in outcome.stderr
    assert outcome.stdout == ""


def test_a_forbidden_diff_still_compares_and_prints_the_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = orders_server()
    server.diff_status = 403

    with server:
        outcome = _run(
            [str(_parquet(tmp_path)), "--contract", "payments.orders"], capsys, monkeypatch
        )

    assert outcome.code == 1
    assert "the classified diff is not available to this caller" in outcome.stdout
    assert "contract diff:" not in outcome.stdout


def test_a_server_error_on_the_diff_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_transport, "_retry_delay", lambda attempt, retry_after: 0.0)
    server = orders_server()
    server.diff_status = 500

    with server:
        outcome = _run(
            [str(_parquet(tmp_path)), "--contract", "payments.orders"], capsys, monkeypatch
        )

    assert outcome.code == 2
    assert "HTTP 500" in outcome.stderr


def test_a_warning_in_the_target_version_exits_one_only_with_strict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _parquet(tmp_path, quantity_cap=2, extra=True)
    argv = [str(data), "--contract", "payments.orders"]

    with orders_server():
        relaxed = _run(argv, capsys, monkeypatch)
    with orders_server():
        strict = _run([*argv, "--strict"], capsys, monkeypatch)

    assert (relaxed.code, strict.code) == (0, 1)


def test_json_to_stdout_prints_only_the_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = [str(_parquet(tmp_path)), "--contract", "payments.orders", "--json", "-"]

    with orders_server():
        outcome = _run(argv, capsys, monkeypatch)

    parsed = CompareResult.model_validate(json.loads(outcome.stdout))
    assert outcome.code == 1
    assert (parsed.from_semver, parsed.to_semver, parsed.newly_failing) == ("1.1.0", "2.0.0", 1)
    assert parsed.from_report.summary.failed == 0
    assert parsed.to_report.summary.failed == 1
    assert "newly failing" not in outcome.stdout.split('"schema_version"')[0]
    assert "-> exit 1" in outcome.stderr


def test_json_and_markdown_files_are_written_with_masked_values_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = orders_rows(good=True)
    rows[1]["customer_email"] = CANARY
    data = tmp_path / "canary.parquet"
    pq.write_table(arrow_table(rows), data)
    out_json, out_md = tmp_path / "out.json", tmp_path / "out.md"
    argv = [str(data), "--contract", "payments.orders", "--json", str(out_json)]
    argv += ["--markdown", str(out_md)]

    with orders_server():
        outcome = _run(argv, capsys, monkeypatch)

    assert outcome.code == 1
    written = out_json.read_text(encoding="utf-8")
    markdown = out_md.read_text(encoding="utf-8")
    assert "customer_email.format" in written
    assert "<masked" in written
    assert markdown.startswith("## Contractum compare:")
    assert "| Check | Column | Rule | 1.1.0 | 2.0.0 | Change |" in markdown
    for text in (written, markdown, outcome.stdout, outcome.stderr):
        assert CANARY not in text


def test_an_output_folder_that_does_not_exist_is_a_usage_error_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = [str(_parquet(tmp_path)), "--contract", "payments.orders"]
    argv += ["--markdown", str(tmp_path / "missing" / "out.md")]

    with orders_server() as server:
        outcome = _run(argv, capsys, monkeypatch)

    assert outcome.code == 2
    assert "the folder for --markdown does not exist" in outcome.stderr
    assert server.requests == []


def test_the_contract_option_is_required(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run([str(_parquet(tmp_path))], capsys, monkeypatch)

    assert outcome.code == 2
    assert "--contract" in outcome.stderr


def test_help_lists_the_compare_options(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run(["--help"], capsys, monkeypatch)

    assert outcome.code == 0
    for option in ("--contract", "--from", "--to", "--json", "--markdown", "--strict", "--object"):
        assert option in outcome.stdout


def _result(old: Report, new: Report) -> CompareResult:
    deltas = compare_reports(old, new)
    return CompareResult(
        ref="payments.orders",
        from_semver="1.1.0",
        to_semver="2.0.0",
        from_report=old,
        to_report=new,
        deltas=tuple(deltas),
        unchanged=sum(1 for d in deltas if d.change == "same"),
        newly_failing=sum(1 for d in deltas if d.regression),
    )


def test_a_terminal_gets_a_rich_table_and_hostile_values_stay_literal() -> None:
    old = make_report(
        check("[red]id[/red]", "passed", column="[bold]c[/bold]"), check("only", "failed")
    )
    new = make_report(
        check("[red]id[/red]", "failed", column="[bold]c[/bold]"), check("new", "passed")
    )
    buffer = io.StringIO()
    console = Console(file=buffer, width=140, force_terminal=False, color_system=None)

    compare_cli.render_compare(_result(old, new), GlobalOptions(), console=console)

    text = buffer.getvalue()
    assert "[red]id[/red]" in text
    assert "[bold]c[/bold]" in text
    assert "changed, newly failing" in text
    assert "removed" in text
    assert "new" in text
    assert "1 check newly failing in 2.0.0" in text
    assert "3 differ, 0 unchanged (3 checks)" in text


def test_a_tty_without_an_injected_console_uses_the_rich_table(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compare_cli.sys.stdout, "isatty", lambda: True, raising=False)
    result = _result(_report_for("a", "b"), _report_for("a", "b"))
    changed = _result(make_report(check("a", "passed")), make_report(check("a", "failed")))

    compare_cli.render_compare(result, GlobalOptions(color=False))
    compare_cli.render_compare(changed, GlobalOptions(color=False))

    out = capsys.readouterr().out
    assert "no check changed status" in out
    assert "changed, newly failing" in out


def test_the_footer_uses_the_plural_only_for_other_counts() -> None:
    one = _result(make_report(check("a", "passed")), make_report(check("a", "failed")))
    two = _result(
        make_report(check("a", "passed"), check("b", "passed")),
        make_report(check("a", "failed"), check("b", "failed")),
    )
    open_ended = one.model_copy(update={"to_semver": None})

    assert compare_cli._footer_lines(one)[0] == "1 check newly failing in 2.0.0"
    assert compare_cli._footer_lines(two)[0] == "2 checks newly failing in 2.0.0"
    assert compare_cli._footer_lines(open_ended)[0] == "1 check newly failing in the target version"
    assert compare_cli.headers(open_ended)[4] == "to"
