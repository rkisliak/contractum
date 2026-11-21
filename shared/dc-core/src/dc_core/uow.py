"""Unit of work, audit columns and the transactional outbox (AUD-01, AUD-02, AUD-03).

Rules encoded here:

* One explicit audit event per business operation (``UnitOfWork.audit``), not one per dirty row.
* ``created_by/created_at/updated_by/updated_at`` are stamped in ``before_flush`` from the
  session actor and the database clock, overwriting anything client code assigned.
* A ``before_commit`` tripwire raises when audited rows are written without an outbox row.
* Snapshots are built from each model's ``__audit_fields__`` allow-list only, so secret
  columns can never reach an outbox row.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar

import uuid_utils.compat as uuid_compat
from asgi_correlation_id import correlation_id
from sqlalchemy import BigInteger, DateTime, Identity, Integer, Text, event, func, inspect, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from dc_core.actor import Actor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ACTOR = "actor"
_READ_ONLY = "read_only"
_AUDITED_WRITE = "audited_write"
_OUTBOX_WRITTEN = "outbox_written"


class AuditColumnsMixin:
    """Audit columns for every audited table.

    Declare ``__audit_fields__`` (an allow-list of attribute names) on each model; password
    hashes, token hashes and signing keys must never be listed.
    """

    __audit_fields__: ClassVar[tuple[str, ...]] = ()
    __audit_event_required__: ClassVar[bool] = True
    # created_at/updated_at are SQL expressions (now()): fetch them with RETURNING so reading
    # them after a flush never needs a lazy load, which would fail in async code.
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}

    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutboxMixin:
    """Columns of the per-service ``outbox`` table (the Alembic helper creates the same)."""

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), unique=True, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    scope_path: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    actor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # none_as_null: a create event (before=None) is SQL NULL, not the JSON value null.
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class AuditTripwireError(RuntimeError):
    """Audited rows were written in a transaction that has no outbox row."""


class AuditedSession(Session):
    """Sync session class behind the async sessions; carries the audit hooks."""


def _is_audited(obj: object) -> bool:
    return isinstance(obj, AuditColumnsMixin)


def _event_required(obj: object) -> bool:
    return _is_audited(obj) and bool(getattr(type(obj), "__audit_event_required__", True))


def _require_actor(session: Session) -> Actor:
    actor = session.info.get(_ACTOR)
    if not isinstance(actor, Actor):
        raise RuntimeError("audited write without an actor: session.info['actor'] is not set")
    return actor


@event.listens_for(AuditedSession, "before_flush")
def _stamp_audit_columns(session: Session, flush_context: Any, instances: Any) -> None:
    if session.info.get(_READ_ONLY) and (session.new or session.dirty or session.deleted):
        raise RuntimeError("write attempted on a read-only session (no actor)")

    for obj in session.new:
        if isinstance(obj, OutboxMixin):
            session.info[_OUTBOX_WRITTEN] = True
        if isinstance(obj, AuditColumnsMixin):
            actor = _require_actor(session)
            # Overwrite whatever client code assigned: actor and clock come from the server.
            obj.created_by = actor.id
            obj.updated_by = actor.id
            obj.created_at = func.now()
            obj.updated_at = func.now()
            if _event_required(obj):
                session.info[_AUDITED_WRITE] = True

    for obj in session.dirty:
        if not isinstance(obj, AuditColumnsMixin) or not session.is_modified(obj):
            continue
        actor = _require_actor(session)
        # created_* are immutable after insert: undo any client change to them.
        for name in ("created_by", "created_at"):
            hist = _history_deleted(obj, name)
            if hist is not _MISSING:
                setattr(obj, name, hist)
        obj.updated_by = actor.id
        obj.updated_at = func.now()
        if _event_required(obj):
            session.info[_AUDITED_WRITE] = True

    for obj in session.deleted:
        if isinstance(obj, AuditColumnsMixin):
            _require_actor(session)
            if _event_required(obj):
                session.info[_AUDITED_WRITE] = True


_MISSING = object()


def _history_deleted(obj: object, name: str) -> object:
    hist = inspect(obj).attrs[name].history
    if hist.has_changes() and hist.deleted:
        return hist.deleted[0]
    return _MISSING


@event.listens_for(AuditedSession, "before_commit")
def _audit_tripwire(session: Session) -> None:
    audited = bool(session.info.get(_AUDITED_WRITE)) or any(
        _event_required(o) for o in (*session.new, *session.deleted)
    )
    if not audited:
        audited = any(_event_required(o) and session.is_modified(o) for o in session.dirty)
    if not audited:
        return
    outbox = bool(session.info.get(_OUTBOX_WRITTEN)) or any(
        isinstance(o, OutboxMixin) for o in session.new
    )
    if not outbox:
        raise AuditTripwireError(
            "audited rows were written without an outbox row: call UnitOfWork.audit() before commit"
        )


@event.listens_for(AuditedSession, "after_commit")
@event.listens_for(AuditedSession, "after_rollback")
def _reset_tripwire_flags(session: Session) -> None:
    """Tripwire state is per transaction: a later commit must not inherit an earlier one's flags.

    Otherwise an outbox row written by the first commit of a unit would let a second audited
    write with no outbox row pass (WR-07).
    """
    session.info.pop(_AUDITED_WRITE, None)
    session.info.pop(_OUTBOX_WRITTEN, None)


@dataclass(frozen=True)
class _PendingEvent:
    action: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int | None
    scope_path: tuple[uuid.UUID, ...]
    before: dict[str, Any] | None
    after: dict[str, Any] | None


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return _jsonable(value.value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v) for v in value]
    return value


class UnitOfWork:
    """One per request: an AsyncSession bound to an actor, one audit event, one commit."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        actor: Actor,
        outbox_model: type,
        service: str,
        request_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.actor = actor
        self._outbox_model = outbox_model
        self.service = service
        self.request_id = request_id
        self._event: _PendingEvent | None = None
        self._session: AsyncSession | None = None
        self.committed = False

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork used outside 'async with'")
        return self._session

    async def __aenter__(self) -> UnitOfWork:
        self._session = self._session_factory()
        self._session.sync_session.info[_ACTOR] = self.actor
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._session
        if session is None:
            return
        try:
            # Anything not committed (including every exception path) is rolled back.
            await session.rollback()
        finally:
            await session.close()
            self._session = None

    def audit(
        self,
        action: str,
        aggregate_type: str,
        aggregate_id: str | uuid.UUID,
        *,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        scope_path: Sequence[uuid.UUID] = (),
        aggregate_version: int | None = None,
    ) -> None:
        if self._event is not None:
            raise RuntimeError("UnitOfWork.audit() may be called once per unit of work")
        self._event = _PendingEvent(
            action=action,
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            aggregate_version=aggregate_version,
            scope_path=tuple(scope_path),
            before=before,
            after=after,
        )

    def has_pending(self) -> bool:
        if self.committed or self._session is None:
            return False
        s = self._session.sync_session
        return self._event is not None or bool(s.new or s.dirty or s.deleted)

    async def commit(self) -> None:
        session = self.session
        try:
            await session.flush()
            ev = self._event
            if ev is not None:
                session.add(
                    self._outbox_model(
                        event_id=uuid_compat.uuid7(),
                        aggregate_type=ev.aggregate_type,
                        aggregate_id=ev.aggregate_id,
                        aggregate_version=ev.aggregate_version,
                        action=ev.action,
                        scope_path=list(ev.scope_path),
                        actor=self.actor.model_dump(),
                        request_id=self.request_id or correlation_id.get(),
                        before=ev.before,
                        after=ev.after,
                    )
                )
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        self._event = None
        self.committed = True

    async def commit_if_pending(self) -> None:
        """Commit work the handler did not commit itself; no-op for an untouched unit."""
        if self.has_pending():
            await self.commit()

    @staticmethod
    def snapshot(obj: object) -> dict[str, Any]:
        """Allow-listed, JSON-safe view of an audited model instance."""
        fields: tuple[str, ...] = getattr(type(obj), "__audit_fields__", ())
        return {name: _jsonable(getattr(obj, name)) for name in fields}
