"""The pytest plugin: fixtures and ``assert_conforms`` in a real pytest session (pytester)."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

import pytest
from support.fake_server import FakeContractum
from support.pytest_project import project_server, write_project
from support.runs import BASE, orders_server


def _frozen_args(lock: Path) -> list[str]:
    return ["--contractum-frozen", "--contractum-offline", "--contractum-lock", str(lock)]


def test_the_entry_point_registers_the_plugin_module() -> None:
    found = [
        entry.value
        for entry in importlib.metadata.entry_points(group="pytest11")
        if entry.name == "contractum"
    ]

    assert found == ["contractum.pytest_plugin"]


def test_a_conforming_dataset_passes_through_the_fixtures_offline_from_the_lock(
    pytester: pytest.Pytester, tmp_path: Path
) -> None:
    project = write_project(tmp_path / "project")
    pytester.makepyfile(
        f"""
        from contractum.pytest_support import assert_conforms

        def test_orders_conform(contractum_contract):
            contract = contractum_contract("payments.orders")
            report = assert_conforms({str(project.good)!r}, contract)
            assert report.summary.failed == 0
            assert contract.semver == "1.0.0"
        """
    )

    result = pytester.runpytest(*_frozen_args(project.lock))

    result.assert_outcomes(passed=1)


def test_a_non_conforming_dataset_fails_with_the_report_summary(
    pytester: pytest.Pytester, tmp_path: Path
) -> None:
    project = write_project(tmp_path / "project")
    pytester.makepyfile(
        f"""
        from contractum import assert_conforms

        def test_orders_conform(contractum_contract):
            assert_conforms({str(project.bad)!r}, contractum_contract("payments.orders"))
        """
    )

    result = pytester.runpytest(*_frozen_args(project.lock))

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*AssertionError: contract payments.orders@1.0.0 object orders*"])
    result.stdout.fnmatch_lines(["*failed orders.order_id.pattern*", "*row 2: BAD-1*"])


def test_the_plugin_imports_no_heavy_module_in_a_session_that_does_not_use_it(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makepyfile(
        """
        import sys

        def test_nothing_heavy_was_imported():
            assert "contractum.pytest_plugin" in sys.modules
            for name in ("pyarrow", "httpx", "contractum.client", "contractum.testing"):
                assert name not in sys.modules, name

        def test_still_plain():
            assert 1 + 1 == 2
        """
    )

    result = pytester.runpytest_subprocess("-p", "contractum.pytest_plugin")

    result.assert_outcomes(passed=2)


def test_the_plugin_can_be_switched_off_and_other_tests_still_run(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        """
        def test_uses_the_fixture(contractum_contract):
            pass

        def test_plain():
            assert True
        """
    )

    result = pytester.runpytest("-p", "no:contractum")

    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*fixture 'contractum_contract' not found*"])


def test_the_lock_comes_from_the_ini_value_relative_to_the_rootdir(
    pytester: pytest.Pytester,
) -> None:
    project = write_project(pytester.path / "pins")
    pytester.makeini("[pytest]\ncontractum_lock = pins/contractum.lock\n")
    pytester.makepyfile(
        f"""
        from contractum import assert_conforms

        def test_conforms(contractum_contract):
            assert_conforms({str(project.good)!r}, contractum_contract("payments.orders"))
        """
    )

    result = pytester.runpytest("--contractum-frozen", "--contractum-offline")

    result.assert_outcomes(passed=1)


def test_frozen_mode_can_be_switched_on_by_the_environment(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = write_project(tmp_path / "project")
    monkeypatch.setenv("CONTRACTUM_FROZEN", "1")
    pytester.makepyfile(
        """
        def test_missing_reference(contractum_contract):
            contractum_contract("payments.missing")
        """
    )

    result = pytester.runpytest("--contractum-offline", "--contractum-lock", str(project.lock))

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*'payments.missing' is not pinned in contractum.lock*"])
    assert "offline mode cannot resolve" not in result.stdout.str()


def test_a_tampered_pulled_file_fails_with_the_lock_error(
    pytester: pytest.Pytester, tmp_path: Path
) -> None:
    project = write_project(tmp_path / "project", tamper=True)
    pytester.makepyfile(
        """
        def test_tampered(contractum_contract):
            contractum_contract("payments.orders")
        """
    )

    result = pytester.runpytest(*_frozen_args(project.lock))

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*cannot get contract 'payments.orders'*does not match the lock*"])


def test_an_offline_run_names_a_reference_missing_from_the_lock(
    pytester: pytest.Pytester, tmp_path: Path
) -> None:
    project = write_project(tmp_path / "project")
    pytester.makepyfile(
        """
        def test_missing(contractum_contract):
            contractum_contract("payments.missing")
        """
    )

    result = pytester.runpytest("--contractum-offline", "--contractum-lock", str(project.lock))

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        ["*'payments.missing' is not pinned in contractum.lock, and offline mode cannot resolve*"]
    )
    result.stdout.fnmatch_lines(["*pin it first: contractum pull payments.missing*"])


def _online(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTRACTUM_URL", BASE)
    monkeypatch.setenv("CONTRACTUM_TOKEN", "dcp_0123456789abcdef_" + "T" * 43)


def _document_fetches(server: FakeContractum) -> list[str]:
    return [path for path in server.paths() if re.fullmatch(r".*/versions/[^/]+", path)]


def test_online_mode_fetches_a_reference_once_per_session(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _online(monkeypatch)
    server = orders_server()
    pytester.makepyfile(
        """
        def test_first(contractum_contract):
            assert contractum_contract("payments.orders").semver == "1.1.0"

        def test_second(contractum_contract):
            assert contractum_contract("payments.orders").semver == "1.1.0"
        """
    )

    with server:
        result = pytester.runpytest()

    result.assert_outcomes(passed=2)
    assert len(_document_fetches(server)) == 1


def test_the_client_fixture_is_the_one_the_contracts_come_from(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _online(monkeypatch)
    pytester.makepyfile(
        f"""
        def test_client(contractum_client, contractum_contract):
            assert contractum_client.url == {BASE!r}
            assert contractum_contract("payments.orders").semver == "1.1.0"
        """
    )

    with orders_server():
        result = pytester.runpytest()

    result.assert_outcomes(passed=1)


def test_online_mode_without_a_server_url_fails_with_the_configuration_error(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        """
        def test_no_url(contractum_contract):
            contractum_contract("payments.orders")
        """
    )

    result = pytester.runpytest()

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*cannot get contract 'payments.orders'*"])


def test_frozen_online_mode_verifies_the_pin_with_the_servers_hash_and_warns_when_deprecated(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = write_project(tmp_path / "project")
    monkeypatch.setenv("CONTRACTUM_TOKEN", "dcp_0123456789abcdef_" + "T" * 43)
    pytester.makepyfile(
        """
        import pytest
        from contractum.contracts import ContractDeprecationWarning

        def test_pinned(contractum_contract):
            with pytest.warns(ContractDeprecationWarning, match="deprecated"):
                assert contractum_contract("payments.orders").semver == "1.0.0"
        """
    )
    server = project_server(deprecated=True)

    with server:
        result = pytester.runpytest("--contractum-frozen", "--contractum-lock", str(project.lock))

    result.assert_outcomes(passed=1)
    assert any(path.endswith("/versions/1.0.0/metadata") for path in server.paths())
    assert not any(path.endswith("/contracts") for path in server.paths())
