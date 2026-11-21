"""contractum.lock: strict reading, canonical writing, lookup and crash safety."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from contractum.errors import FrozenLockError, UsageError
from contractum.lock import LockEntry, LockFile, load_lock, render_lock, save_lock

CID_A = "0191a2b3-c4d5-4e6f-8091-a2b3c4d5e6f7"
CID_B = "0191a2b3-c4d5-4e6f-8091-a2b3c4d5e6f8"
SHA = "a" * 64


def _entry(**changes: Any) -> LockEntry:
    fields: dict[str, Any] = {
        "ref": "payments.orders",
        "requested": "latest",
        "contract_id": CID_A,
        "odcs_id": "payments.orders",
        "folder_path": None,
        "semver": "1.4.0",
        "content_sha256": SHA,
        "media_type": "application/yaml",
        "file": "contracts/payments.orders-1.4.0-0191a2b3.yaml",
    }
    fields.update(changes)
    return LockEntry(**fields)


def _lock(*entries: LockEntry) -> LockFile:
    return LockFile(server="https://contractum.example", contracts=list(entries))


# --- reading ----------------------------------------------------------------------------------


def test_a_lock_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "contractum.lock"
    original = _lock(_entry(), _entry(ref="b.c", odcs_id="b.c", contract_id=CID_B))
    save_lock(path, original)
    loaded = load_lock(path)
    assert loaded.lock_version == 1
    assert [e.ref for e in loaded.contracts] == ["b.c", "payments.orders"]
    assert loaded.server == original.server


def test_the_rendering_is_canonical_and_sorted() -> None:
    first = render_lock(_lock(_entry(ref="z"), _entry(ref="a", contract_id=CID_B)))
    second = render_lock(_lock(_entry(ref="a", contract_id=CID_B), _entry(ref="z")))
    assert first == second
    assert first.endswith(b"}\n")
    assert not first.endswith(b"\n\n")
    text = first.decode()
    assert '\n  "contracts": [' in text
    assert list(json.loads(text)) == sorted(json.loads(text))


def test_a_missing_lock_is_a_usage_error_with_a_hint(tmp_path: Path) -> None:
    with pytest.raises(UsageError) as raised:
        load_lock(tmp_path / "none.lock")
    assert "none.lock" in str(raised.value)
    assert raised.value.hint is not None
    assert "pull" in raised.value.hint


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{not json", "not valid JSON"),
        ("[1, 2]", "not a lock file"),
        ('{"lock_version": 2, "server": "x", "contracts": []}', "unsupported lock_version"),
        ('{"server": "x", "contracts": []}', "unsupported lock_version"),
        ('{"lock_version": 1, "contracts": []}', "malformed"),
        ('{"lock_version": 1, "server": "x", "contracts": [{"ref": "a"}]}', "malformed"),
        ('{"lock_version": 1, "server": "x", "contracts": [], "extra": 1}', "malformed"),
    ],
)
def test_a_bad_lock_is_a_usage_error(tmp_path: Path, text: str, expected: str) -> None:
    path = tmp_path / "contractum.lock"
    path.write_text(text)
    with pytest.raises(UsageError) as raised:
        load_lock(path)
    assert expected in str(raised.value)


def test_a_bad_hash_in_the_lock_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / "contractum.lock"
    body = json.loads(render_lock(_lock(_entry())))
    body["contracts"][0]["content_sha256"] = "XYZ"
    path.write_text(json.dumps(body))
    with pytest.raises(UsageError) as raised:
        load_lock(path)
    assert "content_sha256" in str(raised.value)


# --- lookup -----------------------------------------------------------------------------------


def test_find_matches_a_reference_an_odcs_id_or_a_contract_id() -> None:
    lock = _lock(_entry(ref="acme/pay:payments.orders", folder_path="acme/pay"))
    assert lock.find("acme/pay:payments.orders") is not None
    assert lock.find("payments.orders") is not None
    assert lock.find(CID_A) is not None
    assert lock.find("payments.other") is None
    assert lock.find("acme/other:payments.orders") is None


def test_find_with_a_folder_path_matches_the_stored_folder() -> None:
    lock = _lock(_entry(ref="payments.orders", folder_path="acme/pay"))
    assert lock.find("acme/pay:payments.orders") is not None


def test_find_checks_the_version() -> None:
    lock = _lock(_entry(semver="1.4.0+b1", requested="latest"))
    assert lock.find("payments.orders", "1.4.0") is not None
    assert lock.find("payments.orders@1.4.0") is not None
    assert lock.find("payments.orders", "1.5.0") is None
    assert lock.find("payments.orders", "latest") is not None
    assert lock.find("payments.orders", "preview") is None


def test_an_explicit_version_overrides_the_one_in_the_reference() -> None:
    lock = _lock(_entry())
    assert lock.find("payments.orders@9.9.9", "1.4.0") is not None


def test_several_pinned_versions_without_a_choice_are_an_error() -> None:
    lock = _lock(
        _entry(requested="1.3.0", semver="1.3.0"), _entry(requested="1.4.0", semver="1.4.0")
    )
    with pytest.raises(FrozenLockError) as raised:
        lock.find("payments.orders")
    assert "1.3.0, 1.4.0" in str(raised.value)
    assert lock.find("payments.orders", "1.3.0") is not None


def test_an_entry_has_a_label_and_a_key() -> None:
    entry = _entry()
    assert entry.label == "payments.orders@1.4.0"
    assert entry.key() == ("payments.orders", "latest")
    assert _entry(odcs_id=None).label == "payments.orders@1.4.0"


# --- writing ----------------------------------------------------------------------------------


def test_a_failing_write_leaves_the_previous_lock_byte_equal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "contractum.lock"
    save_lock(path, _lock(_entry()))
    before = path.read_bytes()

    def boom(source: object, target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        save_lock(path, _lock(_entry(semver="9.9.9")))
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "cache") == ["contractum.lock"]


def test_the_lock_guard_lives_in_the_given_cache_directory(tmp_path: Path) -> None:
    cache = tmp_path / "somewhere"
    save_lock(tmp_path / "contractum.lock", _lock(_entry()), cache_dir=cache)
    assert len(list((cache / "locks").glob("*.lock"))) == 1
