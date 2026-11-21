"""Access events for contract content reads (AUD-04, D-171).

Every 200 response that returns contract document content calls :func:`record_content_read` just
before it returns. The helper builds one value-free envelope and hands it to the app's
``dc_core.access.AccessLog`` (``app.state.access_log``), which publishes it later, best effort, on
``access.contracts.contract.read``. It never awaits and never raises: a failure is logged with its
error class and swallowed, so an access event can never change a read. Without NATS
(``app.state.access_log`` None) it does nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

import structlog
from asgi_correlation_id import correlation_id
from dc_auth.deps import current_decision
from dc_auth.principal import principal_to_actor
from dc_core.events import build_access_envelope, subject_for
from fastapi import Request

_log = structlog.get_logger("dc_contracts.access_events")

ACCESS_SUBJECT: Final = subject_for("contracts", "contract", "read", prefix="access")
DETAIL_KEYS: Final = frozenset(
    {"contract_id", "version_id", "draft_id", "semver", "ref", "media_type"}
)
DETAIL_MAX_CHARS: Final = 512


def record_content_read(
    request: Request,
    *,
    contract_id: uuid.UUID,
    ref: str,
    media_type: str,
    version_id: uuid.UUID | None = None,
    semver: str | None = None,
    draft_id: uuid.UUID | None = None,
    scope_path: Sequence[uuid.UUID] | None = None,
) -> None:
    """Enqueue one access event for a content read; a no-op without an access log.

    ``ref`` is ``latest``, the resolved SemVer, ``head`` or ``draft``, never the raw client input.
    Only :data:`DETAIL_KEYS` are emitted, absent values are omitted and strings are cut at
    :data:`DETAIL_MAX_CHARS`. ``scope_path`` defaults to the ancestors of the route decision's
    folder; a read of another folder's contract (the create-from-version source, WR-04) passes
    that contract folder's ancestors, so the event is visible to the source's auditors.
    """
    try:
        access_log = getattr(request.app.state, "access_log", None)
        if access_log is None:
            return
        values = {
            "contract_id": contract_id,
            "version_id": version_id,
            "draft_id": draft_id,
            "semver": semver,
            "ref": ref,
            "media_type": media_type,
        }
        detail = {
            key: str(value)[:DETAIL_MAX_CHARS]
            for key, value in values.items()
            if key in DETAIL_KEYS and value is not None
        }
        if scope_path is None:
            folder = current_decision(request).folder
            scope_path = folder.ancestor_ids if folder is not None else ()
        envelope = build_access_envelope(
            service="contracts",
            aggregate_type="contract",
            aggregate_id=str(contract_id),
            action="read",
            actor=principal_to_actor(request.state.principal).model_dump(),
            scope_path=scope_path,
            request_id=correlation_id.get(),
            detail=detail,
        )
        access_log.record(ACCESS_SUBJECT, envelope)
    except Exception as exc:
        # Only the error class: never the envelope, the principal or the document.
        _log.warning("access_event_not_recorded", error=type(exc).__name__)


__all__ = ["ACCESS_SUBJECT", "DETAIL_KEYS", "DETAIL_MAX_CHARS", "record_content_read"]
