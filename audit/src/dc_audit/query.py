"""The public audit query: filters, cursor codec, visibility predicate and keyset statement.

AUD-05, D-166..D-168. One statement per request with every value a bound parameter. Visibility
is one SQL predicate built from identity's compact scope for ``audit:query``; pages are never
post-filtered in Python and no error ever falls back to an unfiltered query.

Ordering is by occurrence time, newest first, with the row id as the tie-breaker. An event that
arrives late (held in a producer's outbox during a broker outage) can land behind a page a client
already passed; that is accepted and documented (research Pitfall 15).
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from dc_auth.authorizer import FolderScope
from dc_core.problems import ProblemError
from sqlalchemy import (
    ColumnElement,
    Select,
    any_,
    literal,
    literal_column,
    not_,
    or_,
    select,
    true,
    tuple_,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from dc_audit.models import AuditEvent

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
MAX_TEXT_LENGTH = 256
MAX_CURSOR_LENGTH = 200

Category = Literal["change", "access", "denial"]


def _invalid(pointer: str, detail: str) -> ProblemError:
    return ProblemError(
        422,
        "validation-error",
        "Unprocessable Content",
        "The request failed validation.",
        errors=[{"pointer": pointer, "detail": detail}],
    )


def like_escape(text: str) -> str:
    """Escape ``\\``, ``%`` and ``_`` so ``text`` matches only itself inside a LIKE pattern.

    The backslash goes first: escaping it after the other two would double the escapes just added.
    """
    return text.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


# --- cursor ----------------------------------------------------------------------------------


def encode_cursor(at: datetime, row_id: int) -> str:
    """Opaque cursor: base64url, without padding, of ``"<UTC isoformat>|<id>"``."""
    raw = f"{at.astimezone(UTC).isoformat()}|{row_id}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> tuple[datetime, int]:
    """Inverse of :func:`encode_cursor`. Anything else is a 422 (T-7-17).

    Only the canonical spelling is accepted (re-encoding must give back ``raw``), so a position
    has exactly one cursor: padding, extra fields, a non-UTC offset or a non-canonical integer
    are refused, and a naive time is refused before it could be read in a local zone.
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        text = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True).decode(
            "ascii"
        )
        stamp, separator, number = text.partition("|")
        if not separator:
            raise ValueError("no id")
        at = datetime.fromisoformat(stamp)
        if at.tzinfo is None:
            raise ValueError("naive time")
        row_id = int(number)
        if encode_cursor(at, row_id) != raw:
            raise ValueError("not the canonical cursor")
    except (ValueError, binascii.Error, UnicodeError):
        raise _invalid("/query/cursor", "cursor is not valid") from None
    return at, row_id


# --- filters ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditFilters:
    """Optional filters, combined with AND. ``from_``/``to`` are the half-open ``[from, to)``."""

    aggregate_type: str | None = None
    aggregate_id: str | None = None
    actor_id: str | None = None
    service: str | None = None
    action: str | None = None
    category: Category | None = None
    from_: datetime | None = None
    to: datetime | None = None


def make_filters(
    *,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
    actor_id: str | None = None,
    service: str | None = None,
    action: str | None = None,
    category: Category | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> AuditFilters:
    """Validated filters, or a 422 ``validation-error`` naming the offending query parameter.

    NUL characters are refused (PostgreSQL cannot bind them in text), time bounds must carry an
    offset, and ``from`` must not be after ``to`` (``from == to`` is a valid, empty window).
    """
    texts = {
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "actor_id": actor_id,
        "service": service,
        "action": action,
    }
    for name, value in texts.items():
        if value is not None and "\x00" in value:
            raise _invalid(f"/query/{name}", "NUL (U+0000) characters are not allowed")
    for name, bound in (("from", from_), ("to", to)):
        if bound is not None and bound.tzinfo is None:
            raise _invalid(f"/query/{name}", "a time zone offset is required (RFC 3339)")
    if from_ is not None and to is not None and from_ > to:
        raise _invalid("/query/from", "from must not be after to")
    return AuditFilters(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        actor_id=actor_id,
        service=service,
        action=action,
        category=category,
        from_=from_,
        to=to,
    )


def _action_condition(action: str) -> ColumnElement[bool]:
    """A trailing dot means a dotted prefix (``contract.version.``), else an exact action."""
    if action.endswith("."):
        return AuditEvent.action.like(like_escape(action) + "%", escape="\\")
    return AuditEvent.action == action


def _uuid_array(ids: frozenset[uuid.UUID]) -> ColumnElement[list[uuid.UUID]]:
    return literal(sorted(ids, key=str), ARRAY(PG_UUID(as_uuid=True)))


# --- visibility and statement ----------------------------------------------------------------


def visible(scope: FolderScope) -> ColumnElement[bool] | None:
    """WHERE clause for the events ``scope`` may see, or ``None`` for an empty page (no query).

    A local copy of ``dc_contracts.search.scope_clause`` (audit cannot import contracts), proven
    equal to ``dc_auth.oracle`` by the D-186 property test. It mirrors ``FolderScope.covers``:
    ``(all AND scope_path does not overlap excluded_subtrees) OR leaf_folder_id IN folder_ids``.
    ``scope_path`` is root-first and ends with the event's own folder, so an excluded folder hides
    itself and everything below it. An event with an empty ``scope_path`` has a NULL leaf and one
    with ``[root]`` has the ROOT leaf, which is never in ``folder_ids``: both are visible only
    through the ``all`` branch, that is to holders of ``audit:query`` at ROOT (D-167).
    """
    if scope.is_empty:
        return None
    parts: list[ColumnElement[bool]] = []
    if scope.all:
        if scope.excluded_subtrees:
            excluded = _uuid_array(scope.excluded_subtrees)
            parts.append(not_(AuditEvent.scope_path.overlap(excluded)))
        else:
            parts.append(true())
    if scope.folder_ids:
        parts.append(AuditEvent.leaf_folder_id == any_(_uuid_array(scope.folder_ids)))
    return or_(*parts)


def build_statement(
    predicate: ColumnElement[bool],
    filters: AuditFilters,
    limit: int,
    *,
    cursor: tuple[datetime, int] | None = None,
) -> Select[AuditEvent]:
    """Newest first by ``(occurred_at, id)``; one extra row tells whether another page exists."""
    stmt = select(AuditEvent).where(predicate)
    if filters.aggregate_type is not None:
        stmt = stmt.where(AuditEvent.aggregate_type == filters.aggregate_type)
    if filters.aggregate_id is not None:
        stmt = stmt.where(AuditEvent.aggregate_id == filters.aggregate_id)
    if filters.actor_id is not None:
        # Spelled exactly like ix_audit_event_actor_time's expression so the index applies.
        actor_id = AuditEvent.actor.op("->>")(literal_column("'id'"))
        stmt = stmt.where(actor_id == filters.actor_id)
    if filters.service is not None:
        stmt = stmt.where(AuditEvent.service == filters.service)
    if filters.action is not None:
        stmt = stmt.where(_action_condition(filters.action))
    if filters.category is not None:
        stmt = stmt.where(AuditEvent.category == filters.category)
    if filters.from_ is not None:
        stmt = stmt.where(AuditEvent.occurred_at >= filters.from_)
    if filters.to is not None:
        stmt = stmt.where(AuditEvent.occurred_at < filters.to)
    if cursor is not None:
        at, row_id = cursor
        stmt = stmt.where(tuple_(AuditEvent.occurred_at, AuditEvent.id) < tuple_(at, row_id))
    return stmt.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(limit + 1)


def next_cursor(rows: list[AuditEvent], limit: int) -> str | None:
    """The cursor after the last returned row, non-null exactly when the extra row exists."""
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return encode_cursor(last.occurred_at, last.id)
