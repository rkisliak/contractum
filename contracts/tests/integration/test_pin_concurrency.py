"""Races between subscribing and the owner's unpublish or delete (SUB-06, T-04.1-19).

Subscribe takes the contract lock and unpublish / delete take it too, then look for pins before
any write, so a pair of concurrent requests always ends in one of two legal states: the pin
committed first and the owner is refused, or the owner finished first and the subscribe finds the
version unpublished or the contract deleted. ``INVARIANT_SQL`` must find no active pin on an
unpublished version or a deleted contract afterwards, and a seeded random walk over every
lifecycle and subscription operation checks it after every single step.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
Invariant = Callable[[str], list[dict[str, Any]]]

CONTRACTS = "/api/v1/contracts/contracts"
CARDS = "/api/v1/contracts/cards"
ITERATIONS_UNPUBLISH = 20
ITERATIONS_DELETE = 10
STEPS = 40


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _unpublish(stack: Any, contract: str, semver: str) -> httpx.Response:
    return await asyncio.wait_for(
        stack.client.post(
            f"{CONTRACTS}/{contract}/versions/{semver}/unpublish",
            headers=bearer(stack.admin_token),
        ),
        30,
    )


async def _delete(stack: Any, contract: str) -> httpx.Response:
    return await asyncio.wait_for(
        stack.client.delete(f"{CONTRACTS}/{contract}", headers=bearer(stack.admin_token)), 30
    )


async def test_subscribe_racing_unpublish_never_leaves_a_pin_on_an_unpublished_version(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        org = await stack.chain()
        previews = [f"1.{i}.0" for i in range(1, ITERATIONS_UNPUBLISH + 1)]
        contract, _ = await stack.publish_versions(org.team, ["1.0.0", *previews])
        card = await stack.create_card(org.team)

        for semver in previews:
            subscribed, unpublished = await asyncio.gather(
                stack.subscribe(card["id"], contract, semver), _unpublish(stack, contract, semver)
            )
            outcome = (subscribed.status_code, unpublished.status_code)
            assert outcome in {(201, 409), (404, 200), (409, 200)}, (semver, outcome)

        assert invariant(stack.contracts_db_url) == []
        # Every refused unpublish left its version published.
        states = sql(
            stack.contracts_db_url,
            "SELECT v.semver, v.state, count(s.id) FILTER (WHERE s.ended_at IS NULL) AS pins "
            "FROM contract_version v LEFT JOIN card_subscription s ON s.version_id = v.id "
            "WHERE v.contract_id = %s GROUP BY v.semver, v.state",
            contract,
        )
        assert all(not (r["pins"] and r["state"] != "published") for r in states)


async def test_subscribe_racing_delete_never_leaves_a_pin_on_a_deleted_contract(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        org = await stack.chain()
        card = await stack.create_card(org.team)
        contracts = [
            (await stack.publish_versions(org.team, ["1.0.0"]))[0] for _ in range(ITERATIONS_DELETE)
        ]

        for contract in contracts:
            subscribed, deleted = await asyncio.gather(
                stack.subscribe(card["id"], contract, "1.0.0"), _delete(stack, contract)
            )
            outcome = (subscribed.status_code, deleted.status_code)
            assert outcome in {(201, 409), (404, 204)}, (contract, outcome)

        assert invariant(stack.contracts_db_url) == []


def _unpublish_cleared_every_pin(sql: Sql, stack: Any) -> list[dict[str, Any]]:
    """Pins that were active at the moment their version was unpublished (must be none)."""
    return sql(
        stack.contracts_db_url,
        "SELECT s.id FROM card_subscription s JOIN contract_version v ON v.id = s.version_id "
        "WHERE v.unpublished_at IS NOT NULL AND s.created_at < v.unpublished_at "
        "AND (s.ended_at IS NULL OR s.ended_at > v.unpublished_at)",
    )


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_a_seeded_random_walk_keeps_the_invariants_after_every_step(
    make_stack: MakeStack, sql: Sql, invariant: Invariant, seed: int
) -> None:
    rng = random.Random(seed)  # noqa: S311 - a seeded, reproducible walk, not a secret
    async with make_stack() as stack:
        org = await stack.chain()
        semvers = ["1.0.0", "1.1.0", "1.2.0", "1.3.0"]
        contracts = [(await stack.publish_versions(org.team, semvers))[0] for _ in range(2)]
        cards = [(await stack.create_card(org.team))["id"] for _ in range(3)]
        admin = bearer(stack.admin_token)

        def active_pins() -> list[dict[str, Any]]:
            return sql(
                stack.contracts_db_url,
                "SELECT id::text, card_id::text, contract_id::text FROM card_subscription "
                "WHERE ended_at IS NULL ORDER BY id",
            )

        for step in range(STEPS):
            contract = rng.choice(contracts)
            semver = rng.choice(semvers)
            card = rng.choice(cards)
            pins = active_pins()
            op = rng.choice(
                [
                    "subscribe",
                    "subscribe",
                    "subscribe",
                    "move",
                    "unsubscribe",
                    "remove",
                    "promote",
                    "deprecate",
                    "unpublish",
                    "unpublish",
                    "archive",
                ]
            )
            if op in {"move", "unsubscribe", "remove"} and not pins:
                op = "subscribe"
            if op == "archive" and rng.random() < 0.7:
                op = "subscribe"  # keep most cards alive

            if op == "subscribe":
                response = await stack.subscribe(card, contract, semver)
            elif op == "move":
                pin = rng.choice(pins)
                response = await stack.client.post(
                    f"{CARDS}/{pin['card_id']}/subscriptions/{pin['id']}/move",
                    json={"version": semver},
                    headers=admin,
                )
            elif op == "unsubscribe":
                pin = rng.choice(pins)
                response = await stack.client.post(
                    f"{CARDS}/{pin['card_id']}/subscriptions/{pin['id']}/unsubscribe",
                    json={},
                    headers=admin,
                )
            elif op == "remove":
                pin = rng.choice(pins)
                response = await stack.client.post(
                    f"{CONTRACTS}/{pin['contract_id']}/subscriptions/{pin['id']}/remove",
                    json={"reason": "walk"},
                    headers=admin,
                )
            elif op == "promote":
                response = await stack.client.post(
                    f"{CONTRACTS}/{contract}/versions/{semver}/promote", headers=admin
                )
            elif op == "deprecate":
                response = await stack.client.post(
                    f"{CONTRACTS}/{contract}/versions/{semver}/deprecate",
                    json={"sunset_date": "2099-01-01"},
                    headers=admin,
                )
            elif op == "unpublish":
                response = await _unpublish(stack, contract, semver)
            else:
                response = await stack.client.post(f"{CARDS}/{card}/archive", headers=admin)

            where = f"seed {seed} step {step}: {op} -> {response.status_code} {response.text}"
            assert response.status_code < 500, where
            assert invariant(stack.contracts_db_url) == [], where
            assert _unpublish_cleared_every_pin(sql, stack) == [], where
