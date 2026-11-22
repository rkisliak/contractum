"""``contractum list``, ``versions`` and ``show`` through the real ``main`` entry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import rich.console
from support.auth import Outcome, run_main
from support.fake_server import FakeContractum, FakeVersion

from contractum.cli import fetch

BASE = "https://contractum.example"
DOC = b"apiVersion: v3.0.2\nkind: DataContract\nid: payments.orders\nversion: 1.2.0\n"
REPORT: dict[str, Any] = {
    "status": "completed",
    "structural": {"valid": True, "error_count": 0, "errors": []},
    "findings": [
        {
            "rule_set": {"name": "odcs-publication"},
            "source": "mandatory",
            "rule_key": "published-status-active",
            "severity": "warning",
            "blocking": False,
        },
        {
            "rule_set": {"name": "acme-owner"},
            "source": "mandatory",
            "rule_key": "owner-required",
            "severity": "error",
            "blocking": True,
        },
    ],
    "summary": {"publishable": False, "blocking_count": 1},
}


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, canary_token: str) -> None:
    monkeypatch.setenv("CONTRACTUM_URL", BASE)
    monkeypatch.setenv("CONTRACTUM_TOKEN", canary_token)


def _run(
    argv: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Outcome:
    return run_main(argv, capsys=capsys, monkeypatch=monkeypatch)


def _orders() -> FakeContractum:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [
            FakeVersion("1.2.0", DOC, status="latest", report=REPORT),
            FakeVersion(
                "1.1.0",
                DOC + b"# old\n",
                deprecated=True,
                sunset_date="2027-01-31",
            ),
            FakeVersion("1.3.0", DOC + b"# next\n", status="preview"),
        ],
        name="Orders",
        domain="sales",
    )
    return server


# --- list -----------------------------------------------------------------------------------


def test_list_prints_plain_aligned_rows_off_a_terminal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _orders()
    with server:
        outcome = _run(["list"], capsys, monkeypatch)
    assert outcome.code == 0
    header, row = outcome.stdout.splitlines()
    assert header.split()[:3] == ["ODCS", "ID", "NAME"]
    assert row.split()[:3] == ["payments.orders", "Orders", "1.2.0"]
    assert server.contracts[0].contract_id in row
    assert outcome.stderr == ""


def test_list_json_is_machine_readable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _orders():
        outcome = _run(["list", "--json", "--domain", "sales"], capsys, monkeypatch)
    data = json.loads(outcome.stdout)
    assert [item["odcs_id"] for item in data] == ["payments.orders"]
    assert data[0]["latest_version"]["semver"] == "1.2.0"


def test_an_empty_list_says_so_on_stderr_and_exits_0(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with FakeContractum():
        table = _run(["list"], capsys, monkeypatch)
        as_json = _run(["list", "--json"], capsys, monkeypatch)
    assert table.code == 0
    assert table.stdout == ""
    assert "no contracts found" in table.stderr
    assert as_json.code == 0
    assert json.loads(as_json.stdout) == []
    assert "no contracts found" in as_json.stderr


def test_list_keeps_server_order_and_shows_duplicate_odcs_ids_with_both_ids(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeContractum()
    folders = ["bbbbbbbb-0000-4000-8000-000000000002", "aaaaaaaa-0000-4000-8000-000000000001"]
    for folder in folders:
        server.add_contract(
            "payments.orders", [FakeVersion("1.0.0", DOC, status="latest")], folder_id=folder
        )
    server.add_contract("zeta", [FakeVersion("1.0.0", DOC, status="latest")])
    with server:
        outcome = _run(["list"], capsys, monkeypatch)
    rows = outcome.stdout.splitlines()[1:]
    assert [row.split()[0] for row in rows] == ["payments.orders", "payments.orders", "zeta"]
    assert folders[0] in rows[0]
    assert folders[1] in rows[1]
    assert server.contracts[0].contract_id in rows[0]
    assert server.contracts[1].contract_id in rows[1]


def test_list_all_and_limit_and_folder_filters(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeContractum()
    folder = server.add_folder("acme/payments")
    for number in range(3):
        server.add_contract(
            f"c-{number}",
            [FakeVersion("1.0.0", DOC, status="latest")],
            folder_id=folder,
            tags=["a"],
        )
    server.page_size = 1
    with server:
        limited = _run(["list", "--limit", "2", "--json"], capsys, monkeypatch)
        everything = _run(
            ["list", "--all", "--json", "--folder", "acme/payments", "--tag", "a"],
            capsys,
            monkeypatch,
        )
    assert len(json.loads(limited.stdout)) == 2
    assert len(json.loads(everything.stdout)) == 3


def test_list_in_an_unknown_folder_exits_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with FakeContractum():
        outcome = _run(["list", "--folder", "acme/none"], capsys, monkeypatch)
    assert outcome.code == 2
    assert "acme/none" in outcome.stderr


@pytest.mark.parametrize(
    ("legacy_console", "corner"),
    [(False, "┏"), (True, "┌")],
    ids=["modern-console", "legacy-windows-console"],
)
def test_list_on_a_terminal_renders_a_rich_table(
    legacy_console: bool,
    corner: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rich swaps the heavy header box for a light one on a legacy Windows console, whose fonts
    # lack the heavy glyphs. Pin the detection so both renderings are checked on every OS.
    monkeypatch.setattr(rich.console, "detect_legacy_windows", lambda: legacy_console)
    monkeypatch.setattr(fetch, "_is_tty", lambda: True)
    with _orders():
        outcome = _run(["--no-color", "list"], capsys, monkeypatch)
    assert outcome.code == 0
    assert "Contracts" in outcome.stdout
    assert "payments" in outcome.stdout
    assert corner in outcome.stdout


# --- versions -------------------------------------------------------------------------------


def test_versions_show_state_deprecation_and_sunset(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _orders():
        outcome = _run(["versions", "payments.orders"], capsys, monkeypatch)
    assert outcome.code == 0
    rows = [line.split() for line in outcome.stdout.splitlines()[1:]]
    assert [row[:2] for row in rows] == [
        ["1.3.0", "preview"],
        ["1.2.0", "latest"],
        ["1.1.0", "older"],
    ]
    assert rows[2][2:4] == ["yes", "2027-01-31"]
    assert rows[0][2:4] == ["no", "-"]


def test_versions_json_and_the_table_on_a_terminal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _orders():
        as_json = _run(["versions", "payments.orders", "--json"], capsys, monkeypatch)
        monkeypatch.setattr(fetch, "_is_tty", lambda: True)
        rich_table = _run(["--no-color", "versions", "payments.orders"], capsys, monkeypatch)
    data = json.loads(as_json.stdout)
    assert [item["status"] for item in data] == ["preview", "latest", "older"]
    assert data[2]["deprecated"] is True
    assert data[2]["sunset_date"] == "2027-01-31"
    assert "Versions of payments.orders" in rich_table.stdout


def test_versions_with_no_visible_version_is_an_empty_listing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeContractum()
    server.add_contract("payments.orders", [])
    with server:
        table = _run(["versions", "payments.orders"], capsys, monkeypatch)
        as_json = _run(["versions", "payments.orders", "--json"], capsys, monkeypatch)
    assert table.code == 0
    assert table.stdout == ""
    assert "no versions found" in table.stderr
    assert json.loads(as_json.stdout) == []


# --- show -----------------------------------------------------------------------------------


def test_show_prints_the_original_bytes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _orders():
        outcome = _run(["show", "payments.orders"], capsys, monkeypatch)
    assert outcome.code == 0
    assert outcome.stdout.encode() == DOC
    assert outcome.stderr == ""


def test_show_selects_a_version_by_reference_or_option(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _orders():
        preview = _run(["show", "payments.orders@preview"], capsys, monkeypatch)
        pinned = _run(["show", "payments.orders", "--version", "1.1.0"], capsys, monkeypatch)
    assert preview.stdout.endswith("# next\n")
    assert pinned.stdout.endswith("# old\n")


def test_show_converts_locally(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _orders():
        as_json = _run(["show", "payments.orders", "--format", "json"], capsys, monkeypatch)
        as_yaml = _run(["show", "payments.orders", "--format", "yaml"], capsys, monkeypatch)
    assert json.loads(as_json.stdout)["id"] == "payments.orders"
    assert as_yaml.stdout.startswith("apiVersion: v3.0.2\n")


def test_show_output_file_gets_the_exact_bytes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "out" / "orders.yaml"
    converted = tmp_path / "orders.json"
    with _orders():
        original = _run(["show", "payments.orders", "-o", str(target)], capsys, monkeypatch)
        as_json = _run(
            ["show", "payments.orders", "--format", "json", "-o", str(converted)],
            capsys,
            monkeypatch,
        )
    assert original.code == 0
    assert original.stdout == ""
    assert target.read_bytes() == DOC
    assert f"wrote {len(DOC)} bytes" in original.stderr
    assert json.loads(converted.read_text())["id"] == "payments.orders"
    assert as_json.code == 0


def test_show_metadata_prints_hash_etag_status_and_sunset(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _orders()
    with server:
        outcome = _run(["show", "payments.orders@1.1.0", "--metadata"], capsys, monkeypatch)
        targets = server.targets()
    lines = dict(line.split(": ", 1) for line in outcome.stdout.splitlines())
    assert outcome.code == 0
    assert lines["semver"] == "1.1.0"
    assert lines["status"] == "older"
    assert lines["deprecated"] == "true"
    assert lines["sunset_date"] == "2027-01-31"
    assert len(lines["content_sha256"]) == 64
    assert lines["etag"] == f'"{lines["content_sha256"]}"'
    assert "payments.orders@1.1.0 is deprecated, sunset 2027-01-31" in outcome.stderr
    assert not any(target.endswith("/versions/1.1.0") for target in targets)


def test_show_report_prints_counts_and_failing_rule_ids(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _orders():
        outcome = _run(["show", "payments.orders", "--report"], capsys, monkeypatch)
    assert outcome.code == 0
    assert "publishable: no" in outcome.stdout
    assert "findings: 2 (1 blocking); by severity: error 1, warning 1" in outcome.stdout
    assert "structural: valid" in outcome.stdout
    assert "owner-required [error blocking] (acme-owner)" in outcome.stdout
    assert "published-status-active [warning] (odcs-publication)" in outcome.stdout


def test_show_report_copes_with_sparse_reports(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeContractum()
    findings = [
        {"rule_key": f"r{n}", "severity": "info", "blocking": False, "source": "native"}
        for n in range(fetch.MAX_FINDING_LINES + 3)
    ]
    findings.append({"rule_key": "bad", "severity": "error"})
    findings.append("not a finding")  # type: ignore[arg-type]
    server.add_contract(
        "a",
        [
            FakeVersion(
                "1.0.0",
                DOC,
                status="latest",
                report={"findings": findings, "structural": {"valid": False, "error_count": 2}},
            )
        ],
    )
    server.add_contract(
        "b", [FakeVersion("1.0.0", DOC, status="latest", report={"summary": {"publishable": True}})]
    )
    server.add_contract("c", [FakeVersion("1.0.0", DOC, status="latest", report={"structural": 5})])
    with server:
        many = _run(["show", "a", "--report"], capsys, monkeypatch)
        publishable = _run(["show", "b", "--report"], capsys, monkeypatch)
        unknown = _run(["show", "c", "--report"], capsys, monkeypatch)
    assert "+4 more" in many.stdout
    assert "structural: invalid (2 errors)" in many.stdout
    assert "(native)" in many.stdout
    assert "publishable: yes" in publishable.stdout
    assert "findings: 0 (0 blocking); by severity: none" in publishable.stdout
    assert "publishable: unknown" in unknown.stdout
    assert "structural: unknown" in unknown.stdout


def test_show_with_metadata_and_report_prints_both(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _orders():
        outcome = _run(["show", "payments.orders", "--metadata", "--report"], capsys, monkeypatch)
    assert "content_sha256:" in outcome.stdout
    assert "publishable: no" in outcome.stdout


def test_output_file_with_metadata_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _orders():
        outcome = _run(
            ["show", "payments.orders", "--metadata", "-o", str(tmp_path / "x")],
            capsys,
            monkeypatch,
        )
    assert outcome.code == 2
    assert not (tmp_path / "x").exists()


def test_show_of_a_deprecated_version_warns_once_with_the_sunset_date(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _orders():
        outcome = _run(["show", "payments.orders@1.1.0"], capsys, monkeypatch)
    assert outcome.code == 0
    assert outcome.stderr.count("warning:") == 1
    assert "warning: payments.orders@1.1.0 is deprecated, sunset 2027-01-31" in outcome.stderr
    assert outcome.stdout.endswith("# old\n")


def test_show_prints_the_several_previews_warning(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _orders()
    server.contracts[0].versions.append(FakeVersion("1.4.0", DOC + b"# 14\n", status="preview"))
    with server:
        outcome = _run(["show", "payments.orders@preview"], capsys, monkeypatch)
    assert outcome.code == 0
    assert "warning:" in outcome.stderr
    assert "using 1.4.0" in outcome.stderr


def test_show_of_an_ambiguous_reference_exits_2_with_candidates_and_hint(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeContractum()
    first = server.add_contract(
        "payments.orders",
        [FakeVersion("1.0.0", DOC, status="latest")],
        folder_id="aaaaaaaa-0000-4000-8000-000000000001",
    )
    second = server.add_contract(
        "payments.orders",
        [FakeVersion("1.0.0", DOC, status="latest")],
        folder_id="bbbbbbbb-0000-4000-8000-000000000002",
    )
    with server:
        outcome = _run(["show", "payments.orders"], capsys, monkeypatch)
    assert outcome.code == 2
    assert outcome.stdout == ""
    assert first.contract_id in outcome.stderr
    assert second.contract_id in outcome.stderr
    assert "hint: use the contract id" in outcome.stderr


def test_show_of_a_missing_contract_and_a_bad_reference_exit_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with FakeContractum():
        missing = _run(["show", "nope"], capsys, monkeypatch)
        malformed = _run(["show", "orders@1.4"], capsys, monkeypatch)
    assert missing.code == 2
    assert "no readable contract" in missing.stderr
    assert malformed.code == 2
    assert "is not a version" in malformed.stderr


def test_help_lists_the_fetch_commands_and_survives_a_command_without_code(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run(["--help"], capsys, monkeypatch)
    assert outcome.code == 0
    for name in ("list", "versions", "show"):
        assert f"  {name}" in outcome.stdout
