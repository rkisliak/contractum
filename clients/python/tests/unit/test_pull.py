"""``pull``: contract files named safely, and a canonical, idempotent contractum.lock."""

from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from support.auth import Outcome, run_main
from support.fake_server import FakeContractum, FakeVersion, sha256
from support.locks import record_guards_during_writes

from contractum import lock as lock_module
from contractum.client import Client
from contractum.contracts import ContractDeprecationWarning
from contractum.errors import FrozenLockError, OfflineError, ServerError, UsageError
from contractum.pull import PullResult, pull, safe_filename

BASE = "https://contractum.example"
DOC = b"apiVersion: v3.0.2\nkind: DataContract\nid: payments.orders\nversion: 1.4.0\n"
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
HOSTILE = [
    "../../etc/passwd",
    "..\\..\\windows\\system32",
    "CON",
    "con.txt",
    "NUL",
    "aux.v1",
    "COM1",
    "lpt9.x",
    "a" * 255,
    "/absolute/path",
    "C:\\temp\\x",
    "...",
    "---",
    "",
    "   ",
    "\x00evil",
    "id with spaces",
    "UPPER.Case",
    ".hidden",
    "_leading",
]


def _server() -> FakeContractum:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [
            FakeVersion("1.4.0", DOC, status="latest"),
            FakeVersion("1.3.0", DOC + b"# older\n"),
        ],
        name="Orders",
    )
    return server


def _client(token: str) -> Client:
    return Client(BASE, token=token)


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, canary_token: str) -> None:
    monkeypatch.setenv("CONTRACTUM_URL", BASE)
    monkeypatch.setenv("CONTRACTUM_TOKEN", canary_token)


# --- the tracer -------------------------------------------------------------------------------


def test_pull_writes_the_served_bytes_and_a_canonical_lock(
    tmp_path: Path, canary_token: str
) -> None:
    directory = tmp_path / "contracts"
    lock_path = tmp_path / "contractum.lock"
    with _server() as server, _client(canary_token) as client:
        result = client.pull(["payments.orders@1.4.0"], directory=directory, lock_path=lock_path)
        contract_id = server.contracts[0].contract_id
    (item,) = result.contracts
    expected = safe_filename("payments.orders", "1.4.0", contract_id, "application/yaml")
    assert item.path == directory / expected
    assert (directory / expected).read_bytes() == DOC
    raw = lock_path.read_bytes()
    body = json.loads(raw)
    assert raw == (json.dumps(body, sort_keys=True, indent=2) + "\n").encode()
    assert body["lock_version"] == 1
    assert body["server"] == BASE
    (entry,) = body["contracts"]
    assert entry["content_sha256"] == sha256(DOC)
    assert entry["semver"] == "1.4.0"
    assert entry["requested"] == "1.4.0"
    assert entry["ref"] == "payments.orders"
    assert entry["contract_id"] == contract_id
    assert entry["media_type"] == "application/yaml"
    assert entry["file"] == f"contracts/{expected}"
    assert set(body) == {"lock_version", "server", "contracts"}
    assert result.failed is False


def test_a_second_identical_pull_leaves_the_lock_byte_equal(
    tmp_path: Path, canary_token: str
) -> None:
    lock_path = tmp_path / "contractum.lock"
    with _server(), _client(canary_token) as client:
        client.pull(["payments.orders"], directory=tmp_path / "c", lock_path=lock_path)
        first = lock_path.read_bytes()
        client.pull(["payments.orders"], directory=tmp_path / "c", lock_path=lock_path)
    assert lock_path.read_bytes() == first


def test_the_lock_artefact_stays_out_of_the_repository(
    tmp_path: Path, canary_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    locks = tmp_path / "cache" / "locks"
    seen = record_guards_during_writes(monkeypatch, lock_module, lambda _: locks)
    with _server(), _client(canary_token) as client:
        client.pull(["payments.orders"], directory=repo / "c", lock_path=repo / "contractum.lock")
    assert sorted(path.name for path in repo.iterdir()) == ["c", "contractum.lock"]
    assert len(seen) == 1
    assert len(seen[0]) == 1
    # filelock keeps the guard after release on POSIX and deletes it on Windows.
    assert {p.name for p in locks.iterdir()} <= set(seen[0])


def test_pull_merges_into_an_existing_lock_and_replaces_the_same_request(
    tmp_path: Path, canary_token: str
) -> None:
    lock_path = tmp_path / "contractum.lock"
    with _server(), _client(canary_token) as client:
        client.pull(["payments.orders@1.3.0"], directory=tmp_path / "c", lock_path=lock_path)
        client.pull(["payments.orders@1.4.0"], directory=tmp_path / "c", lock_path=lock_path)
        client.pull(["payments.orders@1.4.0"], directory=tmp_path / "c", lock_path=lock_path)
    versions = [e["semver"] for e in json.loads(lock_path.read_text())["contracts"]]
    assert versions == ["1.3.0", "1.4.0"]


def test_pull_with_no_refs_re_resolves_each_lock_entry(tmp_path: Path, canary_token: str) -> None:
    lock_path = tmp_path / "contractum.lock"
    server = _server()
    with server, _client(canary_token) as client:
        client.pull(
            ["payments.orders@1.3.0", "payments.orders"],
            directory=tmp_path / "c",
            lock_path=lock_path,
        )
        before = json.loads(lock_path.read_text())["contracts"]
        contract = server.contracts[0]
        contract.versions[0].status = "older"
        contract.versions.append(FakeVersion("1.5.0", DOC + b"# next\n", status="latest"))
        result = client.pull(directory=tmp_path / "c", lock_path=lock_path)
    after = {(e["requested"]): e["semver"] for e in json.loads(lock_path.read_text())["contracts"]}
    assert {e["requested"]: e["semver"] for e in before} == {"1.3.0": "1.3.0", "latest": "1.4.0"}
    assert after == {"1.3.0": "1.3.0", "latest": "1.5.0"}
    assert len(result.contracts) == 2


def test_pull_with_no_refs_and_no_lock_is_a_usage_error(tmp_path: Path, canary_token: str) -> None:
    with _server(), _client(canary_token) as client, pytest.raises(UsageError) as raised:
        client.pull(directory=tmp_path / "c", lock_path=tmp_path / "contractum.lock")
    assert "no lock" in str(raised.value)


def test_a_lock_of_another_server_is_not_merged(tmp_path: Path, canary_token: str) -> None:
    lock_path = tmp_path / "contractum.lock"
    with _server(), _client(canary_token) as client:
        client.pull(["payments.orders"], directory=tmp_path / "c", lock_path=lock_path)
    body = json.loads(lock_path.read_text())
    body["server"] = "https://other.example"
    lock_path.write_text(json.dumps(body))
    with _server(), _client(canary_token) as client, pytest.raises(UsageError) as raised:
        client.pull(["payments.orders"], directory=tmp_path / "c", lock_path=lock_path)
    assert "other.example" in str(raised.value)


def test_a_missing_contract_id_from_the_server_is_a_server_error(
    tmp_path: Path, canary_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contractum import pull as pull_module
    from contractum.contracts import ContractVersion

    bare = ContractVersion(
        contract_id=None,
        odcs_id=None,
        name=None,
        semver=None,
        status=None,
        media_type="application/yaml",
        content=DOC,
        content_sha256=sha256(DOC),
        etag=None,
    )
    with pytest.raises(ServerError):
        pull_module._entry("x", "latest", bare, tmp_path / "x.yaml", tmp_path)


def test_a_relative_path_on_another_drive_falls_back_to_the_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contractum import pull as pull_module

    def refuse(path: str, start: str) -> str:
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", refuse)
    target = tmp_path / "a.yaml"
    assert pull_module._relative(target, tmp_path) == target.resolve().as_posix()


def test_pull_needs_a_client_unless_offline(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        pull(None, ["payments.orders"], directory=tmp_path, lock_path=tmp_path / "x.lock")


def test_a_symlink_pointing_out_of_the_directory_is_refused(
    tmp_path: Path, canary_token: str
) -> None:
    directory = tmp_path / "contracts"
    directory.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(b"untouched")
    server = _server()
    contract_id = server.contracts[0].contract_id
    name = safe_filename("payments.orders", "1.4.0", contract_id, "application/yaml")
    try:
        (directory / name).symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are not available here")
    with server, _client(canary_token) as client, pytest.raises(UsageError):
        client.pull(
            ["payments.orders"], directory=directory, lock_path=tmp_path / "contractum.lock"
        )
    assert outside.read_bytes() == b"untouched"
    assert not (tmp_path / "contractum.lock").exists()


# --- file names -------------------------------------------------------------------------------


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_ids_give_safe_names(hostile: str) -> None:
    name = safe_filename(
        hostile, "1.0.0+build.7", "0191a2b3-c4d5-4e6f-8091-a2b3c4d5e6f7", "application/json"
    )
    assert NAME.fullmatch(name)
    assert len(name) < 160
    assert name.endswith(".json")
    assert name.split(".")[0].lower() not in {"con", "prn", "aux", "nul", "com1", "lpt9"}


def test_a_name_has_the_documented_parts() -> None:
    name = safe_filename(
        "Payments.Orders", "1.4.0", "0191A2B3-c4d5-4e6f-8091-a2b3c4d5e6f7", "application/yaml"
    )
    assert name == "payments.orders-1.4.0-0191a2b3.yaml"


def test_an_empty_id_becomes_contract() -> None:
    assert safe_filename("%%%", "1.0.0", "", "application/yaml") == "contract-1.0.0-0.yaml"
    assert safe_filename(None, None, "", "application/yaml") == "contract-0-0.yaml"


def test_a_windows_device_name_is_prefixed() -> None:
    assert safe_filename("con.x", "1.0.0", "ab", "application/yaml").startswith("id-con.x-")
    assert safe_filename("NUL", "1.0.0", "ab", "application/yaml").startswith("id-nul-")


@given(
    odcs_id=st.one_of(st.text(max_size=300), st.sampled_from(HOSTILE)),
    semver=st.one_of(st.text(max_size=80), st.just("1.2.3-rc.1+b")),
    contract_id=st.text(max_size=60),
    media=st.sampled_from(["application/yaml", "application/json", "text/yaml", ""]),
)
def test_any_input_stays_inside_the_target_directory(
    odcs_id: str, semver: str, contract_id: str, media: str
) -> None:
    name = safe_filename(odcs_id, semver, contract_id, media)
    assert NAME.fullmatch(name)
    assert "/" not in name
    assert "\\" not in name
    assert name not in (".", "..")
    base = Path("target")
    assert (base / name).parent == base
    assert len(name) <= 140


# --- the CLI ----------------------------------------------------------------------------------


def test_the_pull_command_writes_files_and_the_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "contractum.lock"
    with _server():
        outcome = run_main(
            [
                "pull",
                "payments.orders@1.4.0",
                "--dir",
                str(tmp_path / "contracts"),
                "--lock",
                str(lock_path),
                "--json",
            ],
            capsys=capsys,
            monkeypatch=monkeypatch,
        )
    assert outcome.code == 0
    summary = json.loads(outcome.stdout)
    (item,) = summary["contracts"]
    assert item["semver"] == "1.4.0"
    assert item["content_sha256"] == sha256(DOC)
    assert Path(item["file"]).read_bytes() == DOC
    assert summary["lock"] == str(lock_path)
    assert "pinned payments.orders@1.4.0" in outcome.stderr
    assert json.loads(lock_path.read_text())["contracts"][0]["file"].startswith("contracts/")


def test_the_pull_command_without_refs_or_lock_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _server():
        outcome = run_main(
            ["pull", "--lock", str(tmp_path / "none.lock")], capsys=capsys, monkeypatch=monkeypatch
        )
    assert outcome.code == 2
    assert "no references" in outcome.stderr


# --- frozen and offline -----------------------------------------------------------------------


def _pin(tmp_path: Path, token: str, server: FakeContractum | None = None) -> FakeContractum:
    chosen = server or _server()
    with chosen, _client(token) as client:
        client.pull(
            ["payments.orders@1.4.0"],
            directory=tmp_path / "contracts",
            lock_path=tmp_path / "contractum.lock",
        )
    return chosen


def _frozen(
    tmp_path: Path, client: Client | None, refs: tuple[str, ...] | list[str] = (), **kwargs: Any
) -> PullResult:
    return pull(
        client,
        refs,
        directory=tmp_path / "contracts",
        lock_path=tmp_path / "contractum.lock",
        **kwargs,
    )


def _document(tmp_path: Path) -> Path:
    (path,) = (tmp_path / "contracts").iterdir()
    return path


def test_frozen_online_verifies_and_never_resolves_latest(
    tmp_path: Path, canary_token: str
) -> None:
    server = _pin(tmp_path, canary_token)
    with server, _client(canary_token) as client:
        result = _frozen(tmp_path, client, frozen=True)
        paths = server.paths()
    (item,) = result.contracts
    assert item.contract.source == "lock"
    assert item.contract.content == DOC
    assert item.contract.semver == "1.4.0"
    assert paths
    assert not any(path.endswith("/versions") for path in paths)
    assert not any("/versions/latest" in path for path in paths)
    assert all(path.endswith("/versions/1.4.0/metadata") for path in paths)


def test_frozen_fails_when_the_server_content_changed(tmp_path: Path, canary_token: str) -> None:
    server = _pin(tmp_path, canary_token)
    server.contracts[0].versions[0].content = DOC + b"# changed on the server\n"
    with server, _client(canary_token) as client, pytest.raises(FrozenLockError) as raised:
        _frozen(tmp_path, client, frozen=True)
    assert "differs from the lock" in str(raised.value)


def test_frozen_fails_when_the_server_answers_another_version(
    tmp_path: Path, canary_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _pin(tmp_path, canary_token)
    with server, _client(canary_token) as client:
        real = client.version_metadata
        monkeypatch.setattr(
            client,
            "version_metadata",
            lambda cid, ref="latest": real(cid, ref).model_copy(update={"semver": "9.9.9"}),
        )
        with pytest.raises(FrozenLockError) as raised:
            _frozen(tmp_path, client, frozen=True)
    assert "9.9.9" in str(raised.value)


def test_frozen_fails_for_a_reference_missing_from_the_lock(
    tmp_path: Path, canary_token: str
) -> None:
    server = _pin(tmp_path, canary_token)
    with server, _client(canary_token) as client, pytest.raises(FrozenLockError) as raised:
        _frozen(tmp_path, client, refs=["payments.refunds"], frozen=True)
    assert "payments.refunds" in str(raised.value)
    assert server.requests == []


def test_frozen_with_a_tampered_pulled_file_fails_loudly(tmp_path: Path, canary_token: str) -> None:
    server = _pin(tmp_path, canary_token)
    _document(tmp_path).write_bytes(DOC + b"# edited\n")
    with server, _client(canary_token) as client, pytest.raises(FrozenLockError) as raised:
        _frozen(tmp_path, client, frozen=True)
    assert "pulled file" in str(raised.value)
    assert "does not match the lock" in str(raised.value)


def test_frozen_offline_needs_no_url_and_makes_no_request(
    tmp_path: Path, canary_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin(tmp_path, canary_token)
    monkeypatch.delenv("CONTRACTUM_URL")
    monkeypatch.delenv("CONTRACTUM_TOKEN")
    server = FakeContractum()
    with server:
        result = _frozen(tmp_path, None, frozen=True, offline=True)
    assert server.requests == []
    assert result.contracts[0].contract.content == DOC


def test_the_offline_command_succeeds_with_no_url_configured(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin(tmp_path, canary_token)
    monkeypatch.delenv("CONTRACTUM_URL")
    monkeypatch.delenv("CONTRACTUM_TOKEN")
    server = FakeContractum()
    with server:
        outcome = run_main(
            [
                "pull",
                "--frozen",
                "--offline",
                "--dir",
                str(tmp_path / "contracts"),
                "--lock",
                str(tmp_path / "contractum.lock"),
            ],
            capsys=capsys,
            monkeypatch=monkeypatch,
        )
    assert outcome.code == 0
    assert server.requests == []
    assert "pinned payments.orders@1.4.0" in outcome.stderr


def test_offline_restores_a_missing_file_from_the_cache(tmp_path: Path, canary_token: str) -> None:
    _pin(tmp_path, canary_token)
    document = _document(tmp_path)
    document.unlink()
    result = _frozen(tmp_path, None, offline=True)
    assert document.read_bytes() == DOC
    contract = result.contracts[0].contract
    assert contract.name == "Orders"
    assert contract.status == "latest"


def test_offline_without_the_file_or_the_cache_is_an_offline_error(
    tmp_path: Path, canary_token: str
) -> None:
    _pin(tmp_path, canary_token)
    _document(tmp_path).unlink()
    for child in (tmp_path / "cache" / "v1").rglob("document"):
        child.unlink()
    with pytest.raises(OfflineError) as raised:
        _frozen(tmp_path, None, frozen=True, offline=True)
    assert "neither" in str(raised.value)


def test_offline_for_a_reference_that_is_not_pinned_is_an_offline_error(
    tmp_path: Path, canary_token: str
) -> None:
    _pin(tmp_path, canary_token)
    with pytest.raises(OfflineError) as raised:
        _frozen(tmp_path, None, refs=["payments.refunds"], offline=True)
    assert "cannot resolve it" in str(raised.value)


def test_frozen_online_fetches_by_exact_version_when_file_and_cache_are_gone(
    tmp_path: Path, canary_token: str
) -> None:
    server = _pin(tmp_path, canary_token)
    document = _document(tmp_path)
    document.unlink()
    for child in (tmp_path / "cache" / "v1").rglob("document"):
        child.unlink()
    with server, _client(canary_token) as client:
        result = _frozen(tmp_path, client, frozen=True)
        paths = server.paths()
    assert document.read_bytes() == DOC
    assert result.contracts[0].contract.content == DOC
    assert any(path.endswith("/versions/1.4.0") for path in paths)
    assert not any("/versions/latest" in path for path in paths)


def test_a_server_that_sends_no_document_is_a_server_error(
    tmp_path: Path, canary_token: str
) -> None:
    server = _pin(tmp_path, canary_token)
    _document(tmp_path).unlink()
    for child in (tmp_path / "cache" / "v1").rglob("document"):
        child.unlink()
    server.contracts[0].versions[0].not_modified = True
    with server, _client(canary_token) as client, pytest.raises(ServerError):
        _frozen(tmp_path, client, frozen=True)


def test_a_cached_copy_that_differs_from_the_lock_fails_loudly(
    tmp_path: Path, canary_token: str
) -> None:
    _pin(tmp_path, canary_token)
    lock_path = tmp_path / "contractum.lock"
    body = json.loads(lock_path.read_text())
    body["contracts"][0]["content_sha256"] = "b" * 64
    lock_path.write_text(json.dumps(body))
    with pytest.raises(FrozenLockError) as raised:
        _frozen(tmp_path, None, offline=True)
    assert "cached copy" in str(raised.value)


def test_a_file_outside_the_target_is_never_restored(tmp_path: Path, canary_token: str) -> None:
    _pin(tmp_path, canary_token)
    lock_path = tmp_path / "contractum.lock"
    body = json.loads(lock_path.read_text())
    body["contracts"][0]["file"] = "../../elsewhere/x.yaml"
    lock_path.write_text(json.dumps(body))
    _document(tmp_path).unlink()
    result = _frozen(tmp_path, None, offline=True)
    assert result.contracts[0].contract.content == DOC
    assert not (tmp_path.parent.parent / "elsewhere").exists()


def test_an_unreadable_cache_falls_back_to_the_pulled_file(
    tmp_path: Path, canary_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contractum.cache import Cache

    _pin(tmp_path, canary_token)

    def broken(*args: object, **kwargs: object) -> None:
        raise PermissionError("no access")

    monkeypatch.setattr(Cache, "get", broken)
    result = _frozen(tmp_path, None, offline=True)
    contract = result.contracts[0].contract
    assert contract.content == DOC
    assert contract.name is None


def test_an_interrupted_pull_keeps_the_previous_lock_and_its_files(
    tmp_path: Path, canary_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _pin(tmp_path, canary_token)
    lock_path = tmp_path / "contractum.lock"
    before = lock_path.read_bytes()
    first_file = _document(tmp_path)
    real = os.replace
    calls = {"count": 0}

    def fail_on_the_lock(source: str, target: str) -> None:
        if str(target).endswith("contractum.lock"):
            raise OSError("interrupted")
        calls["count"] += 1
        real(source, target)

    monkeypatch.setattr(os, "replace", fail_on_the_lock)
    with server, _client(canary_token) as client, pytest.raises(OSError, match="interrupted"):
        client.pull(
            ["payments.orders@1.3.0"],
            directory=tmp_path / "contracts",
            lock_path=lock_path,
        )
    monkeypatch.undo()
    assert lock_path.read_bytes() == before
    assert first_file.read_bytes() == DOC
    assert calls["count"] >= 1


def test_a_deprecated_version_warns_once_on_pull_and_on_frozen_reads(
    tmp_path: Path, canary_token: str
) -> None:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [FakeVersion("1.4.0", DOC, status="latest", deprecated=True, sunset_date="2027-01-31")],
    )
    with server, _client(canary_token) as client:
        with pytest.warns(ContractDeprecationWarning) as pulled:
            client.pull(
                ["payments.orders"],
                directory=tmp_path / "contracts",
                lock_path=tmp_path / "contractum.lock",
            )
        with pytest.warns(ContractDeprecationWarning) as frozen:
            client.pull(
                directory=tmp_path / "contracts",
                lock_path=tmp_path / "contractum.lock",
                frozen=True,
            )
    assert [str(w.message) for w in pulled] == [
        "payments.orders@1.4.0 is deprecated, sunset 2027-01-31"
    ]
    assert len(frozen) == 1


# --- lifecycle policy on the command ----------------------------------------------------------


def _deprecated(sunset: date | None) -> FakeContractum:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [
            FakeVersion(
                "1.4.0",
                DOC,
                status="latest",
                deprecated=True,
                sunset_date=sunset.isoformat() if sunset else None,
            )
        ],
    )
    return server


def _pull_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *flags: str,
) -> Outcome:
    return run_main(
        [
            "pull",
            "payments.orders",
            "--dir",
            str(tmp_path / "contracts"),
            "--lock",
            str(tmp_path / "contractum.lock"),
            *flags,
        ],
        capsys=capsys,
        monkeypatch=monkeypatch,
    )


def test_a_deprecated_version_only_warns_without_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _deprecated(date(2027, 1, 31)):
        outcome = _pull_command(tmp_path, capsys, monkeypatch)
    assert outcome.code == 0
    assert (
        outcome.stderr.count("warning: payments.orders@1.4.0 is deprecated, sunset 2027-01-31") == 1
    )
    assert "refused" not in outcome.stderr


def test_fail_on_deprecated_exits_1_after_writing_files_and_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _deprecated(date(2027, 1, 31)):
        outcome = _pull_command(tmp_path, capsys, monkeypatch, "--fail-on-deprecated", "--json")
    assert outcome.code == 1
    assert "deprecated is refused" in outcome.stderr
    assert (tmp_path / "contractum.lock").exists()
    (document,) = (tmp_path / "contracts").iterdir()
    assert document.read_bytes() == DOC
    summary = json.loads(outcome.stdout)
    assert summary["contracts"][0]["lifecycle"]["status"] == "failed"
    assert summary["contracts"][0]["deprecated"] is True


def test_sunset_within_exits_1_when_the_sunset_is_near(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _deprecated(date.today() + timedelta(days=10)):
        outcome = _pull_command(tmp_path, capsys, monkeypatch, "--sunset-within", "30")
    assert outcome.code == 1
    assert "sunsets" in outcome.stderr
    assert (tmp_path / "contractum.lock").exists()
    assert len(list((tmp_path / "contracts").iterdir())) == 1


def test_sunset_within_passes_when_the_sunset_is_far(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _deprecated(date.today() + timedelta(days=90)):
        outcome = _pull_command(tmp_path, capsys, monkeypatch, "--sunset-within", "30")
    assert outcome.code == 0


def test_a_live_version_ignores_the_policy_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _server():
        outcome = _pull_command(
            tmp_path,
            capsys,
            monkeypatch,
            "--fail-on-deprecated",
            "--sunset-within",
            "365",
            "--json",
        )
    assert outcome.code == 0
    assert json.loads(outcome.stdout)["contracts"][0]["lifecycle"] is None


def test_the_frozen_command_uses_the_server_named_in_the_lock(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin(tmp_path, canary_token)
    monkeypatch.delenv("CONTRACTUM_URL")
    server = _server()
    with server:
        outcome = _pull_command(tmp_path, capsys, monkeypatch, "--frozen")
        paths = server.paths()
    assert outcome.code == 0
    assert paths
    assert not any("/versions/latest" in path for path in paths)


def test_the_frozen_command_exits_2_when_the_server_changed(
    tmp_path: Path,
    canary_token: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin(tmp_path, canary_token)
    changed = _server()
    changed.contracts[0].versions[0].content = DOC + b"# drift\n"
    with changed:
        outcome = run_main(
            [
                "pull",
                "--frozen",
                "--dir",
                str(tmp_path / "contracts"),
                "--lock",
                str(tmp_path / "contractum.lock"),
            ],
            capsys=capsys,
            monkeypatch=monkeypatch,
        )
    assert outcome.code == 2
    assert "differs from the lock" in outcome.stderr


def test_read_locked_without_a_server_skips_the_cache(tmp_path: Path, canary_token: str) -> None:
    from contractum.cache import Cache
    from contractum.lock import load_lock
    from contractum.pull import read_locked

    _pin(tmp_path, canary_token)
    entry = load_lock(tmp_path / "contractum.lock").contracts[0]
    contract = read_locked(entry, lock_dir=tmp_path, cache=Cache(), client=None)
    assert contract.content == DOC
    assert contract.name is None
    assert contract.source == "lock"


def test_pull_uses_an_explicit_cache_root(
    tmp_path: Path, canary_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contractum.cache import Cache

    explicit = Cache(tmp_path / "own-cache")
    seen = record_guards_during_writes(
        monkeypatch, lock_module, lambda _: tmp_path / "own-cache" / "locks"
    )
    with _server(), _client(canary_token) as client:
        pull(
            client,
            ["payments.orders"],
            directory=tmp_path / "c",
            lock_path=tmp_path / "contractum.lock",
            cache=explicit,
        )
    assert len(seen) == 1
    assert len(seen[0]) == 1
