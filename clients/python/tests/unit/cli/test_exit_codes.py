"""The exit-code matrix of ``contractum test`` (0 ok, 1 mismatch, 2 error, 130 interrupt)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import warnings
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest
import respx
import yaml
from support.auth import Outcome, run_main
from support.datasets import arrow_table, orders_rows
from support.fake_server import FakeContractum, FakeVersion
from support.odcs import contract_doc
from support.runs import BASE, contract_bytes, orders_server

from contractum import _transport
from contractum.cli import render, run
from contractum.client import Client
from contractum.contracts import ContractDeprecationWarning


@dataclass
class Env:
    """Everything one matrix row can use: files, a stack for mocks and the monkeypatcher."""

    tmp_path: Path
    contract_file: Path
    good: Path
    bad: Path
    stack: ExitStack
    monkeypatch: pytest.MonkeyPatch
    token: str

    def server(self, server: FakeContractum | None = None) -> None:
        self.monkeypatch.setenv("CONTRACTUM_URL", BASE)
        self.monkeypatch.setenv("CONTRACTUM_TOKEN", self.token)
        self.stack.enter_context(server or orders_server())

    def failing_api(self, response: httpx.Response | Exception) -> None:
        """Every request to the API answers with ``response`` (or raises it)."""
        self.monkeypatch.setenv("CONTRACTUM_URL", BASE)
        self.monkeypatch.setenv("CONTRACTUM_TOKEN", self.token)
        router = self.stack.enter_context(respx.mock(base_url=BASE, assert_all_called=False))
        if isinstance(response, Exception):
            router.route().mock(side_effect=response)
        else:
            router.route().mock(return_value=response)
        self.monkeypatch.setattr(_transport, "_retry_delay", lambda attempt, retry_after: 0.0)

    def file_args(self, data: Path | str) -> list[str]:
        return [str(data), "--contract-file", str(self.contract_file)]


Scenario = Callable[[Env], list[str]]


def _pass(env: Env) -> list[str]:
    return env.file_args(env.good)


def _mismatch(env: Env) -> list[str]:
    return env.file_args(env.bad)


def _strict_with_warning(env: Env) -> list[str]:
    rows = orders_rows(good=True)
    wide = arrow_table(rows).append_column("unlisted", arrow_table(rows)["quantity"])
    path = env.tmp_path / "wide.parquet"
    pq.write_table(wide, path)
    return [*env.file_args(path), "--extra", "warn", "--strict"]


def _skip_fails(env: Env) -> list[str]:
    return [*env.file_args(env.good), "--schema-only", "--fail-on-skipped"]


def _unknown_option(env: Env) -> list[str]:
    return [*env.file_args(env.good), "--no-such-option"]


def _both_sources(env: Env) -> list[str]:
    return [*env.file_args(env.good), "--contract", "payments.orders"]


def _server_401(env: Env) -> list[str]:
    env.failing_api(httpx.Response(401, json={"title": "no", "status": 401}))
    return [str(env.good), "--contract", "payments.orders"]


def _server_500(env: Env) -> list[str]:
    env.failing_api(httpx.Response(500, json={"title": "boom", "status": 500}))
    return [str(env.good), "--contract", "payments.orders"]


def _connect_error(env: Env) -> list[str]:
    env.failing_api(httpx.ConnectError("refused"))
    return [str(env.good), "--contract", "payments.orders"]


def _hash_mismatch(env: Env) -> list[str]:
    tampered = FakeVersion(
        "1.1.0", contract_bytes("1.1.0"), status="latest", served=contract_bytes("1.1.0") + b"#\n"
    )
    server = FakeContractum()
    server.add_contract("payments.orders", [tampered])
    env.server(server)
    return [str(env.good), "--contract", "payments.orders"]


def _frozen_lock_mismatch(env: Env) -> list[str]:
    lock = env.tmp_path / "contractum.lock"
    server = orders_server()
    env.monkeypatch.setenv("CONTRACTUM_URL", BASE)
    with server, Client(BASE, token=env.token) as client, warnings.catch_warnings():
        warnings.simplefilter("ignore", ContractDeprecationWarning)
        client.pull(["payments.orders@1.1.0"], directory=env.tmp_path / "contracts", lock_path=lock)
    return [
        str(env.good),
        *("--contract", "payments.orders", "--version", "1.0.0"),
        *("--frozen", "--lock", str(lock)),
    ]


def _missing_data(env: Env) -> list[str]:
    return env.file_args(env.tmp_path / "absent.parquet")


def _unsupported_dataset(env: Env) -> list[str]:
    path = env.tmp_path / "data.xlsx"
    path.write_bytes(b"not a dataset")
    return env.file_args(path)


def _internal_error(env: Env) -> list[str]:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("planted")

    env.monkeypatch.setattr(run, "render_report", explode)
    return env.file_args(env.good)


def _interrupted(env: Env) -> list[str]:
    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    env.monkeypatch.setattr(run, "render_report", interrupt)
    return env.file_args(env.good)


MATRIX: list[tuple[str, Scenario, int, str]] = [
    ("pass", _pass, 0, "-> exit 0"),
    ("data mismatch", _mismatch, 1, "orders.order_id.required"),
    ("strict with a warning", _strict_with_warning, 1, "1 warning"),
    ("fail-on-skipped with a skip", _skip_fails, 1, "17 skipped"),
    ("unknown option", _unknown_option, 2, "No such option"),
    ("contract with contract-file", _both_sources, 2, "exactly one of --contract"),
    ("server 401", _server_401, 2, "HTTP 401"),
    ("server 500 after retries", _server_500, 2, "HTTP 500"),
    ("connection error", _connect_error, 2, "could not reach the server"),
    ("hash mismatch", _hash_mismatch, 2, "content hash mismatch"),
    ("frozen lock mismatch", _frozen_lock_mismatch, 2, "the lock pins payments.orders@1.1.0"),
    ("missing data file", _missing_data, 2, "absent.parquet"),
    ("unsupported dataset type", _unsupported_dataset, 2, "cannot tell the format"),
    ("planted internal error", _internal_error, 2, "internal error: RuntimeError"),
    ("keyboard interrupt", _interrupted, 130, "aborted"),
]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, canary_token: str) -> Callable[[], Env]:
    stack = ExitStack()
    contract_file = tmp_path / "orders.yaml"
    contract_file.write_text(yaml.safe_dump(contract_doc()), encoding="utf-8")
    good, bad = tmp_path / "good.parquet", tmp_path / "bad.parquet"
    pq.write_table(arrow_table(orders_rows(good=True)), good)
    pq.write_table(arrow_table(orders_rows(good=False)), bad)

    def build() -> Env:
        return Env(tmp_path, contract_file, good, bad, stack, monkeypatch, canary_token)

    yield build  # type: ignore[misc]
    stack.close()


@pytest.mark.parametrize(
    ("name", "scenario", "expected", "fragment"), MATRIX, ids=[row[0] for row in MATRIX]
)
def test_every_outcome_maps_to_its_exit_code(
    name: str,
    scenario: Scenario,
    expected: int,
    fragment: str,
    env: Callable[[], Env],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = scenario(env())

    outcome: Outcome = run_main(["test", *argv], capsys=capsys, monkeypatch=monkeypatch)

    assert outcome.code == expected, f"{name}: {outcome.output}"
    assert fragment in outcome.output, f"{name}: {outcome.output}"
    assert "Traceback" not in outcome.output


def test_an_internal_error_prints_only_the_type_name(
    env: Callable[[], Env], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = _internal_error(env())

    outcome = run_main(["test", *argv], capsys=capsys, monkeypatch=monkeypatch)

    assert "internal error: RuntimeError" in outcome.stderr
    assert "planted" not in outcome.output


def test_an_unsupported_dataset_names_the_file_and_the_exit_is_two(
    env: Callable[[], Env], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = _unsupported_dataset(env())

    outcome = run_main(["test", *argv], capsys=capsys, monkeypatch=monkeypatch)

    assert outcome.code == 2
    assert "data.xlsx" in outcome.stderr


# --- a real process ----------------------------------------------------------------------------


def _subprocess(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "contractum", "test", *argv],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
    )


def test_a_subprocess_returns_0_1_and_2(env: Callable[[], Env]) -> None:
    built = env()

    good = _subprocess(built.file_args(built.good))
    bad = _subprocess(built.file_args(built.bad))
    missing = _subprocess(built.file_args(built.tmp_path / "absent.parquet"))

    assert (good.returncode, bad.returncode, missing.returncode) == (0, 1, 2)
    assert "PASS" in good.stdout
    assert "FAIL" in bad.stdout
    assert "absent.parquet" in missing.stderr


# --- concurrent writers ------------------------------------------------------------------------


def test_two_runs_writing_the_same_junit_path_leave_one_complete_file(
    env: Callable[[], Env], capsys: pytest.CaptureFixture[str]
) -> None:
    built = env()
    target = built.tmp_path / "shared.xml"
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def worker(data: Path) -> None:
        argv = ["test", *built.file_args(data), "--junit", str(target)]
        barrier.wait()
        codes.append(_exit_code(argv))

    from contractum.cli import _run

    def _exit_code(argv: list[str]) -> int:
        return _run(argv)

    threads = [threading.Thread(target=worker, args=(data,)) for data in (built.good, built.bad)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    capsys.readouterr()

    root = ET.parse(target).getroot()  # noqa: S314
    suite = root.find("testsuite")
    assert suite is not None
    assert sorted(codes) == [0, 1]
    assert int(suite.attrib["tests"]) == len(suite.findall("testcase")) > 30
    assert suite.attrib["failures"] == str(len(root.findall(".//failure")))
    assert [p.name for p in built.tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_json_to_stdout_prints_no_table(
    env: Callable[[], Env], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the table must not be printed")

    monkeypatch.setattr(run, "render_report", refuse)
    monkeypatch.setattr(render, "render_report", refuse)
    built = env()

    outcome = run_main(
        ["test", *built.file_args(built.good), "--json", "-"],
        capsys=capsys,
        monkeypatch=monkeypatch,
    )

    assert outcome.code == 0
    assert outcome.stdout.lstrip().startswith("{")
