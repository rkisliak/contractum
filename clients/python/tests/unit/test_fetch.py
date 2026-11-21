"""Client.get: resolve, read the metadata, fetch the exact version and verify its hash."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from support.fake_server import FakeContractum, FakeVersion, sha256

from contractum.client import Client
from contractum.contracts import ContractVersion
from contractum.errors import IntegrityError, ServerError

BASE = "https://contractum.example"
ORDERS = b"apiVersion: v3.0.2\nkind: DataContract\nid: payments.orders\nversion: 1.2.0\n"


def _server() -> FakeContractum:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [
            FakeVersion("1.2.0", ORDERS, status="latest"),
            FakeVersion("1.1.0", ORDERS + b"# older\n"),
        ],
        name="Orders",
    )
    return server


def test_get_returns_the_latest_bytes_with_a_proved_hash(canary_token: str) -> None:
    with _server() as server, Client(BASE, token=canary_token) as client:
        version = client.get("payments.orders")
        targets = server.targets()
    assert isinstance(version, ContractVersion)
    assert version.content == ORDERS
    assert version.content_sha256 == sha256(ORDERS)
    assert version.semver == "1.2.0"
    assert version.status == "latest"
    assert version.odcs_id == "payments.orders"
    assert version.name == "Orders"
    assert version.document["id"] == "payments.orders"
    assert version.source == "server"
    assert len(targets) == 3
    assert targets[0].startswith("GET /api/v1/contracts/contracts?")
    assert "odcs_id=payments.orders" in targets[0]
    assert targets[1].endswith("/versions/latest/metadata")
    assert targets[2].endswith("/versions/1.2.0")


def test_the_document_request_carries_no_accept_override(canary_token: str) -> None:
    with _server() as server, Client(BASE, token=canary_token) as client:
        client.get("payments.orders")
        document = server.requests[-1]
    accept = document.headers.get("accept", "*/*")
    assert "yaml" not in accept
    assert "json" not in accept


def test_a_tampered_body_raises_an_integrity_error_without_leaking(canary_token: str) -> None:
    secret = b"password: hunter2-do-not-leak"
    server = FakeContractum()
    server.add_contract(
        "payments.orders", [FakeVersion("1.2.0", ORDERS, status="latest", served=secret)]
    )
    with (
        server,
        Client(BASE, token=canary_token) as client,
        pytest.raises(IntegrityError) as raised,
    ):
        client.get("payments.orders")
    message = str(raised.value)
    assert message == "content hash mismatch for payments.orders@1.2.0"
    assert "hunter2" not in message
    assert canary_token not in message


def test_a_wrong_etag_header_is_an_integrity_error(canary_token: str) -> None:
    server = FakeContractum()
    server.add_contract(
        "payments.orders", [FakeVersion("1.2.0", ORDERS, status="latest", etag='"deadbeef"')]
    )
    with server, Client(BASE, token=canary_token) as client, pytest.raises(IntegrityError):
        client.get("payments.orders")


def test_the_media_type_comes_from_the_response(
    canary_token: str, assert_no_secret: Callable[..., None]
) -> None:
    server = FakeContractum()
    body = b'{"apiVersion": "v3.0.2", "kind": "DataContract", "id": "payments.orders"}'
    server.add_contract(
        "payments.orders",
        [FakeVersion("1.0.0", body, status="latest", media_type="application/json")],
    )
    with server, Client(BASE, token=canary_token) as client:
        version = client.get("payments.orders")
    assert version.media_type == "application/json"
    assert version.document["id"] == "payments.orders"
    assert_no_secret(repr(version.info), canary_token)


def test_an_unexpected_304_without_a_conditional_request_is_a_server_error(
    canary_token: str,
) -> None:
    server = FakeContractum()
    server.add_contract(
        "payments.orders", [FakeVersion("1.2.0", ORDERS, status="latest", not_modified=True)]
    )
    with server, Client(BASE, token=canary_token) as client, pytest.raises(ServerError):
        client.get("payments.orders")
