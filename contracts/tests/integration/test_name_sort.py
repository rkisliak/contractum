"""D-07: ``sort=name`` on the contract list, with a keyset cursor that carries the sort key.

Name order is ``lower(coalesce(nullif(name, ''), odcs_id))`` then contract id, so a contract
without a name sorts where its odcs id is shown. Omitting ``sort`` keeps the id order and the id
cursor byte for byte.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from dc_contracts.search import decode_cursor

pytestmark = pytest.mark.integration

LIST = "/api/v1/contracts/contracts"
StackFactory = Callable[..., AbstractAsyncContextManager[Any]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, name: str) -> bytes:
    return (
        f"apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: 1.0.0\n"
        f'status: draft\nname: "{name}"\n'
    ).encode()


async def seed(stack: Any, folder: str, odcs_id: str, name: str) -> str:
    created = await stack.post(folder, doc(odcs_id, name))
    assert created.status_code == 201, created.text
    return str(created.json()["contract"]["id"])


async def page(stack: Any, token: str, **params: Any) -> dict[str, Any]:
    response = await stack.client.get(LIST, params=params, headers=bearer(token))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def build(stack: Any) -> tuple[str, str, list[str]]:
    """A team with three contracts created as beta, Alpha, then an empty name (gamma by its id)."""
    chain = await stack.chain("sort")
    tag = uuid.uuid4().hex[:8]
    gamma_id = f"urn:dc:sort:{tag}:gamma"
    created = [
        await seed(stack, chain.team, f"urn:dc:sort:{tag}:b", "beta"),
        await seed(stack, chain.team, f"urn:dc:sort:{tag}:a", "Alpha"),
        await seed(stack, chain.team, gamma_id, ""),
    ]
    reader, reader_id = stack.token("sort-reader")
    stack.grant_sql(reader_id, "team_reader", chain.team)
    return chain.team, reader, created


async def test_name_sort_orders_by_lowercase_name_then_odcs_id_for_an_empty_name(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        team, reader, (beta, alpha, gamma) = await build(stack)

        body = await page(
            stack, reader, folder_id=team, include_subtree="false", sort="name", limit=100
        )

        assert [item["id"] for item in body["items"]] == [alpha, beta, gamma]
        assert body["next_cursor"] is None


async def test_the_name_cursor_returns_the_following_page(make_stack: StackFactory) -> None:
    async with make_stack() as stack:
        team, reader, (beta, alpha, gamma) = await build(stack)

        first = await page(
            stack, reader, folder_id=team, include_subtree="false", sort="name", limit=2
        )
        assert [item["id"] for item in first["items"]] == [alpha, beta]
        assert first["next_cursor"] is not None

        second = await page(
            stack,
            reader,
            folder_id=team,
            include_subtree="false",
            sort="name",
            limit=2,
            cursor=first["next_cursor"],
        )
        assert [item["id"] for item in second["items"]] == [gamma]
        assert second["next_cursor"] is None


async def test_without_sort_the_order_and_the_id_cursor_are_unchanged(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        team, reader, created = await build(stack)

        first = await page(stack, reader, folder_id=team, include_subtree="false", limit=2)
        assert [item["id"] for item in first["items"]] == sorted(created)[:2]
        assert decode_cursor(first["next_cursor"]) == uuid.UUID(sorted(created)[1])
