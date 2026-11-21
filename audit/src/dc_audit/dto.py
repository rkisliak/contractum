"""Audit response models (D-169), shared by the public query and the internal read.

Module-unique names (``AuditEventOut``, ``AuditEventPageOut``) keep the gateway's OpenAPI merge
collision-free. Snapshots are returned exactly as stored: they are metadata-only by construction
(AUD-03), so there is no redaction layer here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditEventOut(BaseModel):
    """One stored audit event: the envelope plus ``received_at``, ``category`` and ``changes``."""

    model_config = ConfigDict(from_attributes=True)

    event_id: uuid.UUID
    occurred_at: datetime
    received_at: datetime
    service: str
    category: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int | None
    action: str
    actor: dict[str, Any]
    request_id: str | None
    scope_path: list[uuid.UUID]
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    changes: list[dict[str, Any]]
    detail: dict[str, Any] | None


class AuditEventPageOut(BaseModel):
    """A keyset page, newest first. ``next_cursor`` is non-null exactly when more rows exist."""

    items: list[AuditEventOut]
    next_cursor: str | None
