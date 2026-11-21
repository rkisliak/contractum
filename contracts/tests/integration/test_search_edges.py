"""List/search edges: pagination adjacency, empty results, ordering stability, LIKE escaping."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest
from dc_auth.testing import FakeIssuer

pytestmark = pytest.mark.integration

LIST = "/api/v1/contracts/contracts"
StackFactory = Callable[..., AbstractAsyncContextManager[Any]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def seed(stack: Any, team: str, odcs_id: str, name: str) -> str:
    body = (
        f"apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: '1.0.0'\n"
        f"status: draft\nname: {name}\n"
    )
    created = await stack.post(team, body.encode("utf-8"))
    assert created.status_code == 201, created.text
    return str(created.json()["contract"]["id"])


async def listing(stack: Any, params: Any = None, token: str | None = None) -> httpx.Response:
    return await stack.client.get(LIST, params=params, headers=bearer(token or stack.admin_token))


def odcs_ids(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["odcs_id"] for item in response.json()["items"]]


def ids(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def pages(stack: Any, params: dict[str, Any]) -> list[httpx.Response]:
    """Follow next_cursor until it is null."""
    out: list[httpx.Response] = []
    cursor: str | None = None
    while True:
        query = {**params, **({"cursor": cursor} if cursor else {})}
        response = await listing(stack, query)
        assert response.status_code == 200, response.text
        out.append(response)
        cursor = response.json()["next_cursor"]
        if cursor is None:
            return out
        assert len(out) < 20, "pagination does not terminate"


async def seed_many(
    stack: Any, team: str, tag: str, count: int, name: str = "Edge Item"
) -> list[str]:
    return [await seed(stack, team, f"urn:dc:edge:{tag}:{n}", name) for n in range(count)]


async def test_adjacency_pages_of_three_cover_seven_contracts_without_gap_or_duplicate(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        tag = uuid.uuid4().hex[:8]
        created = await seed_many(stack, chain.team, tag, 7)
        params = {"q": f"urn:dc:edge:{tag}", "limit": 3}
        responses = await pages(stack, params)
        sizes = [len(r.json()["items"]) for r in responses]
        assert sizes == [3, 3, 1]
        paged = [i for r in responses for i in ids(r)]
        assert paged == sorted(created)  # the full list in id order
        assert len(set(paged)) == 7  # no duplicate
        # the last item of page N and the first of page N+1 are neighbours in id order
        for left, right in zip(responses, responses[1:], strict=False):
            assert ids(left)[-1] == sorted(created)[sorted(created).index(ids(right)[0]) - 1]
        assert responses[0].json()["next_cursor"] is not None
        assert responses[-1].json()["next_cursor"] is None


async def test_adjacency_an_exact_multiple_of_the_limit_has_no_empty_trailing_page(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        tag = uuid.uuid4().hex[:8]
        await seed_many(stack, chain.team, tag, 6)
        responses = await pages(stack, {"q": f"urn:dc:edge:{tag}", "limit": 3})
        assert [len(r.json()["items"]) for r in responses] == [3, 3]
        assert responses[-1].json()["next_cursor"] is None


async def test_adjacency_like_wildcards_and_backslash_in_q_match_only_themselves(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        tag = uuid.uuid4().hex[:8]
        await seed_many(stack, chain.team, tag, 3)
        # The name rule (32298d3) refuses % and \ in a contract name, so the LIKE metacharacters
        # live in the ODCS id, which q matches with the same escaped pattern as the name.
        special = f"urn:dc:edge:{tag}:100%_done\\x"
        await seed(stack, chain.team, special, "Special_Item")
        everything = f"urn:dc:edge:{tag}"
        assert odcs_ids(await listing(stack, {"q": "100%_done\\x"})) == [special]
        assert odcs_ids(await listing(stack, {"q": "%"})) == [special]  # not "every contract"
        assert odcs_ids(await listing(stack, {"q": "_"})) == [special]
        assert odcs_ids(await listing(stack, {"q": "\\"})) == [special]
        assert odcs_ids(await listing(stack, {"q": "100%_done"})) == [special]
        assert odcs_ids(await listing(stack, {"q": "00%x"})) == []  # % is not a wildcard
        # a q equal to a full name matches; the unescaped pattern would have matched all four
        assert odcs_ids(await listing(stack, {"q": "Edge Item", "name": "edge item"})) == (
            await _by_name(stack, "edge item")
        )
        assert len(odcs_ids(await listing(stack, {"q": everything}))) == 4


async def _by_name(stack: Any, name: str) -> list[str]:
    return odcs_ids(await listing(stack, {"name": name}))


async def test_empty_admin_gets_an_empty_page_and_so_does_a_non_admin_without_grants(
    make_stack: StackFactory, fake_issuer: FakeIssuer
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        await seed(stack, chain.team, f"urn:dc:edge:{uuid.uuid4()}", "Something")
        nothing = {"q": f"no-such-text-{uuid.uuid4()}"}
        empty = await listing(stack, nothing)
        assert empty.status_code == 200
        assert empty.json() == {"items": [], "next_cursor": None}
        # D-35: a caller without grants sees the same empty page, even for a matching query
        stranger = fake_issuer.mint("nobody")
        for params in (nothing, {"q": "Something"}):
            blind = await listing(stack, params, token=stranger)
            assert blind.status_code == 200
            assert blind.json() == {"items": [], "next_cursor": None}
        anonymous = await stack.client.get(LIST, params=nothing)
        assert anonymous.status_code == 401


async def test_empty_invalid_limit_cursor_and_text_are_422_problem_json_for_the_admin(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        good = (await stack.chain()).team
        await seed(stack, good, f"urn:dc:edge:{uuid.uuid4()}", "One")
        cases: list[dict[str, Any]] = [
            {"limit": 0},
            {"limit": 101},
            {"limit": "many"},
            {"cursor": "not-a-cursor"},
            {"cursor": "e30"},
            {"q": "x" * 201},
            {"q": "bad\x00text"},
            {"tags": ["ok", "bad\x00"]},
            {"folder_id": "not-a-uuid"},
        ]
        for params in cases:
            response = await listing(stack, params)
            assert response.status_code == 422, (params, response.text)
            assert response.headers["content-type"].startswith("application/problem+json")
            assert response.json()["type"] == "urn:dc:problem:validation-error"
        # the boundaries themselves are fine
        assert (await listing(stack, {"limit": 1})).status_code == 200
        assert (await listing(stack, {"limit": 100})).status_code == 200
        assert (await listing(stack, {"q": "x" * 200})).status_code == 200


async def test_ordering_equal_names_come_back_in_id_order(make_stack: StackFactory) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        tag = uuid.uuid4().hex[:8]
        created = await seed_many(stack, chain.team, tag, 4, name="Same Name")
        response = await listing(stack, {"name": "same name"})
        assert ids(response) == sorted(created)
        assert ids(await listing(stack, {"name": "SAME NAME", "limit": 2})) == sorted(created)[:2]


async def test_ordering_a_contract_inserted_between_pages_never_duplicates_or_skips(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        tag = uuid.uuid4().hex[:8]
        created = await seed_many(stack, chain.team, tag, 5)
        params = {"q": f"urn:dc:edge:{tag}", "limit": 2}
        first = await listing(stack, params)
        first_ids = ids(first)
        assert first_ids == sorted(created)[:2]

        late = await seed(stack, chain.team, f"urn:dc:edge:{tag}:late", "Late Arrival")
        rest: list[str] = []
        cursor = first.json()["next_cursor"]
        while cursor:
            page = await listing(stack, {**params, "cursor": cursor})
            rest.extend(ids(page))
            cursor = page.json()["next_cursor"]

        everything = first_ids + rest
        assert len(everything) == len(set(everything))  # nothing duplicated
        assert set(created) <= set(everything)  # nothing already-listed or pending is skipped
        assert late in rest  # the newcomer only ever appears after the cursor
        assert everything == sorted(created + [late])
