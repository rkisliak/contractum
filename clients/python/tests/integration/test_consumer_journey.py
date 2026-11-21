"""The library as a consumer uses it: a read-only PAT against the real stack (SDKQA-01).

Fetch Latest, an exact SemVer and the Preview, find the contract, revalidate from the cache,
pin it into files and a lock and read it back offline, refuse a tampered lock, and compare
Latest with the Preview on data that only the Preview rejects.
"""

from __future__ import annotations

import hashlib
import json
import ssl
from pathlib import Path

import httpx
import pytest
from support.integration import CONTRACTS, World, bad_data, good_data, preview_breaking_data

from contractum import Client, FrozenLockError, TestOptions, compare
from contractum.contracts import ContractDeprecationWarning
from contractum.pull import pull


def _client(world: World, token: str) -> Client:
    return Client(world.url, token=token, ca_bundle=world.ca)


def test_latest_exact_version_and_preview_come_back_byte_proven(
    it_world: World, reader_pat: str
) -> None:
    with _client(it_world, reader_pat) as client:
        latest = client.get(it_world.odcs_id)
        assert latest.semver == "1.1.0"
        assert latest.status == "latest"
        metadata = client.version_metadata(latest.contract_id, "latest")
        assert latest.content_sha256 == metadata.content_sha256
        assert hashlib.sha256(latest.content).hexdigest() == metadata.content_sha256

        with pytest.warns(ContractDeprecationWarning):
            old = client.get(f"{it_world.odcs_id}@1.0.0")
        assert old.semver == "1.0.0"
        assert old.deprecated is True
        assert old.sunset_date is not None

        preview = client.get(f"{it_world.odcs_id}@preview")
        assert preview.semver == "2.0.0"
        assert preview.status == "preview"
        assert preview.content != latest.content


def test_search_and_versions_find_the_contract(it_world: World, reader_pat: str) -> None:
    with _client(it_world, reader_pat) as client:
        found = client.search(it_world.odcs_id)
        assert [item.odcs_id for item in found] == [it_world.odcs_id]
        assert str(found[0].id) == it_world.contract_id
        listed = client.search(folder=it_world.folder_path)
        assert it_world.odcs_id in {item.odcs_id for item in listed}
        versions = client.versions(it_world.odcs_id)
        assert [item.semver for item in versions] == ["2.0.0", "1.1.0", "1.0.0"]
        assert {item.semver: item.deprecated for item in versions}["1.0.0"] is True


def test_published_at_wire_format_is_an_iso_timestamp(it_world: World, reader_pat: str) -> None:
    """Assumption A1: record the raw wire format of ``published_at`` and parse it as a datetime."""
    context = ssl.create_default_context(cafile=str(it_world.ca))
    with httpx.Client(base_url=it_world.url, verify=context) as http:
        response = http.get(
            f"{CONTRACTS}/{it_world.contract_id}/versions/latest/metadata",
            headers={"Authorization": f"Bearer {reader_pat}"},
        )
    assert response.status_code == 200
    raw = json.loads(response.content)["published_at"]
    print(f"A1 published_at wire format: {raw!r}")
    with _client(it_world, reader_pat) as client:
        parsed = client.version_metadata(it_world.contract_id, "latest").published_at
    assert parsed is not None
    assert parsed.isoformat().startswith(raw[:19])


def test_a_second_get_is_revalidated_with_a_304_and_served_from_the_cache(
    it_world: World, reader_pat: str
) -> None:
    with _client(it_world, reader_pat) as client:
        first = client.get(it_world.odcs_id)
        second = client.get(it_world.odcs_id)
    assert first.source == "server"
    assert second.source == "cache"
    assert second.content == first.content


def test_pull_writes_a_lock_and_the_frozen_offline_read_needs_no_network(
    it_world: World, reader_pat: str, tmp_path: Path
) -> None:
    directory, lock = tmp_path / "contracts", tmp_path / "contractum.lock"
    with _client(it_world, reader_pat) as client:
        pulled = client.pull([f"{it_world.odcs_id}@1.1.0"], directory=directory, lock_path=lock)
    assert not pulled.failed
    assert lock.is_file()
    (entry,) = pulled.lock.contracts
    assert entry.semver == "1.1.0"
    assert (lock.parent / entry.file).is_file()

    offline = pull(None, directory=directory, lock_path=lock, frozen=True, offline=True)
    (read,) = offline.contracts
    assert read.contract.source == "lock"
    assert read.contract.content_sha256 == entry.content_sha256


def test_a_tampered_lock_is_refused_as_a_frozen_lock_error(
    it_world: World, reader_pat: str, tmp_path: Path
) -> None:
    directory, lock = tmp_path / "contracts", tmp_path / "contractum.lock"
    with _client(it_world, reader_pat) as client:
        client.pull([f"{it_world.odcs_id}@1.1.0"], directory=directory, lock_path=lock)
    document = json.loads(lock.read_text(encoding="utf-8"))
    digest = document["contracts"][0]["content_sha256"]
    document["contracts"][0]["content_sha256"] = ("0" if digest[0] != "0" else "1") + digest[1:]
    lock.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FrozenLockError):
        pull(None, directory=directory, lock_path=lock, frozen=True, offline=True)


def test_compare_shows_the_failure_only_the_preview_has(
    it_world: World, reader_pat: str, tmp_path: Path
) -> None:
    options = TestOptions()
    with _client(it_world, reader_pat) as client:
        breaking = compare(
            client, it_world.odcs_id, preview_breaking_data(tmp_path), options=options
        )
        assert (breaking.from_semver, breaking.to_semver) == ("1.1.0", "2.0.0")
        assert breaking.from_report.exit_code() == 0
        assert breaking.to_report.exit_code() == 1
        assert breaking.newly_failing >= 1
        assert breaking.exit_code() == 1
        assert breaking.server_diff is not None
        assert any(delta.regression for delta in breaking.deltas)

        clean = compare(client, it_world.odcs_id, good_data(tmp_path), options=options)
        assert clean.newly_failing == 0
        assert clean.exit_code() == 0

        both = compare(client, it_world.odcs_id, bad_data(tmp_path), options=options)
        assert both.from_report.exit_code() == 1
