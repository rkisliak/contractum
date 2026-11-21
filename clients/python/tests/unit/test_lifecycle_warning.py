"""Deprecation warnings in the library and the lifecycle policy outcome."""

from __future__ import annotations

import warnings
from datetime import date
from pathlib import Path

import pytest
from support.fake_server import FakeContractum, FakeVersion

from contractum.client import Client
from contractum.contracts import (
    ContractDeprecationWarning,
    ContractVersion,
    LifecyclePolicy,
    deprecation_message,
    evaluate_lifecycle,
)
from contractum.errors import UsageError

BASE = "https://contractum.example"
DOC = b"apiVersion: v3.0.2\nkind: DataContract\nid: payments.orders\nversion: 1.2.0\nname: Orders\n"


def _get(token: str, version: FakeVersion) -> ContractVersion:
    server = FakeContractum()
    server.add_contract("payments.orders", [version])
    with server, Client(BASE, token=token) as client:
        return client.get("payments.orders", version=version.semver)


def test_a_deprecated_version_warns_with_the_sunset_date(canary_token: str) -> None:
    version = FakeVersion("1.2.0", DOC, status="latest", deprecated=True, sunset_date="2027-01-31")
    with pytest.warns(ContractDeprecationWarning, match="deprecated, sunset 2027-01-31") as caught:
        fetched = _get(canary_token, version)
    assert fetched.deprecated
    assert str(caught[0].message) == "payments.orders@1.2.0 is deprecated, sunset 2027-01-31"
    assert caught[0].filename == __file__


def test_a_live_version_does_not_warn(canary_token: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        fetched = _get(canary_token, FakeVersion("1.2.0", DOC, status="latest"))
    assert not fetched.deprecated
    assert fetched.deprecation_notice is None


def test_deprecation_in_headers_alone_is_noticed(canary_token: str) -> None:
    version = FakeVersion(
        "1.2.0", DOC, status="latest", deprecated=True, sunset_date="2027-01-31", headers_only=True
    )
    with pytest.warns(ContractDeprecationWarning, match="sunset 2027-01-31"):
        fetched = _get(canary_token, version)
    assert fetched.deprecated
    assert fetched.sunset_date == date(2027, 1, 31)
    assert fetched.deprecated_at is not None


def test_a_sunset_without_deprecation_says_it_sunsets(canary_token: str) -> None:
    version = FakeVersion("1.2.0", DOC, status="latest", sunset_date="2027-01-31")
    with pytest.warns(ContractDeprecationWarning, match="payments.orders@1.2.0 sunsets 2027-01-31"):
        fetched = _get(canary_token, version)
    assert not fetched.deprecated


def test_deprecation_message_variants() -> None:
    assert deprecation_message("a@1", deprecated=True, sunset_date=None) == (
        "a@1 is deprecated, no sunset date"
    )
    assert deprecation_message("a@1", deprecated=False, sunset_date=None) is None
    assert deprecation_message("a@1", deprecated=False, sunset_date=date(2027, 1, 2)) == (
        "a@1 sunsets 2027-01-02"
    )


def _version(*, deprecated: bool, sunset: date | None) -> ContractVersion:
    return ContractVersion(
        contract_id=None,
        odcs_id="payments.orders",
        name=None,
        semver="1.2.0",
        status="latest",
        media_type="application/yaml",
        content=DOC,
        content_sha256="0" * 64,
        etag=None,
        deprecated=deprecated,
        sunset_date=sunset,
    )


def test_a_live_version_has_no_lifecycle_outcome() -> None:
    assert evaluate_lifecycle(_version(deprecated=False, sunset=None), LifecyclePolicy()) is None


def test_a_deprecated_version_is_a_warning_by_default() -> None:
    outcome = evaluate_lifecycle(
        _version(deprecated=True, sunset=date(2027, 1, 31)), LifecyclePolicy(), date(2026, 11, 1)
    )
    assert outcome is not None
    assert outcome.check_id == "contract.lifecycle"
    assert outcome.status == "warning"


def test_fail_on_deprecated_fails() -> None:
    outcome = evaluate_lifecycle(
        _version(deprecated=True, sunset=None),
        LifecyclePolicy(fail_on_deprecated=True),
        date(2026, 11, 1),
    )
    assert outcome is not None
    assert outcome.status == "failed"


def test_sunset_within_days_fails_only_when_near() -> None:
    version = _version(deprecated=True, sunset=date(2026, 12, 1))
    near = evaluate_lifecycle(version, LifecyclePolicy(sunset_within_days=40), date(2026, 11, 1))
    far = evaluate_lifecycle(version, LifecyclePolicy(sunset_within_days=10), date(2026, 11, 1))
    assert near is not None
    assert near.status == "failed"
    assert far is not None
    assert far.status == "warning"


def test_the_policy_defaults_to_today() -> None:
    version = _version(deprecated=True, sunset=date(2000, 1, 1))
    outcome = evaluate_lifecycle(version, LifecyclePolicy(sunset_within_days=0))
    assert outcome is not None
    assert outcome.status == "failed"


def test_render_converts_locally_and_keeps_the_original() -> None:
    version = _version(deprecated=False, sunset=None)
    assert version.render() == DOC.decode()
    assert version.render("original") == DOC.decode()
    assert version.render("json").startswith('{\n  "apiVersion": "v3.0.2"')
    assert version.render("yaml").startswith("apiVersion: v3.0.2\nkind: DataContract\n")
    with pytest.raises(UsageError):
        version.render("xml")  # type: ignore[arg-type]


def test_info_names_the_contract_for_reports() -> None:
    info = _version(deprecated=True, sunset=date(2027, 1, 31)).info
    assert info.source == "server"
    assert info.odcs_id == "payments.orders"
    assert info.semver == "1.2.0"
    assert info.deprecated is True
    assert info.sunset_date == "2027-01-31"


def test_from_file_hashes_the_bytes_and_reads_the_identity(tmp_path: Path) -> None:
    path = tmp_path / "orders.yaml"
    path.write_bytes(DOC)
    version = ContractVersion.from_file(path)
    assert version.source == "file"
    assert version.odcs_id == "payments.orders"
    assert version.semver == "1.2.0"
    assert version.name == "Orders"
    assert version.media_type == "application/yaml"
    assert version.content == DOC
    import hashlib

    assert version.content_sha256 == hashlib.sha256(DOC).hexdigest()
    json_path = tmp_path / "orders.json"
    json_path.write_text('{"id": "x"}', encoding="utf-8")
    assert ContractVersion.from_file(json_path).media_type == "application/json"
    assert ContractVersion.from_file(str(json_path)).odcs_id == "x"


def test_from_file_reports_unreadable_input_as_usage_errors(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="cannot read"):
        ContractVersion.from_file(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(UsageError):
        ContractVersion.from_file(bad)


def test_the_label_falls_back_to_the_contract_id() -> None:
    bare = ContractVersion(
        contract_id="abc",
        odcs_id=None,
        name=None,
        semver=None,
        status=None,
        media_type="application/yaml",
        content=b"{}",
        content_sha256="0" * 64,
        etag=None,
    )
    assert bare.label == "abc@-"
    assert bare.text == "{}"
