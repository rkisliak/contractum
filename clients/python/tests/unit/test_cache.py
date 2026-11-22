"""The ETag cache: layout, hash-checked reads, atomic concurrent writes and revalidation."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest
from support.fake_server import FakeContractum, FakeVersion, sha256
from support.locks import record_guards_during_writes

from contractum import cache as cache_module
from contractum.cache import Cache, cache_root, normalise_url, server_key
from contractum.client import Client
from contractum.contracts import ContractVersion
from contractum.errors import IntegrityError

BASE = "https://contractum.example"
DOC = b"apiVersion: v3.0.2\nkind: DataContract\nid: payments.orders\nversion: 1.4.0\n"
CID = "0191a2b3-c4d5-4e6f-8091-a2b3c4d5e6f7"


def _version(content: bytes = DOC, **changes: object) -> ContractVersion:
    fields: dict[str, object] = {
        "contract_id": CID,
        "odcs_id": "payments.orders",
        "name": "Orders",
        "semver": "1.4.0",
        "status": "latest",
        "media_type": "application/yaml",
        "content": content,
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "etag": f'"{hashlib.sha256(content).hexdigest()}"',
    }
    fields.update(changes)
    return ContractVersion(**fields)  # type: ignore[arg-type]


def _server() -> FakeContractum:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [FakeVersion("1.4.0", DOC, status="latest")],
        name="Orders",
    )
    return server


# --- the cache itself -------------------------------------------------------------------------


def test_the_cache_root_follows_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cache_root({"CONTRACTUM_CACHE_DIR": str(tmp_path / "x")}) == tmp_path / "x"
    monkeypatch.setenv("CONTRACTUM_CACHE_DIR", str(tmp_path / "from-env"))
    assert Cache().root == tmp_path / "from-env"
    monkeypatch.delenv("CONTRACTUM_CACHE_DIR")
    assert cache_root({"CONTRACTUM_CACHE_DIR": "  "}).name  # the platform default
    assert "Cache(root=" in repr(Cache(tmp_path))


def test_equal_servers_share_a_key() -> None:
    assert server_key("https://Contractum.example/") == server_key("https://contractum.example")
    assert server_key(BASE) != server_key("https://other.example")
    assert len(server_key(BASE)) == 16
    assert normalise_url(" HTTPS://Host:8443/api/ ") == "https://host:8443/api"


def test_put_and_get_round_trip_with_the_documented_layout(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    cache.put(BASE, _version())
    directory = tmp_path / "v1" / server_key(BASE) / CID / "1.4.0"
    assert (directory / "document").read_bytes() == DOC
    meta = json.loads((directory / "meta.json").read_text())
    assert meta["content_sha256"] == sha256(DOC)
    found = cache.get(BASE, CID, "1.4.0")
    assert found is not None
    assert found.content == DOC
    assert found.source == "cache"
    assert found.odcs_id == "payments.orders"
    assert found.name == "Orders"


def test_a_missing_entry_is_a_miss(tmp_path: Path) -> None:
    assert Cache(tmp_path).get(BASE, CID, "1.4.0") is None
    assert not (tmp_path / "v1").exists()


def test_a_modified_document_is_discarded(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    cache.put(BASE, _version())
    directory = cache.entry_dir(BASE, CID, "1.4.0")
    (directory / "document").write_bytes(DOC + b"# tampered\n")
    assert cache.get(BASE, CID, "1.4.0") is None
    assert not (directory / "document").exists()
    assert not (directory / "meta.json").exists()


@pytest.mark.parametrize("damage", [b"{not json", b"[]", b""])
def test_unreadable_metadata_is_discarded(tmp_path: Path, damage: bytes) -> None:
    cache = Cache(tmp_path)
    cache.put(BASE, _version())
    directory = cache.entry_dir(BASE, CID, "1.4.0")
    (directory / "meta.json").write_bytes(damage)
    assert cache.get(BASE, CID, "1.4.0") is None
    assert not (directory / "document").exists()


def test_a_directory_with_a_missing_document_is_a_miss(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    cache.put(BASE, _version())
    (cache.entry_dir(BASE, CID, "1.4.0") / "document").unlink()
    assert cache.get(BASE, CID, "1.4.0") is None


def test_identifiers_with_separators_cannot_leave_the_cache(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    hostile = _version(contract_id="../../escape", semver="..\\..")
    cache.put(BASE, hostile)
    directory = cache.entry_dir(BASE, "../../escape", "..\\..")
    assert directory.resolve().is_relative_to(tmp_path.resolve())
    assert cache.get(BASE, "../../escape", "..\\..") is not None


def test_a_version_without_ids_is_not_stored(tmp_path: Path) -> None:
    Cache(tmp_path).put(BASE, _version(contract_id=None))
    assert not any(tmp_path.rglob("document"))


def test_concurrent_puts_of_one_entry_leave_one_valid_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = Cache(tmp_path)
    seen = record_guards_during_writes(monkeypatch, cache_module, lambda path: path.parent)
    errors: list[BaseException] = []
    contents: list[bytes | None] = []

    def work() -> None:
        try:
            for _ in range(10):
                cache.put(BASE, _version())
                found = cache.get(BASE, CID, "1.4.0")
                contents.append(found.content if found is not None else None)
        except BaseException as exc:  # reported to the main thread
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert contents == [DOC] * 60
    directory = cache.entry_dir(BASE, CID, "1.4.0")
    # Every write ran under the entry's own guard (two writes per put).
    assert seen == [[".lock"]] * 120
    # filelock keeps the guard after release on POSIX and deletes it on Windows.
    names = {p.name for p in directory.iterdir()}
    assert names - {".lock"} == {"document", "meta.json"}


# --- revalidation through the client ----------------------------------------------------------


def test_a_second_get_revalidates_and_serves_the_cached_bytes(canary_token: str) -> None:
    with _server() as server, Client(BASE, token=canary_token) as client:
        first = client.get("payments.orders")
        second = client.get("payments.orders")
        documents = [r for r in server.requests if r.url.path.endswith("/versions/1.4.0")]
    assert first.source == "server"
    assert second.source == "cache"
    assert second.content == DOC
    assert second.content_sha256 == sha256(DOC)
    assert len(documents) == 2
    assert "if-none-match" not in documents[0].headers
    assert documents[1].headers["if-none-match"] == f'"{sha256(DOC)}"'


def test_a_cached_entry_is_shared_by_a_new_client(canary_token: str) -> None:
    with _server() as server:
        with Client(BASE, token=canary_token) as client:
            client.get("payments.orders")
        with Client(BASE, token=canary_token) as client:
            again = client.get("payments.orders")
        documents = [r for r in server.requests if r.url.path.endswith("/versions/1.4.0")]
    assert again.source == "cache"
    assert "if-none-match" in documents[1].headers


def test_a_tampered_cache_entry_is_refetched(canary_token: str, tmp_path: Path) -> None:
    with _server() as server, Client(BASE, token=canary_token) as client:
        client.get("payments.orders")
        cache = client.cache
        assert cache is not None
        contract_id = server.contracts[0].contract_id
        (cache.entry_dir(BASE, contract_id, "1.4.0") / "document").write_bytes(b"corrupted")
        again = client.get("payments.orders")
        documents = [r for r in server.requests if r.url.path.endswith("/versions/1.4.0")]
    assert again.source == "server"
    assert again.content == DOC
    assert "if-none-match" not in documents[1].headers


def test_a_cache_entry_that_differs_from_the_metadata_is_not_trusted(canary_token: str) -> None:
    with _server() as server, Client(BASE, token=canary_token) as client:
        cache = client.cache
        assert cache is not None
        stale = DOC + b"# stale\n"
        cache.put(BASE, _version(stale, contract_id=server.contracts[0].contract_id))
        fresh = client.get("payments.orders")
        document = [r for r in server.requests if r.url.path.endswith("/versions/1.4.0")][0]
    assert fresh.content == DOC
    assert fresh.source == "server"
    assert "if-none-match" not in document.headers


def test_caching_can_be_switched_off(canary_token: str, tmp_path: Path) -> None:
    with _server() as server, Client(BASE, token=canary_token, cache=False) as client:
        client.get("payments.orders")
        client.get("payments.orders")
        documents = [r for r in server.requests if r.url.path.endswith("/versions/1.4.0")]
        assert client.cache is None
    assert all("if-none-match" not in r.headers for r in documents)
    assert not (tmp_path / "cache" / "v1").exists()


def test_an_unusable_cache_never_breaks_a_fetch(
    canary_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise PermissionError("read-only cache")

    monkeypatch.setattr(Cache, "get", broken)
    monkeypatch.setattr(Cache, "put", broken)
    with _server(), Client(BASE, token=canary_token) as client:
        version = client.get("payments.orders")
    assert version.content == DOC
    assert version.source == "server"


def test_a_304_without_headers_still_serves_the_cached_bytes(canary_token: str) -> None:
    server = _server()
    with server, Client(BASE, token=canary_token) as client:
        client.get("payments.orders")
        server.contracts[0].versions[0].not_modified = True
        again = client.get("payments.orders")
    assert again.source == "cache"
    assert again.content == DOC
    assert again.etag is None


def test_a_changed_etag_on_a_full_answer_is_an_integrity_error(canary_token: str) -> None:
    server = _server()
    with server, Client(BASE, token=canary_token) as client:
        client.get("payments.orders")
        server.contracts[0].versions[0].etag = '"' + "0" * 64 + '"'
        with pytest.raises(IntegrityError):
            client.get("payments.orders")
