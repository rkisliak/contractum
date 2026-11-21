"""The contract row lock every lifecycle write takes first (``versions`` re-exports it).

A leaf module (it imports only the model), so modules that ``versions`` imports can lock the
contract too without a cycle. See the ``contract-lifecycle`` skill for the lock order.
"""

from __future__ import annotations

import uuid
from typing import Literal

from dc_core.problems import ProblemError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.models import Contract


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


async def lock_contract(
    session: AsyncSession,
    contract_id: uuid.UUID,
    *,
    mode: Literal["no_key_update", "update"] = "no_key_update",
    include_deleted: bool = False,
) -> Contract:
    """The contract row under a lock; 404 when absent or soft-deleted (D-55).

    A deleted contract is absent for every write; only restore asks for ``include_deleted``.

    ``no_key_update`` (``FOR NO KEY UPDATE``) is the lifecycle-write lock: it serialises writers
    of the same contract without blocking foreign-key checks from rows that reference it.
    ``update`` (``FOR UPDATE``) is for deleting the contract. ``populate_existing`` makes the
    locked read overwrite a stale identity-map copy (research Pitfall 1).
    """
    stmt = select(Contract).where(Contract.id == contract_id)
    if not include_deleted:
        stmt = stmt.where(Contract.deleted_at.is_(None))
    stmt = stmt.with_for_update() if mode == "update" else stmt.with_for_update(key_share=True)
    contract = (
        await session.execute(stmt.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if contract is None:
        raise _not_found()
    return contract
