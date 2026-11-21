"""``compare``: one dataset against Latest and the Preview, with the server's classified diff."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from support.datasets import arrow_table, orders_rows
from support.fake_server import FakeContractum, FakeVersion
from support.payloads import load_payload
from support.reports import check, make_report
from support.runs import BASE, contract_bytes, orders_server

from contractum import _transport
from contractum.client import Client
from contractum.comparison import (
    CheckDelta,
    CompareResult,
    DiffSummary,
    compare,
    compare_reports,
    compare_to_json,
    compare_to_markdown,
    diff_lines,
    status_word,
    summarize_diff,
)
from contractum.contracts import ContractDeprecationWarning, ContractVersion
from contractum.errors import NotFoundError, ServerError, UsageError
from contractum.models import ContractDiff
from contractum.testing import TestOptions

CID = "5c1b7a0e-3f6d-4b6c-9d41-0a8f3f6a2b11"


@pytest.fixture
def client(canary_token: str) -> Client:
    return Client(BASE, token=canary_token)


def _data(tmp_path: Path, *, quantity_cap: int | None = None) -> Path:
    """Orders valid for 1.1.0; with ``quantity_cap`` also valid for the Preview (quantity < 3)."""
    rows = orders_rows(good=True)
    if quantity_cap is not None:
        for row in rows:
            row["quantity"] = min(row["quantity"], quantity_cap)
    path = tmp_path / "orders.parquet"
    pq.write_table(arrow_table(rows), path)
    return path


def test_data_that_passes_latest_but_breaks_the_preview_is_a_regression(
    client: Client, tmp_path: Path
) -> None:
    with orders_server():
        result = compare(client, "payments.orders", _data(tmp_path))

    changed = [d for d in result.deltas if d.change == "changed"]
    assert [(d.id, d.from_status, d.to_status, d.regression) for d in changed] == [
        ("orders.quantity.range", "passed", "failed", True)
    ]
    assert result.from_semver == "1.1.0"
    assert result.to_semver == "2.0.0"
    assert result.from_report.contract.status == "latest"
    assert result.to_report.contract.status == "preview"
    assert result.newly_failing == 1
    assert result.unchanged + len(result.shown) == len(result.deltas)
    assert result.from_report.exit_code() == 0
    assert result.exit_code() == 1


def test_the_servers_classified_diff_is_summarised(client: Client, tmp_path: Path) -> None:
    with orders_server() as server:
        result = compare(client, "payments.orders", _data(tmp_path))

    assert result.server_diff is not None
    assert result.server_diff.counts == {"breaking": 1, "info": 1}
    assert result.server_diff.required_bump == "major"
    assert result.server_diff.changes_total == 2
    assert result.server_diff.examples[0] == (
        "[breaking] property.type.changed orders.quantity "
        "(logicalTypeOptions.exclusiveMaximum: None -> 3)"
    )
    assert result.server_diff.examples[1] == "[info] description.changed orders"
    assert result.server_diff_note is None
    assert any(path.endswith("/diff") for path in server.paths())
    assert diff_lines(result)[0] == (
        "contract diff: 2 changes (1 breaking, 1 info), required version bump: major"
    )


def test_a_preview_that_still_accepts_the_data_exits_zero(client: Client, tmp_path: Path) -> None:
    with orders_server():
        result = compare(client, "payments.orders", _data(tmp_path, quantity_cap=2))

    assert result.newly_failing == 0
    assert result.shown == []
    assert result.exit_code() == 0


def test_a_failure_that_exists_in_both_versions_is_not_a_regression_but_still_exits_one(
    client: Client, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.parquet"
    pq.write_table(arrow_table(orders_rows(good=False)), bad)

    with orders_server():
        result = compare(client, "payments.orders", bad)

    assert result.newly_failing == 0
    assert result.exit_code() == 1
    shared = [d for d in result.deltas if d.id == "orders.order_id.required"]
    assert [(d.change, d.regression) for d in shared] == [("same", False)]


def test_two_exact_versions_can_be_compared_and_a_deprecated_one_warns(
    client: Client, tmp_path: Path
) -> None:
    with orders_server(), pytest.warns(ContractDeprecationWarning, match="deprecated"):
        result = compare(
            client,
            "payments.orders",
            _data(tmp_path),
            from_version="1.0.0",
            to_version="1.1.0",
        )

    assert (result.from_semver, result.to_semver) == ("1.0.0", "1.1.0")
    assert result.exit_code() == 0


def test_the_same_options_are_used_for_both_versions(client: Client, tmp_path: Path) -> None:
    with orders_server():
        result = compare(client, "payments.orders", _data(tmp_path), options=TestOptions(limit=1))

    assert result.from_report.dataset.rows_tested == 1
    assert result.to_report.dataset.rows_tested == 1
    assert result.newly_failing == 0


def test_a_missing_preview_is_reported_by_name(client: Client, tmp_path: Path) -> None:
    server = FakeContractum()
    server.add_contract(
        "payments.orders", [FakeVersion("1.1.0", contract_bytes("1.1.0"), status="latest")]
    )

    with server, pytest.raises(NotFoundError, match="no Preview exists"):
        compare(client, "payments.orders", _data(tmp_path))


@pytest.mark.parametrize("status", [403, 404])
def test_a_diff_the_caller_may_not_read_leaves_the_server_diff_empty_with_a_note(
    client: Client, tmp_path: Path, status: int
) -> None:
    server = orders_server()
    server.diff_status = status

    with server:
        result = compare(client, "payments.orders", _data(tmp_path))

    assert result.server_diff is None
    assert result.server_diff_note == "the classified diff is not available to this caller"
    assert result.newly_failing == 1
    assert diff_lines(result) == [result.server_diff_note]


def test_any_other_diff_error_fails_the_comparison(
    client: Client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_transport, "_retry_delay", lambda attempt, retry_after: 0.0)
    server = orders_server()
    server.diff_status = 500

    with server, pytest.raises(ServerError):
        compare(client, "payments.orders", _data(tmp_path))


def test_comparing_a_version_with_itself_skips_the_diff(client: Client, tmp_path: Path) -> None:
    with orders_server() as server:
        result = compare(
            client,
            "payments.orders",
            _data(tmp_path),
            from_version="1.1.0",
            to_version="latest",
        )

    assert result.server_diff is None
    assert result.server_diff_note == "both sides are version 1.1.0, there is nothing to diff"
    assert not any(path.endswith("/diff") for path in server.paths())
    assert result.shown == []


class _Stub:
    """A client that serves prepared versions; its diff is never reached."""

    def __init__(self, before: ContractVersion, after: ContractVersion) -> None:
        self._versions = {"latest": before, "preview": after}

    def get(self, ref: str, *, version: str | None = None) -> ContractVersion:
        return self._versions[version or "latest"]

    def diff(self, *args: Any, **kwargs: Any) -> ContractDiff:
        raise AssertionError("the diff must not be requested")


def _version(contract_id: str | None, semver: str | None) -> ContractVersion:
    content = contract_bytes(semver or "1.0.0")
    return ContractVersion(
        contract_id=contract_id,
        odcs_id="payments.orders",
        name="Orders",
        semver=semver,
        status=None,
        media_type="application/yaml",
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        etag=None,
    )


def test_two_different_contracts_are_refused(tmp_path: Path) -> None:
    stub = _Stub(_version(CID, "1.0.0"), _version("other", "2.0.0"))

    with pytest.raises(UsageError, match="two different contracts"):
        compare(stub, "payments.orders", _data(tmp_path))  # type: ignore[arg-type]


def test_a_version_without_id_or_semver_is_compared_without_a_diff(tmp_path: Path) -> None:
    stub = _Stub(_version(None, "1.0.0"), _version(None, "2.0.0"))

    result = compare(stub, "payments.orders", _data(tmp_path))  # type: ignore[arg-type]

    assert result.server_diff is None
    assert result.server_diff_note == "the server sent no contract id or version"


def test_checks_only_in_one_report_are_new_or_removed_and_ordered_after_the_target_checks() -> None:
    old = make_report(
        check("a", "passed"), check("gone", "failed"), check("b", "failed", column="b")
    )
    new = make_report(
        check("b", "failed", column="b"),
        check("fresh", "failed", column="f", message="1 of 4 values are null"),
        check("a", "warning"),
        check("ok_new", "passed"),
    )

    deltas = compare_reports(old, new)

    assert [(d.id, d.change, d.regression) for d in deltas] == [
        ("b", "same", False),
        ("fresh", "new", True),
        ("a", "changed", False),
        ("ok_new", "new", False),
        ("gone", "removed", False),
    ]
    assert deltas[1].message == "1 of 4 values are null"
    assert (deltas[4].from_status, deltas[4].to_status) == ("failed", None)


def test_an_error_in_the_target_version_is_a_regression_and_exits_two() -> None:
    old = make_report(check("a", "passed"))
    new = make_report(check("a", "error", message="could not be evaluated"))
    result = _result(old, new)

    assert result.newly_failing == 1
    assert result.exit_code() == 2


def _result(old: Any, new: Any, **fields: Any) -> CompareResult:
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
        **fields,
    )


def test_exit_code_follows_the_target_report_including_strict_and_skipped() -> None:
    warns = _result(make_report(check("a", "passed")), make_report(check("a", "warning")))
    skips = _result(make_report(check("a", "passed")), make_report(check("a", "skipped")))
    errors_before = _result(make_report(check("a", "error")), make_report(check("a", "passed")))

    assert warns.exit_code() == 0
    assert warns.exit_code(strict=True) == 1
    assert skips.exit_code() == 0
    assert skips.exit_code(fail_on_skipped=True) == 1
    assert errors_before.exit_code() == 2


def test_the_json_round_trips_and_carries_both_reports() -> None:
    result = _result(
        make_report(check("a", "passed")),
        make_report(check("a", "failed", message="boom")),
        server_diff=DiffSummary(counts={"breaking": 1}, changes_total=1, required_bump="major"),
    )

    text = compare_to_json(result)

    assert text.endswith("}\n")
    assert CompareResult.model_validate_json(text) == result
    assert '"regression": true' in text


def test_summarize_diff_limits_the_examples_and_flags_a_cut_short_server_list() -> None:
    payload = copy.deepcopy(load_payload("diff"))
    payload["changes"] = [{"kind": "added", "path": f"schema/{n}"} for n in range(12)]
    payload["changes_total"] = 12
    payload["counts"] = {"info": 12}
    diff = ContractDiff.model_validate(payload)

    summary = summarize_diff(diff)

    assert len(summary.examples) == 10
    assert summary.examples[0] == "[change] added schema/0"
    assert summary.truncated is False
    assert summary.changes_total == 12
    payload["changes_total"] = 40
    assert summarize_diff(ContractDiff.model_validate(payload)).truncated is True


def test_diff_lines_say_how_many_changes_are_not_listed() -> None:
    summary = DiffSummary(
        counts={}, changes_total=3, required_bump="none", truncated=True, examples=("[info] x y",)
    )
    result = _result(
        make_report(check("a", "passed")), make_report(check("a", "passed")), server_diff=summary
    )

    assert diff_lines(result) == [
        "contract diff: 3 changes, required version bump: none",
        "  [info] x y",
        "  +2 more",
    ]
    assert diff_lines(_result(make_report(), make_report())) == []


def test_the_markdown_shows_the_regression_and_escapes_hostile_text() -> None:
    old = make_report(check("a|b", "passed", column="<x>"), check("same", "passed"))
    new = make_report(check("a|b", "failed", column="<x>"), check("same", "passed"))
    result = _result(
        old,
        new,
        server_diff=DiffSummary(
            counts={"breaking": 1},
            changes_total=1,
            required_bump="major",
            examples=("[breaking] <b>x</b> | y",),
        ),
    )

    text = compare_to_markdown(result)

    assert text.startswith("## Contractum compare: REGRESSION")
    assert "- **Newly failing in 2.0.0:** 1" in text
    assert "| `a\\|b` | &lt;x&gt; | required | PASS | FAIL | changed, newly failing |" in text
    assert "`same`" not in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert "<b>" not in text


def test_the_markdown_for_an_unchanged_comparison_says_so_and_bounds_its_rows() -> None:
    quiet = _result(make_report(check("a", "passed")), make_report(check("a", "passed")))
    many_old = make_report(*(check(f"c{n}", "passed") for n in range(150)))
    many_new = make_report(*(check(f"c{n}", "failed") for n in range(150)))

    plain = compare_to_markdown(quiet)
    long = compare_to_markdown(_result(many_old, many_new))

    assert plain.startswith("## Contractum compare: OK")
    assert "No check changed status." in plain
    assert long.count("| `c") == 100
    assert "+50 more" in long


def test_status_word_and_delta_defaults() -> None:
    assert status_word(None) == "-"
    assert status_word("failed") == "FAIL"
    delta = CheckDelta(id="x", rule="r", change="same")
    assert (delta.from_status, delta.regression) == (None, False)
