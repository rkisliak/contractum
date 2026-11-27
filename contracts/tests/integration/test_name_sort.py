"""D-07: ``sort=name`` on the contract list, with a keyset cursor that carries the sort key.

Name order is ``lower(coalesce(nullif(name, ''), odcs_id))`` then contract id, so a contract
without a name sorts where its odcs id is shown. Omitting ``sort`` keeps the id order and the id
cursor byte for byte.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from dc_contracts.search import (
    MAX_CURSOR_CHARS,
    NAME_KEY_CHARS,
    decode_cursor,
    decode_name_cursor,
    encode_cursor,
    encode_name_cursor,
)

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


Sql = Callable[..., list[dict[str, Any]]]
CURSOR_POINTER = "/query/cursor"


def doc_without_name(odcs_id: str) -> bytes:
    return (
        f"apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: 1.0.0\nstatus: draft\n"
    ).encode()


async def team_and_reader(stack: Any, prefix: str) -> tuple[str, str, str]:
    """A fresh team, a token that reads only that team, and a unique tag for odcs ids."""
    chain = await stack.chain(prefix)
    reader, reader_id = stack.token(f"{prefix}-reader")
    stack.grant_sql(reader_id, "team_reader", chain.team)
    return chain.team, reader, uuid.uuid4().hex[:8]


async def names(stack: Any, token: str, team: str, **params: Any) -> tuple[list[str], str | None]:
    """The ids on one name-sorted page of a team and its next cursor."""
    body = await page(stack, token, folder_id=team, include_subtree="false", sort="name", **params)
    return [item["id"] for item in body["items"]], body["next_cursor"]


async def refused(stack: Any, token: str, team: str, **params: Any) -> dict[str, Any]:
    response = await stack.client.get(
        LIST,
        params={"folder_id": team, "include_subtree": "false", **params},
        headers=bearer(token),
    )
    assert response.status_code == 422, response.text
    body: dict[str, Any] = response.json()
    assert body["errors"][0]["pointer"] in {CURSOR_POINTER, "/query/sort"}
    return body


async def test_names_equal_apart_from_case_are_two_rows_and_page_exactly_once(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        team, reader, tag = await team_and_reader(stack, "tie")
        first = await seed(stack, team, f"urn:dc:sort:{tag}:one", "Same")
        second = await seed(stack, team, f"urn:dc:sort:{tag}:two", "same")

        page_one, cursor = await names(stack, reader, team, limit=1)
        assert page_one == sorted([first, second])[:1]
        assert cursor is not None
        page_two, last = await names(stack, reader, team, limit=1, cursor=cursor)
        assert page_one + page_two == sorted([first, second])
        assert last is None

        everything, none = await names(stack, reader, team, limit=100)
        assert everything == sorted([first, second])
        assert none is None


async def test_a_document_without_a_name_and_an_empty_name_sort_by_their_odcs_id(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        team, reader, tag = await team_and_reader(stack, "nameless")
        named = await seed(stack, team, f"urn:dc:sort:{tag}:m", "mango")
        created = await stack.post(team, doc_without_name(f"urn:dc:sort:{tag}:a-null"))
        assert created.status_code == 201, created.text
        null_name = str(created.json()["contract"]["id"])
        empty = await seed(stack, team, f"urn:dc:sort:{tag}:z-empty", "")

        ids, cursor = await names(stack, reader, team, limit=100)

        # "mango" < "urn:..." and the two odcs ids order by their suffix (a-null < z-empty)
        assert set(ids) == {named, null_name, empty}
        assert ids == [named, null_name, empty]
        assert cursor is None


async def test_a_300_character_name_pages_with_a_128_character_key(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        team, reader, tag = await team_and_reader(stack, "longname")
        long_id = await seed(stack, team, f"urn:dc:sort:{tag}:long", "A" * 300)
        after = await seed(stack, team, f"urn:dc:sort:{tag}:after", "b")

        first, cursor = await names(stack, reader, team, limit=1)
        assert first == [long_id]
        assert cursor is not None
        key, last_id = decode_name_cursor(cursor)
        assert key == "a" * NAME_KEY_CHARS
        assert len(key) == NAME_KEY_CHARS
        assert str(last_id) == long_id

        second, end = await names(stack, reader, team, limit=1, cursor=cursor)
        assert second == [after]
        assert end is None


async def test_names_sharing_their_first_128_characters_keep_contract_id_order(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        team, reader, tag = await team_and_reader(stack, "prefix")
        z_first = await seed(stack, team, f"urn:dc:sort:{tag}:z", "a" * NAME_KEY_CHARS + "z")
        b_second = await seed(stack, team, f"urn:dc:sort:{tag}:b", "a" * NAME_KEY_CHARS + "b")

        ids, _ = await names(stack, reader, team, limit=100)
        assert ids == [z_first, b_second]

        page_one, cursor = await names(stack, reader, team, limit=1)
        assert page_one == [z_first]
        assert cursor is not None
        page_two, end = await names(stack, reader, team, limit=1, cursor=cursor)
        assert page_two == [b_second]
        assert end is None


async def test_a_contract_inserted_between_pages_never_repeats_or_skips_a_row(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        team, reader, tag = await team_and_reader(stack, "insert")
        b1 = await seed(stack, team, f"urn:dc:sort:{tag}:1", "b1")
        b3 = await seed(stack, team, f"urn:dc:sort:{tag}:3", "b3")
        b5 = await seed(stack, team, f"urn:dc:sort:{tag}:5", "b5")

        page_one, cursor = await names(stack, reader, team, limit=2)
        assert page_one == [b1, b3]
        assert cursor is not None

        b0 = await seed(stack, team, f"urn:dc:sort:{tag}:0", "b0")
        b4 = await seed(stack, team, f"urn:dc:sort:{tag}:4", "b4")

        page_two, end = await names(stack, reader, team, limit=2, cursor=cursor)
        assert page_two == [b4, b5]
        assert end is None
        assert not {b0, b1, b3} & set(page_two)


async def test_a_cursor_of_the_other_sort_mode_or_an_unknown_sort_is_a_422(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        team, reader, tag = await team_and_reader(stack, "mode")
        created = await seed(stack, team, f"urn:dc:sort:{tag}:a", "a")
        await seed(stack, team, f"urn:dc:sort:{tag}:b", "b")

        body = await refused(
            stack, reader, team, sort="name", cursor=encode_cursor(uuid.UUID(created))
        )
        assert body["errors"][0]["pointer"] == CURSOR_POINTER

        name_cursor = encode_name_cursor("a", uuid.UUID(created))
        body = await refused(stack, reader, team, cursor=name_cursor)
        assert body["errors"][0]["pointer"] == CURSOR_POINTER
        body = await refused(stack, reader, team, sort="id", cursor=name_cursor)
        assert body["errors"][0]["pointer"] == CURSOR_POINTER

        await refused(stack, reader, team, sort="bogus")


async def test_every_malformed_name_cursor_is_a_422_at_the_cursor_and_never_a_500(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        team, reader, tag = await team_and_reader(stack, "malformed")
        created = uuid.UUID(await seed(stack, team, f"urn:dc:sort:{tag}:a", "a"))
        canonical = encode_name_cursor("alpha", created)
        payload = b"n:" + str(created).encode() + b":alpha??>>"
        url_safe = base64.urlsafe_b64encode(payload).decode("ascii")
        standard = base64.b64encode(payload).decode("ascii").rstrip("=")
        assert "+" in standard or "/" in standard
        bad = {
            "nul": encode_name_cursor("a\x00b", created),
            "lone surrogate": base64.urlsafe_b64encode(
                b"n:" + str(created).encode() + b":\xed\xa0\x80"
            )
            .decode()
            .rstrip("="),
            "129 characters": encode_name_cursor("a" * (NAME_KEY_CHARS + 1), created),
            "padded": canonical + "=" * (-len(canonical) % 4 or 1),
            "standard alphabet": standard,
            "over the length cap": "A" * (MAX_CURSOR_CHARS + 1),
            "not base64": "!!!!",
        }
        assert url_safe.rstrip("=") != standard
        for label, raw in bad.items():
            body = await refused(stack, reader, team, sort="name", cursor=raw)
            assert body["errors"][0]["pointer"] == CURSOR_POINTER, label


async def test_a_crafted_cursor_naming_a_hidden_contract_reveals_nothing(
    make_stack: StackFactory, sql: Sql
) -> None:
    async with make_stack() as stack:
        team_x, reader, tag = await team_and_reader(stack, "oracle-x")
        chain_y = await stack.chain("oracle-y")
        await seed(stack, team_x, f"urn:dc:sort:{tag}:a", "aaa")
        low = await seed(stack, team_x, f"urn:dc:sort:{tag}:z", "zzz")
        hidden = await seed(stack, chain_y.team, f"urn:dc:sort:{tag}:h", "mmm")

        # the key exactly as the server computes it, read back from the contracts database
        rows = sql(
            stack.contracts_db_url,
            "SELECT left(lower(coalesce(nullif(v.name, ''), c.odcs_id)), %s) AS key "
            "FROM contract c JOIN contract_version v ON v.contract_id = c.id WHERE c.id = %s",
            NAME_KEY_CHARS,
            hidden,
        )
        key = str(rows[0]["key"])
        assert key == "mmm"

        responses = []
        for contract_id in (uuid.UUID(hidden), uuid.uuid4()):
            response = await stack.client.get(
                LIST,
                params={
                    "folder_id": team_x,
                    "include_subtree": "false",
                    "sort": "name",
                    "cursor": encode_name_cursor(key, contract_id),
                },
                headers=bearer(reader),
            )
            responses.append(response)

        assert [r.status_code for r in responses] == [200, 200]
        assert responses[0].json() == responses[1].json()
        assert responses[0].content == responses[1].content
        assert [item["id"] for item in responses[0].json()["items"]] == [low]
