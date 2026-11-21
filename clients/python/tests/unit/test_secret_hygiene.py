"""No secret or restricted value leaves the credential store, on any command or library path.

``test_secret_hygiene_auth`` covers the sign-in surfaces one by one. This sweep runs the whole
command set (reads, pulls, tests, compares, reports) and the library calls, with a canary for
every secret kind, and then reads back every output and every file the run left on disk.
"""

from __future__ import annotations

import logging
import os
import stat
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
import pytest
import respx
from support.datasets import arrow_table, orders_rows
from support.flows import Credentials, config_flow, read_flow, sign_in_flow
from support.odcs import contract_doc
from support.session import Session, recording_server

from contractum import Client, ContractumError
from contractum.contracts import ContractVersion
from contractum.report.json_out import report_to_json
from contractum.report.junit import report_to_junit
from contractum.report.markdown import report_to_markdown

RESTRICTED = "CANARY-5f3a9c-restricted"
STORE = "credentials.json"
INPUTS = "inputs"
OTHER = "https://other.example"
PROBLEM = {"content-type": "application/problem+json"}


def _holders(root: Path, needles: tuple[str, ...], skip: tuple[str, ...] = ()) -> set[str]:
    """Relative names of the files under ``root`` that contain any needle (binary-safe)."""
    encoded = [needle.encode("utf-8") for needle in needles]
    found: set[str] = set()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or any(relative.startswith(prefix) for prefix in skip):
            continue
        content = path.read_bytes()
        if any(needle in content for needle in encoded):
            found.add(relative)
    return found


def _store_path(root: Path) -> str:
    return (Path(os.environ["CONTRACTUM_DATA_DIR"]) / STORE).relative_to(root).as_posix()


def _canary_parquet(directory: Path) -> Path:
    """Orders whose first customer_email (a restricted column) is a malformed canary value."""
    directory.mkdir(exist_ok=True)
    rows = orders_rows(good=True)
    rows[0]["customer_email"] = RESTRICTED.replace("-", "#", 1)
    path = directory / "canary.parquet"
    pq.write_table(arrow_table(rows), path)
    return path


def _failures(token: str) -> list[str]:
    """The text of every kind of error a Client raises while holding ``token``."""
    texts: list[str] = []

    def collect(action: Callable[[], Any]) -> None:
        try:
            action()
        except ContractumError as exc:
            texts.append("".join(traceback.format_exception(exc)))
            texts.append(repr(exc))
        else:
            pytest.fail("the call was expected to fail")

    with respx.mock(base_url=OTHER, assert_all_called=False) as other:
        other.get("/api/v1/identity/me").respond(401, json={"status": 401}, headers=PROBLEM)
        other.get("/api/v1/contracts/contracts").respond(403, json={"status": 403}, headers=PROBLEM)
        other.get("/api/v1/contracts/contracts/00000000-0000-4000-8000-000000000001").respond(
            429, json={"status": 429}, headers={**PROBLEM, "retry-after": "0"}
        )
        other.get("/api/v1/contracts/contracts/00000000-0000-4000-8000-000000000002").respond(
            500, json={"status": 500}, headers=PROBLEM
        )
        other.get("/api/v1/contracts/contracts/00000000-0000-4000-8000-000000000003").mock(
            side_effect=httpx.ConnectError("refused")
        )
        with Client(OTHER, token=token, cache=False, sleep=lambda _: None) as client:
            collect(client.whoami)
            collect(client.list_contracts)
            for suffix in ("1", "2", "3"):
                identifier = f"00000000-0000-4000-8000-00000000000{suffix}"
                collect(lambda identifier=identifier: client.get_contract(identifier))  # type: ignore[misc]
    collect(lambda: Client(OTHER, token=token, token_provider=object()))  # type: ignore[arg-type]
    collect(lambda: Client("http://plain.example", token=token))
    return texts


@pytest.fixture
def log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def test_no_command_leaks_a_secret_or_a_restricted_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    log: pytest.LogCaptureFixture,
    canary_token: str,
    assert_no_secret: Callable[..., None],
) -> None:
    creds = Credentials(canary_token)
    secrets = (canary_token,)
    server = recording_server()
    session = Session(server, capsys, monkeypatch)
    store = _store_path(tmp_path)
    data = _canary_parquet(tmp_path / INPUTS)

    def only_the_store_holds_secrets() -> None:
        assert _holders(tmp_path, secrets, skip=(INPUTS,)) <= {store}
        assert _holders(tmp_path, (RESTRICTED,), skip=(INPUTS,)) == set()

    session.after = only_the_store_holds_secrets
    monkeypatch.chdir(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    with server:
        sign_in_flow(session, creds)
        read_flow(session, tmp_path, data, creds, latest_exit=1)
        target = [str(data), "--contract", "payments.orders"]
        files = [f"--{kind}={reports / f'test.{kind}'}" for kind in ("json", "junit", "markdown")]
        session.run("test", *target, *files, expect=1, label="test report files")
        session.run("test", *target, "--json", "-", expect=1, label="test --json -")
        compared = [f"--{kind}={reports / f'compare.{kind}'}" for kind in ("json", "markdown")]
        session.run("compare", *target, *compared, expect=1, label="compare report files")
        session.run("compare", *target, "--json", "-", expect=1, label="compare --json -")
        session.run("config", "set", "token", canary_token, expect=2, backed=False, label="refuse")
        session.run("login", "--token", canary_token, expect=2, backed=False, label="argv token")
        config_flow(session)
        failures = _failures(canary_token)

    assert [name for name, count in session.reached.items() if count < 1] == []
    everything = "\n".join([*session.outputs, log.text, *failures])
    assert_no_secret(everything, *secrets, RESTRICTED)
    assert _holders(tmp_path, secrets, skip=(INPUTS,)) <= {store}
    assert _holders(tmp_path, (RESTRICTED,), skip=(INPUTS,)) == set()
    assert len(failures) == 14
    assert "<masked" in (reports / "test.json").read_text(encoding="utf-8")
    assert "<masked" in (reports / "compare.json").read_text(encoding="utf-8")
    assert {path.name for path in reports.iterdir()} == {
        f"{prefix}.{kind}"
        for prefix, kinds in (
            ("test", ("json", "junit", "markdown")),
            ("compare", ("json", "markdown")),
        )
        for kind in kinds
    }


def test_the_store_is_the_only_secret_holder_and_is_private(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    canary_token: str,
) -> None:
    server = recording_server()
    session = Session(server, capsys, monkeypatch)
    with server:
        session.run("login", "--token", "-", stdin=canary_token)
        stored = tmp_path / "data" / STORE
        assert canary_token in stored.read_text(encoding="utf-8")
        if os.name == "posix":
            assert stat.S_IMODE(stored.stat().st_mode) == 0o600
        session.run("logout", backed=False)
        assert canary_token not in stored.read_text(encoding="utf-8")
    assert _holders(tmp_path, (canary_token,)) == set()


def test_library_outputs_mask_restricted_values_and_hold_no_secret(
    tmp_path: Path, canary_token: str, assert_no_secret: Callable[..., None]
) -> None:
    import yaml

    document = tmp_path / "orders.yaml"
    document.write_text(yaml.safe_dump(contract_doc()), encoding="utf-8")
    data = _canary_parquet(tmp_path / INPUTS)

    report = ContractVersion.from_file(document).test(data)

    texts = [
        report_to_json(report),
        report_to_markdown(report),
        report_to_junit(report).decode("utf-8"),
        repr(report),
        str(report.summary),
    ]
    assert report.summary.failed > 0
    assert any("<masked" in text for text in texts[:3])
    assert_no_secret("\n".join(texts), RESTRICTED, canary_token)
