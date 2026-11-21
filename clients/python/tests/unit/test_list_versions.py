"""Client.search and Client.versions against the fake server."""

from __future__ import annotations

import pytest
from support.fake_server import FakeContractum, FakeVersion

from contractum.client import Client
from contractum.errors import AmbiguousReferenceError, NotFoundError, UsageError

BASE = "https://contractum.example"
DOC = b"apiVersion: v3.0.2\nkind: DataContract\nid: x\n"


def _client(token: str) -> Client:
    return Client(BASE, token=token, sleep=lambda seconds: None)


def _many(count: int) -> FakeContractum:
    server = FakeContractum()
    for number in range(count):
        server.add_contract(
            f"orders-{number:03d}",
            [FakeVersion("1.0.0", DOC, status="latest")],
            domain="sales" if number % 2 == 0 else "ops",
            tags=["pii"] if number % 3 == 0 else [],
        )
    return server


def test_search_keeps_the_servers_order_and_one_page_by_default(canary_token: str) -> None:
    server = _many(5)
    server.page_size = 2
    with server, _client(canary_token) as client:
        items = client.search(limit=3)
    assert [item.odcs_id for item in items] == ["orders-000", "orders-001", "orders-002"]


def test_search_all_follows_every_page(canary_token: str) -> None:
    server = _many(5)
    server.page_size = 2
    with server, _client(canary_token) as client:
        items = client.search(all=True)
    assert [item.odcs_id for item in items] == [f"orders-{n:03d}" for n in range(5)]
    assert sum(1 for target in server.targets() if "/contracts/contracts?" in target) == 3


def test_search_with_a_limit_above_one_page_clamps_the_page_size(canary_token: str) -> None:
    server = _many(3)
    with server, _client(canary_token) as client:
        items = client.search(limit=500)
    assert len(items) == 3
    assert "limit=100" in server.targets()[0]


def test_search_maps_filters_to_server_parameters(canary_token: str) -> None:
    server = _many(6)
    with server, _client(canary_token) as client:
        items = client.search("orders", domain="sales", status="active", tags=["pii"])
    assert [item.odcs_id for item in items] == ["orders-000"]
    target = server.targets()[0]
    assert "q=orders" in target
    assert "domain=sales" in target
    assert "status=active" in target
    assert "tags=pii" in target


def test_search_by_folder_path_uses_the_folder_id_with_its_subtree(canary_token: str) -> None:
    server = FakeContractum()
    folder = server.add_folder("acme/payments")
    server.add_contract("inside", [FakeVersion("1.0.0", DOC, status="latest")], folder_id=folder)
    server.add_contract("outside", [FakeVersion("1.0.0", DOC, status="latest")])
    with server, _client(canary_token) as client:
        items = client.search(folder="/acme/payments/")
    assert [item.odcs_id for item in items] == ["inside"]
    listing = server.targets()[1]
    assert f"folder_id={folder}" in listing
    assert "include_subtree=true" in listing


def test_search_in_an_unknown_folder_is_not_found(canary_token: str) -> None:
    with FakeContractum(), _client(canary_token) as client:
        with pytest.raises(NotFoundError, match="acme/none"):
            client.search(folder="acme/none")


def test_search_rejects_a_limit_below_one(canary_token: str) -> None:
    with _client(canary_token) as client, pytest.raises(UsageError):
        client.search(limit=0)


def test_an_empty_search_is_an_empty_list(canary_token: str) -> None:
    with FakeContractum(), _client(canary_token) as client:
        assert client.search() == []


def test_two_contracts_with_one_odcs_id_are_both_listed(canary_token: str) -> None:
    server = FakeContractum()
    first = server.add_contract(
        "payments.orders",
        [FakeVersion("1.0.0", DOC, status="latest")],
        folder_id="aaaaaaaa-0000-4000-8000-000000000001",
    )
    second = server.add_contract(
        "payments.orders",
        [FakeVersion("1.0.0", DOC, status="latest")],
        folder_id="bbbbbbbb-0000-4000-8000-000000000002",
    )
    with server, _client(canary_token) as client:
        items = client.search()
    assert [str(item.id) for item in items] == [first.contract_id, second.contract_id]
    assert [str(item.folder_id) for item in items] == [
        "aaaaaaaa-0000-4000-8000-000000000001",
        "bbbbbbbb-0000-4000-8000-000000000002",
    ]


def _versioned() -> FakeContractum:
    server = FakeContractum()
    server.add_contract(
        "payments.orders",
        [
            FakeVersion(
                "1.0.0", DOC + b"# 1\n", status="older", deprecated=True, sunset_date="2027-01-31"
            ),
            FakeVersion("2.0.0", DOC + b"# 2\n", status="latest"),
            FakeVersion("2.1.0-rc.1", DOC + b"# 3\n", status="preview"),
        ],
    )
    return server


def test_versions_come_in_server_order_with_state_and_sunset(canary_token: str) -> None:
    with _versioned(), _client(canary_token) as client:
        items = client.versions("payments.orders")
    assert [item.semver for item in items] == ["2.1.0-rc.1", "2.0.0", "1.0.0"]
    assert [item.status for item in items] == ["preview", "latest", "older"]
    assert [item.deprecated for item in items] == [False, False, True]
    assert items[2].sunset_date is not None
    assert items[2].sunset_date.isoformat() == "2027-01-31"


def test_versions_refuses_a_reference_with_a_version(canary_token: str) -> None:
    with _versioned(), _client(canary_token) as client, pytest.raises(UsageError):
        client.versions("payments.orders@1.0.0")


def test_versions_of_an_ambiguous_reference_is_an_error(canary_token: str) -> None:
    server = _versioned()
    server.add_contract(
        "payments.orders",
        [FakeVersion("1.0.0", DOC, status="latest")],
        folder_id="cccccccc-0000-4000-8000-000000000003",
    )
    with server, _client(canary_token) as client, pytest.raises(AmbiguousReferenceError):
        client.versions("payments.orders")
