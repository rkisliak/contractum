"""``resolve_contract``: one way for ``test`` and ``compare`` to pick their contract."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import click
import pytest
from support.auth import Outcome, run_main
from support.fake_server import FakeContractum, FakeVersion, sha256

from contractum.cli import COMMANDS
from contractum.cli.context import GlobalOptions
from contractum.cli.source import (
    client_with_fallback,
    contract_source_options,
    lock_server,
    resolve_contract,
)
from contractum.client import Client
from contractum.contracts import ContractDeprecationWarning
from contractum.errors import ConfigError

BASE = "https://contractum.example"
DOC = b"apiVersion: v3.0.2\nkind: DataContract\nid: payments.orders\nversion: 1.4.0\n"
OLD = DOC + b"# 1.3.0\n"


@click.command("probe")
@contract_source_options
@click.pass_context
def probe(ctx: click.Context, **options: Any) -> None:
    """A stand-in command that only resolves its contract."""
    found = resolve_contract(ctx, **options)
    click.echo(f"{found.source} {found.semver} {found.content_sha256}")


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, canary_token: str) -> None:
    monkeypatch.setenv("CONTRACTUM_URL", BASE)
    monkeypatch.setenv("CONTRACTUM_TOKEN", canary_token)
    monkeypatch.setitem(COMMANDS, "probe", f"{__name__}:probe")


def _server(*, deprecated: bool = False) -> FakeContractum:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [
            FakeVersion(
                "1.4.0",
                DOC,
                status="latest",
                deprecated=deprecated,
                sunset_date="2027-01-31" if deprecated else None,
            ),
            FakeVersion("1.3.0", OLD),
        ],
        name="Orders",
    )
    return server


def _run(
    argv: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Outcome:
    return run_main(["probe", *argv], capsys=capsys, monkeypatch=monkeypatch)


def _pin(tmp_path: Path, token: str, server: FakeContractum, *refs: str) -> Path:
    lock_path = tmp_path / "contractum.lock"
    with server, Client(BASE, token=token) as client, warnings.catch_warnings():
        warnings.simplefilter("ignore", ContractDeprecationWarning)
        client.pull(list(refs), directory=tmp_path / "contracts", lock_path=lock_path)
    return lock_path


def _frozen(lock_path: Path, *extra: str) -> list[str]:
    return ["--contract", "payments.orders", *extra, "--frozen", "--lock", str(lock_path)]


# --- exactly one source -----------------------------------------------------------------------


def test_contract_and_contract_file_are_mutually_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "c.yaml"
    file.write_bytes(DOC)
    both = _run(
        ["--contract", "payments.orders", "--contract-file", str(file)], capsys, monkeypatch
    )
    neither = _run([], capsys, monkeypatch)
    assert both.code == 2
    assert neither.code == 2
    assert "exactly one of --contract and --contract-file" in both.stderr
    assert "exactly one of --contract and --contract-file" in neither.stderr


def test_a_contract_file_needs_no_server_and_no_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CONTRACTUM_URL")
    monkeypatch.delenv("CONTRACTUM_TOKEN")
    file = tmp_path / "orders.yaml"
    file.write_bytes(DOC)
    server = FakeContractum()
    with server:
        outcome = _run(["--contract-file", str(file)], capsys, monkeypatch)
    assert outcome.code == 0
    assert outcome.stdout.split() == ["file", "1.4.0", sha256(DOC)]
    assert server.requests == []


def test_a_version_does_not_apply_to_a_contract_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "orders.yaml"
    file.write_bytes(DOC)
    outcome = _run(["--contract-file", str(file), "--version", "1.3.0"], capsys, monkeypatch)
    assert outcome.code == 2
    assert "--version applies to --contract" in outcome.stderr


def test_a_missing_contract_file_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run(["--contract-file", str(tmp_path / "none.yaml")], capsys, monkeypatch)
    assert outcome.code == 2


# --- the server -------------------------------------------------------------------------------


def test_a_reference_resolves_on_the_server(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _server():
        outcome = _run(["--contract", "payments.orders"], capsys, monkeypatch)
    assert outcome.code == 0
    assert outcome.stdout.split() == ["server", "1.4.0", sha256(DOC)]


def test_version_overrides_the_reference_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _server():
        outcome = _run(
            ["--contract", "payments.orders@1.4.0", "--version", "1.3.0"], capsys, monkeypatch
        )
    assert outcome.stdout.split() == ["server", "1.3.0", sha256(OLD)]


def test_a_deprecated_version_warns_for_a_server_resolution(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _server(deprecated=True):
        outcome = _run(["--contract", "payments.orders"], capsys, monkeypatch)
    assert outcome.code == 0
    notice = "warning: payments.orders@1.4.0 is deprecated, sunset 2027-01-31"
    assert outcome.stderr.count(notice) == 1


def test_without_a_configured_url_the_server_path_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CONTRACTUM_URL")
    outcome = _run(["--contract", "payments.orders"], capsys, monkeypatch)
    assert outcome.code == 2


# --- the lock ---------------------------------------------------------------------------------


def test_frozen_uses_the_lock_and_never_resolves_latest(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _pin(tmp_path, canary_token, _server(), "payments.orders@1.3.0")
    server = _server()
    with server:
        outcome = _run(_frozen(lock_path), capsys, monkeypatch)
        paths = server.paths()
    assert outcome.code == 0
    assert outcome.stdout.split() == ["lock", "1.3.0", sha256(OLD)]
    assert paths
    assert all(path.endswith("/versions/1.3.0/metadata") for path in paths)


def test_frozen_without_a_configured_url_uses_the_server_in_the_lock(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _pin(tmp_path, canary_token, _server(), "payments.orders@1.4.0")
    monkeypatch.delenv("CONTRACTUM_URL")
    server = _server()
    with server:
        outcome = _run(_frozen(lock_path), capsys, monkeypatch)
        assert server.requests
    assert outcome.code == 0
    assert outcome.stdout.split()[0] == "lock"


def test_frozen_offline_builds_no_client(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _pin(tmp_path, canary_token, _server(), "payments.orders@1.4.0")
    monkeypatch.delenv("CONTRACTUM_URL")
    monkeypatch.delenv("CONTRACTUM_TOKEN")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a client was built")

    monkeypatch.setattr(Client, "from_settings", forbidden)
    server = FakeContractum()
    with server:
        outcome = _run(_frozen(lock_path, "--offline"), capsys, monkeypatch)
    assert outcome.code == 0
    assert outcome.stdout.split() == ["lock", "1.4.0", sha256(DOC)]
    assert server.requests == []


def test_offline_alone_reads_the_lock_too(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _pin(tmp_path, canary_token, _server(), "payments.orders@1.4.0")
    outcome = _run(
        ["--contract", "payments.orders", "--offline", "--lock", str(lock_path)],
        capsys,
        monkeypatch,
    )
    assert outcome.code == 0
    assert outcome.stdout.split()[0] == "lock"


def test_the_environment_switches_frozen_on(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _pin(tmp_path, canary_token, _server(), "payments.orders@1.4.0")
    monkeypatch.setenv("CONTRACTUM_FROZEN", "1")
    with _server():
        outcome = _run(
            ["--contract", "payments.orders", "--lock", str(lock_path)], capsys, monkeypatch
        )
    assert outcome.stdout.split()[0] == "lock"


def test_a_version_that_differs_from_the_lock_exits_2(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _pin(tmp_path, canary_token, _server(), "payments.orders@1.4.0")
    server = _server()
    with server:
        by_option = _run(_frozen(lock_path, "--version", "1.3.0"), capsys, monkeypatch)
        by_ref = _run(
            ["--contract", "payments.orders@1.3.0", "--frozen", "--lock", str(lock_path)],
            capsys,
            monkeypatch,
        )
    assert by_option.code == 2
    assert "the lock pins payments.orders@1.4.0, not 1.3.0" in by_option.stderr
    assert by_ref.code == 2
    assert "not 1.3.0" in by_ref.stderr
    assert server.requests == []


def test_a_matching_version_under_frozen_is_accepted(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _pin(tmp_path, canary_token, _server(), "payments.orders@1.4.0")
    with _server():
        outcome = _run(_frozen(lock_path, "--version", "1.4.0"), capsys, monkeypatch)
    assert outcome.code == 0


def test_a_reference_missing_from_the_lock_exits_2(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _pin(tmp_path, canary_token, _server(), "payments.orders@1.4.0")
    lock = str(lock_path)
    frozen = _run(
        ["--contract", "payments.refunds", "--frozen", "--lock", lock], capsys, monkeypatch
    )
    offline = _run(
        ["--contract", "payments.refunds", "--offline", "--lock", lock], capsys, monkeypatch
    )
    assert frozen.code == 2
    assert "not pinned in the lock" in frozen.stderr
    assert offline.code == 2
    assert "offline mode cannot resolve it" in offline.stderr


def test_frozen_without_a_lock_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run(_frozen(tmp_path / "none.lock"), capsys, monkeypatch)
    assert outcome.code == 2
    assert "none.lock" in outcome.stderr


def test_frozen_fails_when_the_server_changed_the_pinned_content(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _pin(tmp_path, canary_token, _server(), "payments.orders@1.4.0")
    changed = _server()
    changed.contracts[0].versions[0].content = DOC + b"# changed\n"
    with changed:
        outcome = _run(_frozen(lock_path), capsys, monkeypatch)
    assert outcome.code == 2
    assert "differs from the lock" in outcome.stderr


def test_a_deprecated_pinned_version_warns_for_a_lock_resolution(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(deprecated=True)
    lock_path = _pin(tmp_path, canary_token, server, "payments.orders@1.4.0")
    with server:
        outcome = _run(_frozen(lock_path), capsys, monkeypatch)
    assert outcome.code == 0
    notice = "payments.orders@1.4.0 is deprecated, sunset 2027-01-31"
    assert outcome.stderr.count(notice) == 1


# --- helpers ----------------------------------------------------------------------------------


def test_lock_server_reads_the_server_or_gives_none(tmp_path: Path, canary_token: str) -> None:
    lock_path = _pin(tmp_path, canary_token, _server(), "payments.orders")
    assert lock_server(lock_path) == BASE
    assert lock_server(tmp_path / "none.lock") is None
    broken = tmp_path / "broken.lock"
    broken.write_text("{")
    assert lock_server(broken) is None


def test_the_fallback_url_fills_only_a_missing_url(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = click.Context(click.Command("x"))
    monkeypatch.delenv("CONTRACTUM_URL")
    with client_with_fallback(ctx, BASE) as client:
        assert client.url == BASE
    ctx.obj = GlobalOptions(profile="nope")
    with pytest.raises(ConfigError):
        client_with_fallback(ctx, BASE)
    ctx.obj = GlobalOptions()
    with pytest.raises(ConfigError):
        client_with_fallback(ctx, None)


def test_the_options_appear_in_the_help(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _run(["--help"], capsys, monkeypatch)
    for flag in ("--contract", "--contract-file", "--version", "--lock", "--frozen", "--offline"):
        assert flag in outcome.stdout
