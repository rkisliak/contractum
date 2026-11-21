"""SUB-03 / SUB-04 (04.1-05 gap): a card GET racing a pin move never sees a torn pin list.

A move ends the old pin and inserts the new one in one transaction, and the card read embeds its
pins in one statement. So every GET, whenever it lands during a stream of moves, must show exactly
one active pin, and every ended pin's ``replaced_by_id`` must name a pin in the same response.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
CARDS = "/api/v1/contracts/cards"
MOVES = 10
READS = 40


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_every_card_read_during_a_stream_of_moves_is_a_consistent_snapshot(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _versions = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
        card = await stack.create_card(chain.team)
        pinned = await stack.subscribe(card["id"], contract, "2.3.0")
        assert pinned.status_code == 201, pinned.text
        card_id = card["id"]
        auth = bearer(stack.admin_token)

        async def mover() -> None:
            current = pinned.json()["id"]
            targets = ["3.0.0", "2.3.0"]
            for step in range(MOVES):
                response = await stack.client.post(
                    f"{CARDS}/{card_id}/subscriptions/{current}/move",
                    json={"version": targets[step % 2]},
                    headers=auth,
                )
                assert response.status_code == 200, response.text
                current = response.json()["id"]

        async def reader() -> list[list[dict[str, Any]]]:
            seen: list[list[dict[str, Any]]] = []
            for _ in range(READS):
                response = await stack.client.get(f"{CARDS}/{card_id}", headers=auth)
                assert response.status_code == 200, response.text
                seen.append(response.json()["subscriptions"])
                await asyncio.sleep(0)
            return seen

        _, snapshots = await asyncio.gather(mover(), reader())

        assert snapshots
        for subscriptions in snapshots:
            active = [s for s in subscriptions if s["ended_at"] is None]
            assert len(active) == 1, subscriptions
            ids = {s["id"] for s in subscriptions}
            for sub in subscriptions:
                if sub["ended_at"] is not None and sub["replaced_by_id"] is not None:
                    assert sub["replaced_by_id"] in ids, subscriptions

        # The final state after the race: the last pin is the one active.
        final = await stack.client.get(f"{CARDS}/{card_id}", headers=auth)
        assert len([s for s in final.json()["subscriptions"] if s["ended_at"] is None]) == 1
