"""Audit event envelope, subject naming and the producer-side transport seam (D-21).

Producers (the outbox relay) depend only on :class:`EventPublisher`. NATS-specific code lives in
:class:`JetStreamPublisher` (here) and in ``dc_audit.consumer``, so swapping the transport for the
documented fallback (outbox -> HTTP push, or a Postgres queue) adds adapter classes only.

Subject scheme: ``<prefix>.<service>.<aggregate_type>.<action tokens>``. The prefix is ``audit``
for change events (stream ``AUDIT``, outbox relays) and ``access`` for best-effort access events
(stream ``ACCESS``). Actions are dotted (``widget.created``) and every dot-separated segment becomes
its own NATS subject token, so stream subjects and publish permissions always use the multi-token
wildcard ``>``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

import uuid_utils.compat as uuid_compat

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient
    from nats.js import JetStreamContext

AUDIT_STREAM = "AUDIT"
ACCESS_STREAM = "ACCESS"
SUBJECT_PREFIXES = ("audit", "access")
SUBJECT_TOKEN = r"^[A-Za-z0-9_-]+$"  # noqa: S105 - a regex, not a secret
ENVELOPE_SCHEMA_VERSION = 1

_TOKEN = re.compile(SUBJECT_TOKEN)


def _check_token(part: str, value: str) -> str:
    # fullmatch semantics via ^...$: also rejects a trailing newline ("$" would allow it).
    if not _TOKEN.fullmatch(value):
        raise ValueError(f"invalid subject token for {part}: {value!r}")
    return value


def subject_for(
    service: str,
    aggregate_type: str,
    action: str,
    *,
    prefix: Literal["audit", "access"] = "audit",
) -> str:
    """``<prefix>.<service>.<aggregate_type>.<action>`` with dotted actions split into tokens.

    ``prefix`` is ``audit`` (the default, change events) or ``access`` (access events); any other
    value raises ``ValueError`` naming ``prefix``. ``service`` and ``aggregate_type`` are single
    tokens; every segment of ``action`` must match ``SUBJECT_TOKEN``. Wildcards (``*``, ``>``),
    whitespace, empty segments and dots inside ``service`` or ``aggregate_type`` raise
    ``ValueError`` naming the offending part.
    """
    if prefix not in SUBJECT_PREFIXES:
        raise ValueError(f"invalid subject prefix: {prefix!r}")
    _check_token("service", service)
    _check_token("aggregate_type", aggregate_type)
    segments = action.split(".")
    for segment in segments:
        _check_token("action", segment)
    return ".".join((prefix, service, aggregate_type, *segments))


def _iso(value: datetime) -> str:
    return value.isoformat()


def build_envelope(row: Any, *, service: str) -> dict[str, Any]:
    """JSON-safe envelope of one outbox row (UUIDs and datetimes as strings).

    ``detail`` is added only when the row carries a non-null ``detail`` attribute (identity's
    denial rows); every other producer's envelope keeps exactly the 13 baseline keys.
    """
    envelope: dict[str, Any] = {
        "event_id": str(row.event_id),
        "occurred_at": _iso(row.occurred_at),
        "service": service,
        "aggregate_type": row.aggregate_type,
        "aggregate_id": row.aggregate_id,
        "aggregate_version": row.aggregate_version,
        "scope_path": [str(item) for item in row.scope_path],
        "action": row.action,
        "actor": row.actor,
        "request_id": row.request_id,
        "before": row.before,
        "after": row.after,
        "schema_version": row.schema_version,
    }
    detail = getattr(row, "detail", None)
    if detail is not None:
        envelope["detail"] = detail
    return envelope


def build_access_envelope(
    *,
    service: str,
    aggregate_type: str,
    aggregate_id: str,
    action: str,
    actor: dict[str, Any],
    scope_path: Iterable[object],
    request_id: str | None,
    detail: dict[str, Any],
) -> dict[str, Any]:
    """JSON-safe envelope of one best-effort access event (D-171), for the ``access.`` subjects.

    A fresh UUIDv7 ``event_id`` (also the ``Nats-Msg-Id``), the current UTC time, ``category``
    ``access``, ``before`` and ``after`` always None (the audit ingest refuses content on access
    events) and ``schema_version`` 1. ``detail`` must already be flat and allow-listed by the
    producer: this function copies it, it never adds document content.
    """
    return {
        "event_id": str(uuid_compat.uuid7()),
        "occurred_at": _iso(datetime.now(UTC)),
        "service": service,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": None,
        "scope_path": [str(item) for item in scope_path],
        "action": action,
        "actor": actor,
        "request_id": request_id,
        "before": None,
        "after": None,
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "category": "access",
        "detail": dict(detail),
    }


def encode_envelope(envelope: dict[str, Any]) -> bytes:
    """Compact UTF-8 JSON."""
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class PublishAck:
    duplicate: bool


class EventPublisher(Protocol):
    """The narrow seam the relay publishes through (D-21)."""

    async def publish(self, subject: str, payload: bytes, *, msg_id: str) -> PublishAck: ...

    async def close(self) -> None: ...


class JetStreamPublisher:
    """JetStream implementation of :class:`EventPublisher`.

    Connects lazily on first use. ``Nats-Msg-Id`` carries the event id so JetStream's duplicate
    window absorbs republishes (the unique ``event_id`` in the audit store is the real guarantee).
    """

    def __init__(
        self,
        nats_url: str,
        *,
        user: str | None = None,
        password: str | None = None,
        stream: str = AUDIT_STREAM,
        timeout_s: float = 5.0,
    ) -> None:
        self._url = nats_url
        self._user = user
        self._password = password
        self._stream = stream
        self._timeout_s = timeout_s
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._lock = asyncio.Lock()

    async def _jetstream(self) -> JetStreamContext:
        async with self._lock:
            if self._js is None:
                import nats  # NATS is imported here only (D-21: the relay never imports it)

                self._nc = await nats.connect(
                    servers=[self._url],
                    user=self._user,
                    password=self._password,
                    max_reconnect_attempts=-1,
                    connect_timeout=self._timeout_s,
                )
                self._js = self._nc.jetstream()
            return self._js

    async def publish(self, subject: str, payload: bytes, *, msg_id: str) -> PublishAck:
        js = await self._jetstream()
        ack = await js.publish(
            subject,
            payload,
            headers={"Nats-Msg-Id": msg_id},
            stream=self._stream,
            timeout=self._timeout_s,
        )
        return PublishAck(duplicate=bool(ack.duplicate))

    async def close(self) -> None:
        async with self._lock:
            nc, self._nc, self._js = self._nc, None, None
        if nc is not None and not nc.is_closed:
            try:
                await nc.close()
            except Exception:  # noqa: S110 - best effort on shutdown
                pass
