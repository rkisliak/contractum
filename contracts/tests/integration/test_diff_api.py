"""The classified changelog between two versions the caller can read (VAL-09, VAL-02, D-156).

``GET /api/v1/contracts/contracts/{contract_id}/diff?from={ref}&to={ref}`` computes ``dc_odcs.diff``
on the fly (nothing is stored). A ref is a SemVer string, ``latest`` or, for a caller holding
``contract:read`` at the folder, a draft id. Consumers can only name versions visible to them.
``from`` newer than ``to`` is simply compared in that order. Pages hold at most ``limit`` changes.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]

CONTRACTS = "/api/v1/contracts/contracts"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, *names: str, version: str = "1.0.0") -> bytes:
    """A conformant ODCS v3.2.0 document with one table holding the named properties."""
    props = "".join(f"      - name: {n}\n        logicalType: string\n" for n in names)
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: draft\nname: Orders\nschema:\n  - name: orders\n    properties:\n{props}"
    ).encode()


def new_id() -> str:
    return f"urn:dc:diff-api:{uuid.uuid4().hex}"


def diff_url(contract: str) -> str:
    return f"{CONTRACTS}/{contract}/diff"


async def get_diff(stack: Any, contract: str, token: str, frm: str, to: str, **params: Any) -> Any:
    headers = bearer(token)
    if "if_none_match" in params:
        headers["If-None-Match"] = params.pop("if_none_match")
    return await stack.client.get(
        diff_url(contract), params={"from": frm, "to": to, **params}, headers=headers
    )


def stable(response: Any) -> tuple[int, list[tuple[str, Any]]]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return response.status_code, sorted(body.items())


async def role_at(stack: Any, folder: str, role: str) -> str:
    token, principal = stack.token(f"{role}-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, role, folder)
    return str(token)


async def save_draft(
    stack: Any, contract: str, odcs_id: str, version: str, names: tuple[str, ...], *, base: str
) -> tuple[str, str]:
    """A new draft from ``base`` saved with ``version`` and ``names``; (draft id, ETag)."""
    created = await stack.client.post(
        f"{CONTRACTS}/{contract}/drafts",
        json={"from_version": base, "bump": "none"},
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    draft = created.json()["draft"]
    saved = await stack.client.put(
        f"{CONTRACTS}/{contract}/drafts/{draft['id']}",
        content=doc(odcs_id, *names, version=version),
        headers={
            **bearer(stack.admin_token),
            "Content-Type": "application/yaml",
            "If-Match": draft["etag"],
        },
    )
    assert saved.status_code == 200, saved.text
    return str(draft["id"]), str(saved.headers["etag"])


async def contract_with(
    stack: Any, team: str, versions: list[tuple[str, tuple[str, ...]]]
) -> tuple[str, str]:
    """Publish each (version, properties) in order; returns (contract id, odcs id)."""
    odcs_id = new_id()
    first_version, first_names = versions[0]
    contract, draft, etag = await stack.create_contract(
        team, doc(odcs_id, *first_names, version=first_version)
    )
    assert (await stack.publish(contract, draft, etag)).status_code == 201
    for version, names in versions[1:]:
        draft, etag = await save_draft(stack, contract, odcs_id, version, names, base=first_version)
        published = await stack.publish(contract, draft, etag)
        assert published.status_code == 201, published.text
    return str(contract), odcs_id


# -- classification and direction -----------------------------------------------------------------


async def test_a_consumer_diffs_two_published_versions_in_either_order(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await contract_with(
            stack, chain.team, [("1.0.0", ("id", "amount")), ("2.0.0", ("id",))]
        )
        consumer = await role_at(stack, chain.team, "consumer")

        forward = await get_diff(stack, contract, consumer, "1.0.0", "2.0.0")

        assert forward.status_code == 200, forward.text
        body = forward.json()
        assert body["classifier_version"] == "odcs-compat-1"
        assert body["required_bump"] == "major"
        assert body["counts"] == {"breaking": 1, "warning": 0, "info": 0}
        assert body["changes_total"] == 1
        assert body["next_cursor"] is None
        assert [(c["code"], c["class"], c["path"]) for c in body["changes"]] == [
            ("property-removed", "breaking", "schema.orders.properties.amount")
        ]
        assert body["from"]["ref"] == "1.0.0"
        assert body["from"]["semver"] == "1.0.0"
        assert body["from"]["kind"] == "version"
        assert body["to"]["semver"] == "2.0.0"
        assert forward.headers["etag"]
        assert "private" in forward.headers["cache-control"]

        reverse = await get_diff(stack, contract, consumer, "2.0.0", "1.0.0")
        assert reverse.status_code == 200, reverse.text
        assert [c["code"] for c in reverse.json()["changes"]] == ["property-added"]
        assert reverse.json()["required_bump"] == "patch"

        # A later publish is a Preview until promoted; then `latest` names 2.0.0.
        promoted = await stack.client.post(
            f"{CONTRACTS}/{contract}/versions/2.0.0/promote", headers=bearer(stack.admin_token)
        )
        assert promoted.status_code == 200, promoted.text
        aliased = await get_diff(stack, contract, consumer, "latest", "1.0.0")
        assert aliased.status_code == 200, aliased.text
        assert aliased.json()["changes"] == reverse.json()["changes"]
        assert aliased.json()["from"]["semver"] == "2.0.0"
        assert aliased.json()["from"]["ref"] == "latest"


async def test_a_version_against_itself_has_no_changes_and_no_next_page(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await contract_with(stack, chain.team, [("1.0.0", ("id", "amount"))])
        consumer = await role_at(stack, chain.team, "consumer")

        for frm, to in (("1.0.0", "1.0.0"), ("latest", "1.0.0"), ("latest", "latest")):
            response = await get_diff(stack, contract, consumer, frm, to)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["changes"] == []
            assert body["changes_total"] == 0
            assert body["required_bump"] == "none"
            assert body["next_cursor"] is None


# -- visibility -----------------------------------------------------------------------------------


async def test_consumers_name_only_visible_versions_and_editors_may_name_a_draft(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, odcs_id = await contract_with(
            stack,
            chain.team,
            [("1.0.0", ("id", "amount")), ("1.1.0", ("id", "amount", "note"))],
        )
        promoted = await stack.client.post(
            f"{CONTRACTS}/{contract}/versions/1.1.0/promote", headers=bearer(stack.admin_token)
        )
        assert promoted.status_code == 200, promoted.text
        unpublished = await stack.client.post(
            f"{CONTRACTS}/{contract}/versions/1.0.0/unpublish", headers=bearer(stack.admin_token)
        )
        assert unpublished.status_code == 200, unpublished.text
        draft, _ = await save_draft(stack, contract, odcs_id, "2.0.0", ("id",), base="1.1.0")
        consumer = await role_at(stack, chain.team, "consumer")
        editor = await role_at(stack, chain.team, "team_editor")
        manager = stack.policy_manager(chain.org)
        reference = await get_diff(stack, str(uuid.uuid4()), consumer, "1.1.0", "1.1.0")
        assert reference.status_code == 404

        hidden = [
            await get_diff(stack, contract, consumer, "1.0.0", "1.1.0"),  # unpublished
            await get_diff(stack, contract, consumer, "1.1.0", draft),  # a draft id
            await get_diff(stack, contract, editor, "1.1.0", str(uuid.uuid4())),  # unknown draft
            await get_diff(stack, contract, editor, "1.1.0", "not-a-version"),
            await get_diff(stack, contract, manager, "1.1.0", "1.1.0"),  # D-36
        ]
        for response in hidden:
            assert stable(response) == stable(reference)

        drafted = await get_diff(stack, contract, editor, "1.1.0", draft)
        assert drafted.status_code == 200, drafted.text
        body = drafted.json()
        assert body["to"]["kind"] == "draft"
        assert body["to"]["ref"] == draft
        assert body["required_bump"] == "major"
        assert sorted(c["path"] for c in body["changes"]) == [
            "schema.orders.properties.amount",
            "schema.orders.properties.note",
        ]

        anonymous = await stack.client.get(
            diff_url(contract), params={"from": "1.1.0", "to": "1.1.0"}
        )
        assert anonymous.status_code == 401


# -- caching --------------------------------------------------------------------------------------


async def test_the_etag_is_stable_and_revalidates_only_after_authorisation(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await contract_with(
            stack,
            chain.team,
            [("1.0.0", ("id", "amount")), ("2.0.0", ("id",)), ("3.0.0", ("amount",))],
        )
        consumer = await role_at(stack, chain.team, "consumer")
        first = await get_diff(stack, contract, consumer, "1.0.0", "2.0.0")
        second = await get_diff(stack, contract, consumer, "1.0.0", "2.0.0")
        other_pair = await get_diff(stack, contract, consumer, "1.0.0", "3.0.0")
        etag = first.headers["etag"]
        assert etag.startswith('"') and not etag.startswith("W/")
        assert second.headers["etag"] == etag
        assert other_pair.headers["etag"] != etag

        cached = await get_diff(stack, contract, consumer, "1.0.0", "2.0.0", if_none_match=etag)
        assert cached.status_code == 304, cached.text
        assert cached.content == b""
        assert cached.headers["etag"] == etag

        manager = stack.policy_manager(chain.org)
        denied = await get_diff(stack, contract, manager, "1.0.0", "2.0.0", if_none_match=etag)
        assert denied.status_code == 404


# -- paging, ordering and concurrency -------------------------------------------------------------


async def test_pages_neither_skip_nor_repeat_and_the_limit_is_bounded(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await contract_with(
            stack, chain.team, [("1.0.0", ("id",)), ("1.1.0", ("id", "c", "a", "b"))]
        )
        consumer = await role_at(stack, chain.team, "consumer")
        whole = (await get_diff(stack, contract, consumer, "1.0.0", "1.1.0")).json()
        assert whole["changes_total"] == 3
        keys = [(c["path"], c["code"], c["attribute"] or "") for c in whole["changes"]]
        assert keys == sorted(keys)

        page_one = await get_diff(stack, contract, consumer, "1.0.0", "1.1.0", limit=2)
        assert page_one.status_code == 200, page_one.text
        assert len(page_one.json()["changes"]) == 2
        cursor = page_one.json()["next_cursor"]
        assert cursor
        page_two = await get_diff(
            stack, contract, consumer, "1.0.0", "1.1.0", limit=2, cursor=cursor
        )
        assert page_two.status_code == 200, page_two.text
        assert len(page_two.json()["changes"]) == 1
        assert page_two.json()["next_cursor"] is None
        assert page_one.json()["changes"] + page_two.json()["changes"] == whole["changes"]
        assert page_two.json()["changes_total"] == 3

        exact = await get_diff(stack, contract, consumer, "1.0.0", "1.1.0", limit=3)
        assert len(exact.json()["changes"]) == 3
        assert exact.json()["next_cursor"] is None

        for params in ({"limit": 1001}, {"limit": 0}, {"cursor": "not a cursor"}):
            refused = await get_diff(stack, contract, consumer, "1.0.0", "1.1.0", **params)
            assert refused.status_code == 422, (params, refused.text)


async def test_two_concurrent_requests_for_the_same_pair_answer_identically(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await contract_with(
            stack, chain.team, [("1.0.0", ("id", "amount")), ("2.0.0", ("id", "total"))]
        )
        consumer = await role_at(stack, chain.team, "consumer")

        one, two = await asyncio.gather(
            get_diff(stack, contract, consumer, "1.0.0", "2.0.0"),
            get_diff(stack, contract, consumer, "1.0.0", "2.0.0"),
        )

        assert one.status_code == two.status_code == 200
        assert one.content == two.content
        assert one.headers["etag"] == two.headers["etag"]
