"""Release of a session advisory lock held on a pooled engine connection.

A session-level ``pg_try_advisory_lock`` belongs to the Postgres backend, not to the SQLAlchemy
``AsyncConnection``. ``AsyncConnection.close()`` on a pooled engine only returns the backend to the
pool, where it stays open with the lock still held, so no other replica could ever take the lock
over. The background loops therefore unlock explicitly, and drop the backend when that is not
possible (a dead connection, a failed statement), so the server ends the session and frees the lock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import (
        AsyncConnection,
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
    )

_log = structlog.get_logger("dc_contracts.advisory_lock")


async def release_session_lock(conn: AsyncConnection, key: str) -> None:
    """Unlock ``key`` and give the connection back; on any failure close the backend itself.

    Raises the original error after the fallback so the caller can log it.
    """
    try:
        try:
            await conn.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": key})
        except BaseException:
            # The unlock did not happen, so returning this backend to the pool would keep the lock.
            # ``invalidate`` closes the DBAPI connection; Postgres then drops the session lock.
            await conn.invalidate()
            raise
    finally:
        await conn.close()


async def try_session_lock(
    session_factory: async_sessionmaker[AsyncSession], key: str, owner: str
) -> AsyncConnection | None:
    """Take the session advisory lock ``key`` on one dedicated AUTOCOMMIT connection.

    Returns the connection (held for the caller's life, released with ``close_session_lock``) or
    ``None`` when another holder has the lock; the connection is closed in that case and on any
    failure. ``owner`` names the caller in the error raised for a factory without an engine.
    """
    engine: AsyncEngine | None = session_factory.kw.get("bind")
    if engine is None:
        raise RuntimeError(f"{owner} needs a session factory bound to an engine")
    conn = await engine.connect()
    try:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        got = (
            await conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": key})
        ).scalar_one()
    except BaseException:
        await conn.close()
        raise
    if not got:
        await conn.close()
        return None
    return conn


async def close_session_lock(conn: AsyncConnection | None, key: str, log_event: str) -> None:
    """Release a lock taken by ``try_session_lock``; a failed release is logged as ``log_event``."""
    if conn is None:
        return
    try:
        # Closing a pooled connection does not end the Postgres session: unlock explicitly.
        await release_session_lock(conn, key)
    except Exception as exc:
        _log.warning(log_event, error=type(exc).__name__)
