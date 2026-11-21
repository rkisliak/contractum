"""SecretStore: keyring first, a 0600 file otherwise, never a secret in a repr or message."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import keyring
import keyring.backends.fail
import keyring.core
import pytest
from pydantic import SecretStr
from support.auth import FailingKeyring, MemoryKeyring

from contractum.auth import store as store_module
from contractum.auth.store import (
    SecretStore,
    StoredCredential,
    default_data_dir,
    same_server,
)
from contractum.errors import AuthError, ConfigError

SERVER = "https://contractum.example"
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


def _credential(token: str, *, server: str = SERVER, kind: str = "pat") -> StoredCredential:
    return StoredCredential(
        kind=kind,  # type: ignore[arg-type]
        server=server,
        access_token=SecretStr(token),
        principal_label="dcp_0123456789abcdef",
    )


@pytest.fixture
def warnings() -> list[str]:
    return []


@pytest.fixture
def file_store(tmp_path: Path, warnings: list[str]) -> SecretStore:
    return SecretStore(
        path=tmp_path / "creds" / "credentials.json",
        env={"CONTRACTUM_KEYRING": "0"},
        warn_func=warnings.append,
    )


def test_a_credential_never_shows_secrets_in_repr_or_str(canary_token: str) -> None:
    credential = _credential(canary_token)
    for text in (repr(credential), str(credential), repr(credential.model_dump())):
        assert canary_token not in text


def test_file_store_round_trips_and_is_private(file_store: SecretStore, canary_token: str) -> None:
    file_store.put("work", _credential(canary_token))
    found = file_store.get("work")
    path = file_store.credentials_path
    assert found is not None
    assert found.access_token.get_secret_value() == canary_token
    assert found.principal_label == "dcp_0123456789abcdef"
    assert json.loads(path.read_text())["work"]["access_token"] == canary_token
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert file_store.get("other") is None


def test_the_file_holds_one_entry_per_profile(file_store: SecretStore) -> None:
    file_store.put("a", _credential("tok-a"))
    file_store.put("b", _credential("tok-b"))
    file_store.put("a", _credential("tok-a2"))
    entries = json.loads(file_store.credentials_path.read_text())
    assert sorted(entries) == ["a", "b"]
    assert entries["a"]["access_token"] == "tok-a2"


def test_keyring_is_used_when_available_and_no_file_is_written(
    tmp_path: Path, canary_token: str
) -> None:
    backend = MemoryKeyring()
    store = SecretStore(backend=backend, path=tmp_path / "credentials.json", env={})
    store.put("work", _credential(canary_token))
    assert ("contractum", "work") in backend.entries
    assert not (tmp_path / "credentials.json").exists()
    assert store.get("work") is not None
    assert store.location() == "the OS keyring"
    assert store.delete("work") is True
    assert store.get("work") is None
    assert store.delete("work") is False


def test_a_keyring_write_clears_a_stale_file_entry(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    SecretStore(path=path, env={"CONTRACTUM_KEYRING": "0"}).put("work", _credential("old"))
    backend = MemoryKeyring()
    store = SecretStore(backend=backend, path=path, env={})
    store.put("work", _credential("new"))
    assert json.loads(path.read_text()) == {}
    found = store.get("work")
    assert found is not None
    assert found.access_token.get_secret_value() == "new"


def test_a_failing_keyring_falls_back_to_the_file_with_one_warning(
    tmp_path: Path, warnings: list[str]
) -> None:
    path = tmp_path / "credentials.json"
    store = SecretStore(backend=FailingKeyring(), path=path, env={}, warn_func=warnings.append)
    store.put("work", _credential("SECRETVALUE"))
    found = store.get("work")
    assert found is not None
    assert found.access_token.get_secret_value() == "SECRETVALUE"
    assert len(warnings) == 1
    assert str(path) in warnings[0]
    assert "SECRETVALUE" not in warnings[0]


def test_the_fail_backend_counts_as_no_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warnings: list[str]
) -> None:
    monkeypatch.setattr(keyring.core, "_keyring_backend", keyring.backends.fail.Keyring())
    store = SecretStore(path=tmp_path / "credentials.json", env={}, warn_func=warnings.append)
    store.put("work", _credential("tok"))
    assert (tmp_path / "credentials.json").exists()
    assert store.location() == str(tmp_path / "credentials.json")
    assert len(warnings) == 1


def test_a_plaintext_alt_backend_counts_as_no_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PlaintextKeyring(MemoryKeyring):
        pass

    PlaintextKeyring.__module__ = "keyrings.alt.file"
    monkeypatch.setattr(keyring.core, "_keyring_backend", PlaintextKeyring())
    store = SecretStore(path=tmp_path / "credentials.json", env={}, warn_func=lambda _: None)
    store.put("work", _credential("tok"))
    assert (tmp_path / "credentials.json").exists()


def test_a_real_discovered_backend_is_used_through_the_keyring_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = MemoryKeyring()
    monkeypatch.setattr(keyring.core, "_keyring_backend", memory)
    store = SecretStore(path=tmp_path / "credentials.json", env={})
    store.put("work", _credential("tok"))
    assert ("contractum", "work") in memory.entries


def test_keyring_required_fails_without_a_usable_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(keyring.core, "_keyring_backend", keyring.backends.fail.Keyring())
    store = SecretStore(path=tmp_path / "credentials.json", env={"CONTRACTUM_KEYRING": "1"})
    with pytest.raises(ConfigError, match="no OS keyring"):
        store.put("work", _credential("tok"))


def test_keyring_required_fails_when_the_keyring_refuses(tmp_path: Path) -> None:
    store = SecretStore(
        backend=FailingKeyring(),
        path=tmp_path / "credentials.json",
        env={"CONTRACTUM_KEYRING": "1"},
    )
    with pytest.raises(ConfigError, match="refused"):
        store.put("work", _credential("tok"))
    assert not (tmp_path / "credentials.json").exists()


def test_keyring_required_get_does_not_read_the_file(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    SecretStore(path=path, env={"CONTRACTUM_KEYRING": "0"}).put("work", _credential("tok"))
    store = SecretStore(backend=MemoryKeyring(), path=path, env={"CONTRACTUM_KEYRING": "1"})
    assert store.get("work") is None


def test_a_keyring_read_failure_falls_back_to_the_file(tmp_path: Path, warnings: list[str]) -> None:
    path = tmp_path / "credentials.json"
    SecretStore(path=path, env={"CONTRACTUM_KEYRING": "0"}).put("work", _credential("tok"))
    store = SecretStore(backend=FailingKeyring(), path=path, env={}, warn_func=warnings.append)
    assert store.get("work") is not None
    assert len(warnings) == 1


@posix_only
def test_a_file_readable_by_others_is_refused_with_a_chmod_hint(
    file_store: SecretStore,
) -> None:
    file_store.put("work", _credential("SECRETVALUE"))
    path = file_store.credentials_path
    path.chmod(0o644)
    with pytest.raises(ConfigError) as raised:
        file_store.get("work")
    assert raised.value.hint == f"run: chmod 600 {path}"
    assert "SECRETVALUE" not in str(raised.value)


@posix_only
def test_a_group_readable_file_is_refused(file_store: SecretStore) -> None:
    file_store.put("work", _credential("tok"))
    file_store.credentials_path.chmod(0o640)
    with pytest.raises(ConfigError, match="readable by other users"):
        file_store.get("work")


def test_damaged_files_and_entries_are_reported_without_content(
    file_store: SecretStore,
) -> None:
    path = file_store.credentials_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json secret-ish")
    path.chmod(0o600)
    with pytest.raises(ConfigError, match="could not be read") as raised:
        file_store.get("work")
    assert "secret-ish" not in str(raised.value)
    path.write_text("[1, 2]")
    with pytest.raises(ConfigError, match="not a JSON object"):
        file_store.get("work")
    path.write_text(json.dumps({"work": {"kind": "bogus"}}))
    with pytest.raises(AuthError, match="cannot be used") as unusable:
        file_store.get("work")
    assert unusable.value.hint == "run `contractum login --token -`"
    assert file_store.delete("work") is True
    assert file_store.get("work") is None


@pytest.mark.parametrize("kind", ["local_session", "entra_user"])
def test_an_entry_of_a_retired_kind_cannot_be_used_and_can_be_deleted(
    tmp_path: Path, kind: str
) -> None:
    backend = MemoryKeyring()
    backend.entries[("contractum", "work")] = json.dumps(
        {"kind": kind, "server": SERVER, "access_token": "a", "refresh_token": "r"}
    )
    store = SecretStore(backend=backend, path=tmp_path / "credentials.json", env={})
    with pytest.raises(AuthError, match="cannot be used"):
        store.get("work")
    assert store.delete("work") is True
    assert backend.entries == {}


def test_extra_fields_of_an_older_entry_are_ignored_and_never_written_back(
    file_store: SecretStore,
) -> None:
    path = file_store.credentials_path
    path.parent.mkdir(parents=True, exist_ok=True)
    old = {"kind": "pat", "server": SERVER, "access_token": "a", "expires_at": 1.0}
    path.write_text(json.dumps({"work": old}))
    path.chmod(0o600)
    found = file_store.get("work")
    assert found is not None
    file_store.put("work", found)
    assert "expires_at" not in json.loads(path.read_text())["work"]


def test_an_oversized_file_is_refused(
    file_store: SecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_store.put("work", _credential("tok"))
    monkeypatch.setattr(store_module, "MAX_STORE_BYTES", 5)
    with pytest.raises(ConfigError, match="too large"):
        file_store.get("work")


def test_a_damaged_keyring_entry_is_reported(tmp_path: Path) -> None:
    backend = MemoryKeyring()
    backend.entries[("contractum", "work")] = "not json at all"
    store = SecretStore(backend=backend, path=tmp_path / "credentials.json", env={})
    with pytest.raises(AuthError, match="cannot be used"):
        store.get("work")


def test_an_interrupted_write_keeps_the_previous_credential(
    file_store: SecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_store.put("work", _credential("old-token"))

    def boom(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        file_store.put("work", _credential("new-token"))
    monkeypatch.undo()
    found = file_store.get("work")
    assert found is not None
    assert found.access_token.get_secret_value() == "old-token"
    leftovers = [p.name for p in file_store.credentials_path.parent.iterdir()]
    assert not any(name.endswith(".tmp") for name in leftovers)


def test_delete_removes_the_file_entry_and_keeps_the_others(file_store: SecretStore) -> None:
    file_store.put("a", _credential("tok-a"))
    file_store.put("b", _credential("tok-b"))
    assert file_store.delete("a") is True
    assert file_store.get("a") is None
    assert file_store.get("b") is not None
    assert file_store.delete("missing") is False


def test_delete_without_any_file_is_false(tmp_path: Path) -> None:
    store = SecretStore(path=tmp_path / "nothing.json", env={"CONTRACTUM_KEYRING": "0"})
    assert store.delete("work") is False


def test_a_keyring_entry_that_cannot_be_deleted_counts_as_not_removed(tmp_path: Path) -> None:
    class StubbornKeyring(MemoryKeyring):
        def delete_password(self, service_name: str, username: str) -> None:
            from keyring.errors import PasswordDeleteError

            raise PasswordDeleteError("no")

    backend = StubbornKeyring()
    backend.entries[("contractum", "work")] = "x"
    store = SecretStore(backend=backend, path=tmp_path / "c.json", env={})
    assert store.delete("work") is False


def test_a_locked_file_reports_the_lock(
    file_store: SecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import filelock

    monkeypatch.setattr(store_module, "LOCK_TIMEOUT_SECONDS", 0.05)
    file_store.credentials_path.parent.mkdir(parents=True, exist_ok=True)
    with filelock.FileLock(f"{file_store.credentials_path}.lock"):
        with pytest.raises(ConfigError, match="locked by another process"):
            file_store.put("work", _credential("tok"))


def test_data_dir_follows_the_environment_then_the_platform(tmp_path: Path) -> None:
    assert default_data_dir({"CONTRACTUM_DATA_DIR": str(tmp_path / "d")}) == tmp_path / "d"
    assert default_data_dir({}).name.lower().startswith("contractum")
    store = SecretStore(env={"CONTRACTUM_DATA_DIR": str(tmp_path / "d")})
    assert store.data_dir == tmp_path / "d"
    assert store.credentials_path == tmp_path / "d" / "credentials.json"


def test_same_server_ignores_case_and_a_trailing_slash() -> None:
    assert same_server("https://Contractum.example/", "https://contractum.example")
    assert not same_server("https://a.example", "https://b.example")
