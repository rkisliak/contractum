"""``contractum test``: table, report files and exit code for a contract file and a dataset."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml
from support.auth import Outcome, run_main
from support.datasets import arrow_table, orders_rows
from support.engine import options
from support.odcs import contract_doc
from support.runs import BASE, orders_server

from contractum.contracts import ContractVersion
from contractum.engine.arrow_schema import contract_to_arrow_schema
from contractum.report import json_out
from contractum.testing import TestOptions


def _run(
    argv: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Outcome:
    return run_main(["test", *argv], capsys=capsys, monkeypatch=monkeypatch)


@pytest.fixture
def contract_file(tmp_path: Path) -> Path:
    path = tmp_path / "orders.yaml"
    path.write_text(yaml.safe_dump(contract_doc()), encoding="utf-8")
    return path


def _parquet(tmp_path: Path, *, good: bool) -> Path:
    path = tmp_path / ("good.parquet" if good else "bad.parquet")
    pq.write_table(arrow_table(orders_rows(good=good)), path)
    return path


def test_good_data_prints_pass_and_writes_a_clean_junit_file(
    tmp_path: Path,
    contract_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    junit = tmp_path / "out.xml"

    outcome = _run(
        [str(_parquet(tmp_path, good=True)), "--contract-file", str(contract_file)]
        + ["--junit", str(junit)],
        capsys,
        monkeypatch,
    )

    assert outcome.code == 0
    assert "PASS" in outcome.stdout
    assert "FAIL" not in outcome.stdout
    assert "contract payments.orders@" in outcome.stdout
    suite = ET.parse(junit).getroot().find("testsuite")  # noqa: S314
    assert suite is not None
    assert suite.get("name") == "orders"
    assert suite.get("failures") == "0"


def test_bad_data_exits_one_names_the_failing_check_and_counts_failures_in_junit(
    tmp_path: Path,
    contract_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    junit = tmp_path / "out.xml"

    outcome = _run(
        [str(_parquet(tmp_path, good=False)), "--contract-file", str(contract_file)]
        + ["--junit", str(junit)],
        capsys,
        monkeypatch,
    )

    assert outcome.code == 1
    assert "FAIL" in outcome.stdout
    assert "orders.order_id.required" in outcome.stdout
    suite = ET.parse(junit).getroot().find("testsuite")  # noqa: S314
    assert suite is not None
    failed = [
        case.get("name") for case in suite.findall("testcase") if case.find("failure") is not None
    ]
    assert "orders.order_id.required" in failed
    assert suite.get("failures") == str(len(failed))
    assert int(suite.get("failures") or 0) > 1


def test_help_lists_the_contract_and_report_options(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run(["--help"], capsys, monkeypatch)

    assert outcome.code == 0
    for flag in ("--contract", "--version", "--frozen", "--json", "--junit", "--markdown"):
        assert flag in outcome.stdout


CANARY = "CANARY-5f3a9c@leak.example"


def _canary_parquet(tmp_path: Path) -> Path:
    rows = orders_rows(good=True)
    rows[0]["customer_email"] = CANARY.replace("@", "#")
    path = tmp_path / "canary.parquet"
    pq.write_table(arrow_table(rows), path)
    return path


def test_a_restricted_value_reaches_none_of_the_four_outputs(
    tmp_path: Path,
    contract_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {name: tmp_path / name for name in ("r.json", "r.xml", "r.md")}
    argv = [str(_canary_parquet(tmp_path)), "--contract-file", str(contract_file)]
    argv += ["--junit", str(files["r.xml"]), "--markdown", str(files["r.md"])]
    argv += ["--json", str(files["r.json"])]

    table = _run(argv, capsys, monkeypatch)

    assert table.code == 1
    assert "orders.customer_email.format" in table.stdout
    assert "<masked" in table.stdout
    stdout_json = _run(argv[:3] + ["--json", "-"], capsys, monkeypatch)
    for text in (
        table.output,
        stdout_json.output,
        *(path.read_text(encoding="utf-8") for path in files.values()),
    ):
        assert "CANARY-5f3a9c" not in text
    assert "<masked" in files["r.json"].read_text(encoding="utf-8")


def test_json_to_stdout_prints_only_json_and_the_summary_goes_to_stderr(
    tmp_path: Path,
    contract_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = [str(_parquet(tmp_path, good=False)), "--contract-file", str(contract_file)]

    outcome = _run([*argv, "--json", "-"], capsys, monkeypatch)

    parsed = json.loads(outcome.stdout)
    assert parsed["schema_version"] == "1"
    assert parsed["summary"]["failed"] > 0
    assert "PASS" not in outcome.stdout
    assert "-> exit 1" in outcome.stderr


def test_all_report_files_can_be_combined_and_a_missing_folder_is_a_usage_error(
    tmp_path: Path,
    contract_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = [str(_parquet(tmp_path, good=True)), "--contract-file", str(contract_file)]

    done = _run(
        [*argv, "--json", str(tmp_path / "a.json"), "--markdown", str(tmp_path / "a.md")],
        capsys,
        monkeypatch,
    )
    missing = _run([*argv, "--junit", str(tmp_path / "nope" / "a.xml")], capsys, monkeypatch)

    assert done.code == 0
    assert json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))["summary"]["ok"] is True
    assert (tmp_path / "a.md").read_text(encoding="utf-8").startswith("## Contractum: PASSED")
    assert missing.code == 2
    assert "does not exist" in missing.stderr
    assert not (tmp_path / "nope").exists()


def test_an_unwritable_report_path_is_exit_two_without_a_traceback(
    tmp_path: Path,
    contract_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "taken").mkdir()
    argv = [str(_parquet(tmp_path, good=True)), "--contract-file", str(contract_file)]

    outcome = _run([*argv, "--json", str(tmp_path / "taken")], capsys, monkeypatch)

    assert outcome.code == 2
    assert "cannot write --json file taken" in outcome.stderr
    assert "Traceback" not in outcome.output


# --- the server, the Preview and lifecycle risk ----------------------------------------------


@pytest.fixture
def server_environment(monkeypatch: pytest.MonkeyPatch, canary_token: str) -> None:
    monkeypatch.setenv("CONTRACTUM_URL", BASE)
    monkeypatch.setenv("CONTRACTUM_TOKEN", canary_token)


def _json_run(
    argv: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> tuple[Outcome, dict[str, Any]]:
    outcome = _run([*argv, "--json", "-"], capsys, monkeypatch)
    return outcome, json.loads(outcome.stdout) if outcome.stdout else {}


@pytest.mark.usefixtures("server_environment")
def test_latest_from_the_server_passes_and_the_report_names_the_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _parquet(tmp_path, good=True)

    with orders_server():
        outcome, report = _json_run(
            [str(good), "--contract", "payments.orders"], capsys, monkeypatch
        )

    assert outcome.code == 0
    assert report["contract"]["semver"] == "1.1.0"
    assert report["contract"]["status"] == "latest"
    assert report["contract"]["source"] == "server"
    assert report["summary"]["failed"] == 0


@pytest.mark.usefixtures("server_environment")
def test_the_preview_is_tested_on_request_and_its_own_rule_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _parquet(tmp_path, good=True)

    with orders_server():
        latest, _ = _json_run([str(good), "--contract", "payments.orders"], capsys, monkeypatch)
        preview, report = _json_run(
            [str(good), "--contract", "payments.orders", "--version", "preview"],
            capsys,
            monkeypatch,
        )

    assert latest.code == 0
    assert preview.code == 1
    assert report["contract"]["semver"] == "2.0.0"
    assert report["contract"]["status"] == "preview"
    failed = [check["id"] for check in report["checks"] if check["status"] == "failed"]
    assert failed == ["orders.quantity.range"]


@pytest.mark.usefixtures("server_environment")
def test_a_deprecated_version_warns_and_adds_a_lifecycle_check_to_every_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _parquet(tmp_path, good=True)
    argv = [str(good), "--contract", "payments.orders@1.0.0"]
    files = ["--junit", str(tmp_path / "l.xml"), "--markdown", str(tmp_path / "l.md")]

    with orders_server():
        outcome, report = _json_run([*argv, *files], capsys, monkeypatch)

    assert outcome.code == 0
    assert "is deprecated" in outcome.stderr
    lifecycle = next(c for c in report["checks"] if c["id"] == "contract.lifecycle")
    assert lifecycle["status"] == "warning"
    assert report["contract"]["deprecated"] is True
    junit = ET.parse(tmp_path / "l.xml").getroot()  # noqa: S314
    names = [case.get("name") for case in junit.iter("testcase")]
    assert names[-1] == "contract.lifecycle"
    assert "`contract.lifecycle`" in (tmp_path / "l.md").read_text(encoding="utf-8")


@pytest.mark.usefixtures("server_environment")
def test_fail_on_deprecated_turns_the_lifecycle_check_into_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _parquet(tmp_path, good=True)
    argv = [str(good), "--contract", "payments.orders@1.0.0", "--junit", str(tmp_path / "l.xml")]

    with orders_server():
        outcome, report = _json_run([*argv, "--fail-on-deprecated"], capsys, monkeypatch)
        near, _ = _json_run([*argv, "--sunset-within", "100000"], capsys, monkeypatch)

    lifecycle = next(c for c in report["checks"] if c["id"] == "contract.lifecycle")
    assert outcome.code == 1
    assert lifecycle["status"] == "failed"
    assert near.code == 1
    junit = ET.parse(tmp_path / "l.xml").getroot()  # noqa: S314
    failed = [c.get("name") for c in junit.iter("testcase") if c.find("failure") is not None]
    assert failed == ["contract.lifecycle"]


# --- the library returns what the CLI prints ---------------------------------------------------


def _without_timing(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key not in ("started_at", "duration_ms")}


def test_contract_version_test_equals_the_cli_report(
    tmp_path: Path,
    contract_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = _parquet(tmp_path, good=False)
    version = ContractVersion.from_file(contract_file)

    _, cli_report = _json_run(
        [str(bad), "--contract-file", str(contract_file), "--limit", "3", "--strict"],
        capsys,
        monkeypatch,
    )
    library = version.test(str(bad), limit=3, strict=True)

    assert _without_timing(json.loads(json_out.report_to_json(library))) == _without_timing(
        cli_report
    )
    assert cli_report["dataset"]["limit"] == 3


def test_contract_version_test_takes_base_options_and_overrides(
    tmp_path: Path, contract_file: Path
) -> None:
    version = ContractVersion.from_file(contract_file)
    bad = _parquet(tmp_path, good=False)

    masked = version.test(str(bad), options=TestOptions(mask="all"), samples=False)
    plain = version.test(str(bad))

    assert masked.summary.failed == plain.summary.failed > 0
    assert all(not check.samples for check in masked.checks)
    assert any(check.samples for check in plain.checks)


def test_to_arrow_schema_is_the_contract_schema(contract_file: Path) -> None:
    version = ContractVersion.from_file(contract_file)

    assert version.to_arrow_schema() == contract_to_arrow_schema(version.document)
    assert version.to_arrow_schema("orders").names[0] == "order_id"


def test_a_skipped_check_stays_skipped_in_every_format(
    tmp_path: Path,
    contract_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = [str(_parquet(tmp_path, good=True)), "--contract-file", str(contract_file)]
    argv += [
        "--schema-only",
        "--junit",
        str(tmp_path / "s.xml"),
        "--markdown",
        str(tmp_path / "s.md"),
    ]

    outcome, report = _json_run(argv, capsys, monkeypatch)

    skipped = [c for c in report["checks"] if c["status"] == "skipped"]
    assert outcome.code == 0
    assert skipped
    assert all(c["skipped_reason"] for c in skipped)
    assert not any(c["status"] == "passed" for c in skipped)
    junit = ET.parse(tmp_path / "s.xml").getroot()  # noqa: S314
    assert len(junit.findall(".//skipped")) == len(skipped)
    assert "schema-only run" in (tmp_path / "s.md").read_text(encoding="utf-8")


def _width_case(tmp_path: Path, values: list[int]) -> list[str]:
    """A contract with one i32 column and a Parquet file holding ``values`` in it."""
    document = contract_doc(
        schema=[{"name": "t", "properties": [options("n", "integer", format="i32")]}]
    )
    contract = tmp_path / "widths.yaml"
    contract.write_text(yaml.safe_dump(document), encoding="utf-8")
    data = tmp_path / "widths.parquet"
    pq.write_table(pa.table({"n": pa.array(values, pa.int64())}), data)
    return [str(data), "--contract-file", str(contract)]


def test_an_in_range_format_width_passes_with_fail_on_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = _width_case(tmp_path, [-(2**31), 0, 2**31 - 1])

    outcome = _run([*argv, "--fail-on-skipped", "--json", "-"], capsys, monkeypatch)

    assert outcome.code == 0, outcome.output
    report = json.loads(outcome.stdout)
    assert report["summary"]["skipped"] == 0
    (check,) = [c for c in report["checks"] if c["id"] == "t.n.format"]
    assert check["status"] == "passed"


@pytest.mark.parametrize(
    ("flags", "code", "status"),
    [([], 0, "warning"), (["--strict"], 1, "warning"), (["--strict-types"], 1, "failed")],
)
def test_a_value_outside_a_format_width_warns_unless_strict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    flags: list[str],
    code: int,
    status: str,
) -> None:
    argv = _width_case(tmp_path, [1, 2**31])

    outcome = _run([*argv, *flags, "--json", "-"], capsys, monkeypatch)

    assert outcome.code == code, outcome.output
    (check,) = [c for c in json.loads(outcome.stdout)["checks"] if c["id"] == "t.n.format"]
    assert check["status"] == status
