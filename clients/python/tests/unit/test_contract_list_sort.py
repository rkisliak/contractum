"""The list sort parameter and the deprecated flag on list rows, through the SDK.

The fake server orders by ``sort`` and pins a cursor to the sort it came from, so these tests prove
the SDK forwards ``sort`` on every page and that a cursor round trip returns the right order. The
real server's ordering and cursor strictness are covered server-side by ``test_name_sort.py`` and
``test_tree_visibility.py`` in the contracts package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from support.fake_server import FakeContractum, FakeVersion

from contractum import _api
from contractum._generated.models import ContractListOut
from contractum.client import Client
from contractum.errors import ServerError
from contractum.models import ContractPage

pytestmark = pytest.mark.unit

BASE = "https://contractum.example"
DOC = b"apiVersion: v3.0.2\nkind: DataContract\nid: x\n"
PAYLOADS = Path(__file__).parent.parent / "fixtures" / "payloads"


def _client(token: str) -> Client:
    return Client(BASE, token=token, sleep=lambda seconds: None)


def _server(count: int) -> FakeContractum:
    server = FakeContractum()
    for number in range(count):
        server.add_contract(f"orders-{number:03d}", [FakeVersion("1.0.0", DOC, status="latest")])
    return server


def _named_server() -> FakeContractum:
    """Five contracts whose creation order differs from their name order."""
    server = FakeContractum()
    for odcs_id, name in [
        ("c-1", "echo"),
        ("c-2", "Alpha"),
        ("c-3", "delta"),
        ("c-4", "Bravo"),
        ("c-5", "charlie"),
    ]:
        server.add_contract(odcs_id, [FakeVersion("1.0.0", DOC, status="latest")], name=name)
    return server


def _list_targets(server: FakeContractum) -> list[str]:
    return [target for target in server.targets() if "/contracts/contracts?" in target]


def _query(target: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(target).query)


def test_list_contracts_sends_sort_name_in_the_query(canary_token: str) -> None:
    server = _server(2)
    with server, _client(canary_token) as client:
        page = client.list_contracts(sort="name")
    assert len(page.items) == 2
    assert _query(_list_targets(server)[0])["sort"] == ["name"]


def test_the_default_call_sends_the_servers_default_sort(canary_token: str) -> None:
    server = _server(1)
    with server, _client(canary_token) as client:
        client.list_contracts()
    (target,) = _list_targets(server)
    assert _query(target)["sort"] == ["id"]


def test_sort_decides_the_order_the_server_answers_in(canary_token: str) -> None:
    """Name order and creation order differ, so the order proves which sort reached the server."""
    server = _named_server()
    with server, _client(canary_token) as client:
        by_name = [item.name for item in client.list_contracts(sort="name").items]
        by_id = [item.name for item in client.list_contracts().items]
    assert by_name == ["Alpha", "Bravo", "charlie", "delta", "echo"]
    assert by_id == ["echo", "Alpha", "delta", "Bravo", "charlie"]


def test_a_name_cursor_round_trip_returns_every_contract_in_name_order(
    canary_token: str,
) -> None:
    server = _named_server()
    server.page_size = 2
    with server, _client(canary_token) as client:
        first = client.list_contracts(sort="name")
        assert [item.name for item in first.items] == ["Alpha", "Bravo"]
        assert first.next_cursor is not None
        second = client.list_contracts(sort="name", cursor=first.next_cursor)
        assert [item.name for item in second.items] == ["charlie", "delta"]
        # A cursor is only valid with the sort it came from.
        with pytest.raises(ServerError) as refused:
            client.list_contracts(sort="id", cursor=first.next_cursor)
        assert refused.value.status == 422


def test_iter_contracts_forwards_sort_on_every_page(canary_token: str) -> None:
    server = _named_server()
    server.page_size = 2
    with server, _client(canary_token) as client:
        items = list(client.iter_contracts(sort="name"))
    assert [item.name for item in items] == ["Alpha", "Bravo", "charlie", "delta", "echo"]
    targets = _list_targets(server)
    assert len(targets) == 3
    assert all(_query(target)["sort"] == ["name"] for target in targets)


def test_the_low_level_wrapper_forwards_sort_to_the_generated_operation(
    canary_token: str,
) -> None:
    server = _server(1)
    with server, _client(canary_token) as client:
        _api.list_contracts(client._http, sort="name")
    assert _query(_list_targets(server)[0])["sort"] == ["name"]


def test_the_shipped_fixtures_carry_deprecated_false() -> None:
    page = ContractPage.from_generated(
        ContractListOut.from_dict(json.loads((PAYLOADS / "contract_list.json").read_text()))
    )
    assert page.items[0].version is not None
    assert page.items[0].version.deprecated is False


def test_a_deprecated_head_version_maps_to_true() -> None:
    payload: dict[str, Any] = json.loads((PAYLOADS / "contract_list.json").read_text())
    payload["items"][0]["version"]["deprecated"] = True
    page = ContractPage.from_generated(ContractListOut.from_dict(payload))
    assert page.items[0].version is not None
    assert page.items[0].version.deprecated is True
