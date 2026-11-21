"""A compact scope with excluded subtrees trims the contract list in SQL (T-02-03)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from dc_auth.authorizer import FolderScope
from dc_contracts.repo import search_contracts
from dc_contracts.search import ContractSearchFilters
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration

StackFactory = Callable[..., AbstractAsyncContextManager[Any]]


def doc(odcs_id: str) -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "1.0.0"\n'
        f"status: draft\nname: {odcs_id}\n"
    ).encode()


async def _odcs_ids(db_url: str, scope: FolderScope) -> list[str]:
    engine = create_async_engine(db_url)
    try:
        async with async_sessionmaker(engine)() as session:
            rows, _ = await search_contracts(
                session, ContractSearchFilters(), metadata_scope=scope, content_scope=scope
            )
    finally:
        await engine.dispose()
    return [contract.odcs_id for contract, _, _ in rows]


async def test_excluded_subtree_is_hidden_and_exact_folder_ids_add_back(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        a = await stack.chain()
        b = await stack.chain()
        a_team_2 = await stack.team_in_org(a.org, "second")
        tag = uuid.uuid4().hex[:8]
        id_a, id_a2, id_b = (f"urn:dc:scope:{tag}:{n}" for n in ("a", "a2", "b"))
        for team, odcs_id in ((a.team, id_a), (a_team_2, id_a2), (b.team, id_b)):
            created = await stack.post(team, doc(odcs_id))
            assert created.status_code == 201, created.text
        url = stack.contracts_db_url
        org_a, team_a = uuid.UUID(a.org), uuid.UUID(a.team)

        everything = await _odcs_ids(url, FolderScope(all=True))
        assert {id_a, id_a2, id_b} <= set(everything)

        hidden = await _odcs_ids(url, FolderScope(all=True, excluded_subtrees=frozenset({org_a})))
        assert id_b in hidden
        assert id_a not in hidden
        assert id_a2 not in hidden

        # an exact folder inside the excluded subtree is added back; its sibling team is not
        back = await _odcs_ids(
            url,
            FolderScope(
                all=True,
                folder_ids=frozenset({team_a}),
                excluded_subtrees=frozenset({org_a}),
            ),
        )
        assert id_a in back
        assert id_b in back
        assert id_a2 not in back

        # without all, only the exact folders are visible and exclusions are irrelevant
        only = await _odcs_ids(url, FolderScope(all=False, folder_ids=frozenset({team_a})))
        assert only == [id_a]


async def test_empty_scope_issues_no_query(make_stack: StackFactory) -> None:
    async with make_stack() as stack:

        class Exploding:
            async def execute(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("an empty scope must not reach the database")

        empty = FolderScope(all=False, excluded_subtrees=frozenset({uuid.uuid4()}))
        rows, more = await search_contracts(
            Exploding(),  # type: ignore[arg-type]
            ContractSearchFilters(),
            metadata_scope=empty,
            content_scope=empty,
        )
        assert (rows, more) == ([], False)
        assert stack.contracts_db_url
