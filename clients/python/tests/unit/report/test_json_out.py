"""The JSON report and the committed schema that pins it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from support.datasets import arrow_table, orders_rows
from support.odcs import contract_doc
from support.reports import all_statuses, schema_errors

from contractum.report import json_out
from contractum.report.model import Report
from contractum.testing import run_test


def _committed() -> dict[str, object]:
    loaded: dict[str, object] = json.loads(json_out.SCHEMA_PATH.read_text(encoding="utf-8"))
    return loaded


def test_report_json_round_trips_through_the_model() -> None:
    report = all_statuses()

    parsed = Report.model_validate_json(json_out.report_to_json(report))

    assert parsed == report
    assert json_out.report_to_json(report).endswith("}\n")


def test_json_of_a_real_run_validates_against_the_committed_schema(tmp_path: Path) -> None:
    path = tmp_path / "bad.parquet"
    pq.write_table(arrow_table(orders_rows(good=False)), path)
    report = run_test(contract_doc(), path)

    document = json.loads(json_out.report_to_json(report))

    assert report.summary.failed > 0
    assert schema_errors(document, _committed()) == []  # type: ignore[arg-type]
    assert document["schema_version"] == "1"


def test_the_schema_checker_rejects_a_document_that_breaks_the_schema() -> None:
    document = json.loads(json_out.report_to_json(all_statuses()))
    document["checks"][0]["status"] = "great"
    document["surprise"] = 1

    problems = schema_errors(document, _committed())  # type: ignore[arg-type]

    assert any("great" in problem for problem in problems)
    assert "unexpected surprise" in problems


def test_the_committed_schema_matches_the_model() -> None:
    assert json_out.SCHEMA_PATH.read_text(encoding="utf-8") == json_out.schema_text()
    assert json_out.schema_text().endswith("}\n")
    assert json_out.report_json_schema()["title"] == "Report"


def test_check_schema_passes_for_the_committed_file() -> None:
    assert json_out.main(["--check-schema"]) == 0


def test_check_schema_fails_and_names_the_fix_when_a_property_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    schema = _committed()
    del schema["properties"]["duration_ms"]  # type: ignore[attr-defined]
    stale = tmp_path / "report.schema.json"
    stale.write_text(json_out.schema_text(schema), encoding="utf-8")  # type: ignore[arg-type]
    monkeypatch.setattr(json_out, "SCHEMA_PATH", stale)

    assert json_out.main(["--check-schema"]) == 1
    assert json_out.REGENERATE in capsys.readouterr().err

    assert json_out.main(["--write-schema"]) == 0
    assert json_out.main(["--check-schema"]) == 0
    assert stale.read_text(encoding="utf-8") == json_out.schema_text()


def test_check_schema_fails_when_the_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(json_out, "SCHEMA_PATH", tmp_path / "absent.json")

    assert json_out.main(["--check-schema"]) == 1


def test_json_to_stdout_prints_only_the_json(capsys: pytest.CaptureFixture[str]) -> None:
    report = all_statuses()

    json_out.write_json(report, "-")

    assert capsys.readouterr().out == json_out.report_to_json(report)


def test_json_to_a_path_writes_the_file(tmp_path: Path) -> None:
    target = tmp_path / "report.json"

    json_out.write_json(all_statuses(), target)

    assert Report.model_validate_json(target.read_text(encoding="utf-8")) == all_statuses()


def test_the_module_runs_as_a_script() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "contractum.report.json_out", "--check-schema"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
