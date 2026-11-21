"""``assert_conforms``: options, contract inputs and what the failure message may contain."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from support.datasets import arrow_table, orders_rows
from support.odcs import contract_doc
from support.pytest_project import Project, write_project

from contractum import assert_conforms
from contractum.contracts import ContractVersion
from contractum.errors import ContractumError
from contractum.report.model import Report

CANARY = "CANARY-secret-value"


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return write_project(tmp_path / "project")


def _message(project: Project, data: object | None = None, **options: object) -> str:
    with pytest.raises(AssertionError) as raised:
        assert_conforms(data or project.bad, project.contract_file, **options)  # type: ignore[arg-type]
    return str(raised.value)


def _samples_under(text: str, check_id: str) -> list[str]:
    """The ``row N:`` lines that follow the line of ``check_id`` in a failure message."""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if f" {check_id}:" in line) + 1
    found: list[str] = []
    for line in lines[start:]:
        if not line.startswith("    row "):
            break
        found.append(line.strip())
    return found


def test_a_conforming_dataset_returns_the_report(project: Project) -> None:
    report = assert_conforms(project.good, project.contract_file)

    assert isinstance(report, Report)
    assert report.summary.failed == 0
    assert report.contract.semver == "1.0.0"


def test_the_message_names_contract_version_check_failing_count_and_samples(
    project: Project,
) -> None:
    text = _message(project)

    assert text.startswith("contract payments.orders@1.0.0 object orders:")
    assert "failed orders.order_id.pattern: 1 of 4 values do not match the pattern" in text
    assert _samples_under(text, "orders.order_id.pattern") == ["row 2: BAD-1"]


def test_max_failures_limits_the_samples_shown_per_check(project: Project) -> None:
    assert len(_samples_under(_message(project, max_failures=5), "orders.order_id.unique")) == 2
    assert len(_samples_under(_message(project, max_failures=1), "orders.order_id.unique")) == 1


def test_max_failures_zero_shows_no_samples(project: Project) -> None:
    assert "    row " not in _message(project, max_failures=0)


def test_auto_masking_hides_a_restricted_column_value_and_mask_all_hides_every_sample(
    project: Project, tmp_path: Path
) -> None:
    rows = orders_rows(good=True)
    rows[1]["customer_email"] = CANARY
    data = tmp_path / "canary.parquet"
    pq.write_table(arrow_table(rows), data)

    automatic = _message(project, data)
    everything = _message(project, data, mask="all")

    assert "customer_email.format" in automatic
    assert CANARY not in automatic
    assert "<masked" in automatic
    assert CANARY not in everything
    assert "ORD-" not in _message(project, mask="all")


def test_mask_none_shows_the_values(project: Project) -> None:
    assert "not-an-email" in _message(project, mask="none")


def test_a_warning_only_fails_under_strict(project: Project, tmp_path: Path) -> None:
    table = arrow_table(orders_rows(good=True))
    table = table.append_column("surprise", pa.array([1, 2, 3]))
    data = tmp_path / "extra.parquet"
    pq.write_table(table, data)

    report = assert_conforms(data, project.contract_file)
    text = _message(project, data, strict=True)

    assert report.summary.warning == 1
    assert "1 warning" in text
    assert "warning orders.extra_columns" in text


def test_a_skipped_check_never_fails_but_fail_on_skipped_makes_it_fail(project: Project) -> None:
    report = assert_conforms(project.good, project.contract_file)

    assert report.summary.skipped >= 1
    assert "skipped" in _message(project, project.good, fail_on_skipped=True)


def test_object_selects_the_schema_object(project: Project) -> None:
    report = assert_conforms(project.good, project.contract_file, object="orders")

    assert report.object == "orders"
    with pytest.raises(ContractumError):
        assert_conforms(project.good, project.contract_file, object="nope")


def test_other_options_reach_the_engine(project: Project) -> None:
    report = assert_conforms(project.bad, project.contract_file, limit=1)

    assert report.dataset.rows_tested == 1
    assert report.summary.failed == 0


def test_a_contract_can_be_a_version_a_mapping_text_or_a_path(project: Project) -> None:
    version = ContractVersion.from_file(project.contract_file)
    inputs: list[object] = [
        version,
        contract_doc(),
        project.contract_file,
        str(project.contract_file),
        project.contract_file.read_text(encoding="utf-8"),
        project.contract_file.read_bytes(),
    ]

    reports = [assert_conforms(project.good, contract) for contract in inputs]

    assert all(report.summary.failed == 0 for report in reports)
    assert reports[0].contract.source == "file"


def test_a_failing_contract_version_input_reports_the_same_text(project: Project) -> None:
    version = ContractVersion.from_file(project.contract_file)

    with pytest.raises(AssertionError) as raised:
        assert_conforms(project.bad, version)

    assert str(raised.value) == _message(project)


def test_an_unknown_option_is_rejected_before_any_data_is_read(project: Project) -> None:
    with pytest.raises(ValueError, match="nonsense"):
        assert_conforms(project.good, project.contract_file, nonsense=1)
