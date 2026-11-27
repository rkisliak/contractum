"""The list sort parameter and the deprecated flag on list rows, through the SDK."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from support.fake_server import FakeContractum, FakeVersion

from contractum import _api
from contractum._generated.models import ContractListOut
from contractum.client import Client
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


def _list_targets(server: FakeContractum) -> list[str]:
    return [target for target in server.targets() if "/contracts/contracts?" in target]


def test_list_contracts_sends_sort_name_in_the_query(canary_token: str) -> None:
    server = _server(2)
    with server, _client(canary_token) as client:
        page = client.list_contracts(sort="name")
    assert len(page.items) == 2
    assert "sort=name" in _list_targets(server)[0]


def test_the_default_call_sends_the_servers_default_sort(canary_token: str) -> None:
    server = _server(1)
    with server, _client(canary_token) as client:
        client.list_contracts()
    (target,) = _list_targets(server)
    assert "sort=name" not in target
    assert "sort=id" in target or "sort=" not in target


def test_iter_contracts_forwards_sort_on_every_page(canary_token: str) -> None:
    server = _server(5)
    server.page_size = 2
    with server, _client(canary_token) as client:
        items = list(client.iter_contracts(sort="name"))
    assert len(items) == 5
    targets = _list_targets(server)
    assert len(targets) == 3
    assert all("sort=name" in target for target in targets)


def test_the_low_level_wrapper_forwards_sort_to_the_generated_operation(
    canary_token: str,
) -> None:
    server = _server(1)
    with server, _client(canary_token) as client:
        _api.list_contracts(client._http, sort="name")
    assert "sort=name" in _list_targets(server)[0]


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
