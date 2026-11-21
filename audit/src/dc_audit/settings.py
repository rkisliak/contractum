"""Audit service settings (``DC_`` environment variables plus Docker secret files)."""

from typing import Self

from dc_core.settings import CoreSettings
from pydantic import Field, model_validator


class AuditSettings(CoreSettings):
    """Adds the JetStream consumer settings to the shared core settings."""

    service: str = "audit"
    consumer_durable: str = "audit-writer"
    consumer_batch: int = 50
    max_deliver: int = 10
    # Second durable consumer on stream ACCESS (access.>, best-effort access events, D-171).
    access_consumer_durable: str = "audit-access-writer"
    access_consumer_batch: int = 200
    access_max_age_s: int = 604800  # 7 days; the stream discards the oldest messages
    # WR-03: and at most 1 GiB, so best-effort reads can never fill the JetStream store that the
    # guaranteed AUDIT stream shares (the oldest access events are discarded first).
    access_max_bytes: int = Field(default=1_073_741_824, gt=0)

    # Quick 261120-0ye (P2): retention. ``retention_days`` is the Helm/env default (unset: keep
    # every event); a stored platform setting overrides it. Whatever the source, the effective
    # value is never below ``retention_min_days`` (itself refused below 30 at start-up).
    retention_days: int | None = Field(default=None, ge=1)
    retention_min_days: int = 90
    retention_interval_s: int = Field(default=3600, ge=1)
    retention_batch: int = Field(default=5000, ge=1)
    retention_max_batches: int = Field(default=100, ge=1)

    @model_validator(mode="after")
    def _retention_floor(self) -> Self:
        if self.retention_min_days < 30:
            raise ValueError("retention_min_days must be at least 30")
        return self
