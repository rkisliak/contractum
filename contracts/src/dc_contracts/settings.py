"""Contracts service settings (``DC_`` environment variables plus Docker secret files)."""

from __future__ import annotations

from cryptography.fernet import Fernet
from dc_core.settings import CoreSettings
from dc_odcs import SUPPORTED_API_VERSIONS
from pydantic import SecretStr, ValidationInfo, field_validator
from pydantic_settings import SettingsConfigDict

# Validation's batch caps as deployed (IN-06): DC_MAX_BATCH_ITEMS, DC_MAX_BATCH_FOLDERS and
# DC_MAX_BATCH_BYTES on the validation service, at their defaults. Contracts cannot read
# validation's settings, so the coupling is explicit: the DC_IMPACT_PAGE_* settings are refused at
# start-up above these values, and an operator who lowers a DC_MAX_BATCH_* cap on validation must
# lower the matching DC_IMPACT_PAGE_* setting on contracts too (otherwise every impact page is a 422
# there and a 503 here). Raising a cap needs a change here. `tests/repo/test_batch_caps_coupling.py`
# pins these values to validation's defaults.
VALIDATION_BATCH_CAPS: dict[str, int] = {
    "impact_page_size": 25,  # DC_MAX_BATCH_ITEMS
    "impact_page_max_folders": 20,  # DC_MAX_BATCH_FOLDERS
    "impact_page_max_bytes": 4 * 1024 * 1024,  # DC_MAX_BATCH_BYTES
}


class ContractsSettings(CoreSettings):
    """Adds the document size cap and the supported-ODCS-version allow-list (D-21)."""

    # A malformed webhook key must never be echoed back in a validation error.
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    service: str = "contracts"
    # 1 MiB in the app; the gateway buffers at most 2 MiB (research: CONTEXT discretion).
    max_document_bytes: int = 1048576
    # DC_SUPPORTED_ODCS_VERSIONS as a JSON list, e.g. '["v3.2.0"]'. A subset of what dc_odcs can
    # validate: a value outside it has no vendored schema, so it fails at startup instead.
    supported_odcs_versions: frozenset[str] = SUPPORTED_API_VERSIONS
    # Quick 261120-0ye (G2, G3): the ODCS apiVersion a new contract starts with and whether an
    # unpublish or deprecate needs a reason. Identity's stored governance settings override both
    # at runtime; these are the Helm/env defaults (DC_DEFAULT_ODCS_API_VERSION,
    # DC_REQUIRE_LIFECYCLE_REASON).
    default_odcs_api_version: str = "v3.2.0"
    require_lifecycle_reason: bool = False

    # DC_MAX_DRAFTS_PER_CONTRACT: how many drafts one contract may hold at once (T-03-12).
    max_drafts_per_contract: int = 25

    # DC_MAX_ATTACHMENTS: rule sets one contract may attach explicitly (WR-06). Keep it at or below
    # validation's DC_MAX_ATTACHMENTS (default 50): validation refuses a run with more.
    max_attachments: int = 50

    # DC_NOTICE_CONSUMER_*: the in-service JetStream consumer that turns lifecycle events into card
    # notices (D-99). A message is dropped (logged, acknowledged) after ``max_deliver`` deliveries;
    # a failing handler is retried with a delay of min(2 ** deliveries, max_backoff_s) seconds.
    notice_consumer_batch: int = 50
    notice_consumer_max_deliver: int = 50
    notice_consumer_max_backoff_s: float = 300.0
    notice_consumer_ack_wait_s: int = 30

    # DC_BLOCKED_NOTICE_COOLDOWN_S: a card hears at most once per this many seconds that the owner
    # tried to unpublish a version it pins (default one day, A12); the attempts stay audited.
    blocked_notice_cooldown_s: int = 86400

    # The webhook channel (SUB-07, D-98, D-107). ``webhook_secret_key`` is a Fernet key read from
    # the ``webhook_secret_key`` Docker secret; without it a webhook cannot be enabled (503).
    # DC_WEBHOOK_SECRET_OVERLAP_S: how long the previous signing secret stays valid after a
    # rotation. DC_WEBHOOK_REQUIRE_HTTPS: refuse http URLs (compose dev sets false).
    # DC_WEBHOOK_ALLOWED_PRIVATE_HOSTS: JSON list of hostnames that may resolve to private
    # addresses (empty by default; compose dev allows its sink).
    webhook_secret_key: SecretStr | None = None
    webhook_secret_overlap_s: int = 86400
    webhook_require_https: bool = True
    webhook_allowed_private_hosts: frozenset[str] = frozenset()
    # Quick 261120-s0p (item 6): the webhook allow-list is mandatory (an empty list allows no
    # host). At start-up contracts posts the hosts of the existing webhook channels to identity,
    # which seeds the list once. DC_WEBHOOK_HOST_SEED_ENABLED false skips that (a deployment with
    # no webhook channels, or one that manages the list by hand).
    webhook_host_seed_enabled: bool = True

    # Delivery of card notices (SUB-07, D-98, D-99). DC_WEBHOOK_TIMEOUT_S bounds one webhook POST.
    # DC_NOTIFY_RETRY_SCHEDULE_S is a JSON list of seconds: the wait after the first failed
    # attempt, the second and so on (the last entry repeats), each with +-10% jitter, until
    # DC_NOTIFY_MAX_ATTEMPTS attempts were made and the delivery is marked failed. The worker polls
    # every DC_NOTIFY_POLL_INTERVAL_S, claims DC_NOTIFY_BATCH_SIZE deliveries per pass and leases a
    # claimed row for DC_NOTIFY_LEASE_S so a crashed worker's rows come back.
    # DC_NOTIFY_WORKER_ENABLED false keeps the loop off (tests drive it by hand).
    webhook_timeout_s: float = 10.0
    notify_retry_schedule_s: tuple[int, ...] = (60, 300, 1800, 7200, 21600, 43200)
    notify_max_attempts: int = 7
    notify_poll_interval_s: float = 5.0
    notify_batch_size: int = 20
    notify_lease_s: int = 120
    notify_worker_enabled: bool = True

    # The email channel (SUB-07, D-105): contracts never speaks SMTP, it hands each message to the
    # mail service. DC_MAIL_URL is that service's base URL (backend network, never the gateway).
    # ``mail_api_key`` is the notifier service account's API key, read from the Docker secret
    # of the same name; there is no default. Without either, email deliveries fail with
    # ``mail-not-configured`` and are retried. DC_MAIL_TIMEOUT_S bounds one call.
    mail_url: str | None = None
    mail_api_key: SecretStr | None = None
    mail_timeout_s: float = 10.0

    # Sunset-approaching notices (SUB-07, D-96): the scheduler warns each active pin on a deprecated
    # version once per lead time. DC_SUNSET_LEAD_DAYS is a JSON list of days before the (UTC)
    # sunset date, e.g. '[30, 7]'; DC_SUNSET_SCAN_INTERVAL_S is the wait between scans;
    # DC_SUNSET_SCHEDULER_ENABLED false keeps the loop off (tests drive ``scan_once`` by hand).
    sunset_lead_days: tuple[int, ...] = (30, 7)
    sunset_scan_interval_s: int = 3600
    sunset_scheduler_enabled: bool = True

    # The publish gate (VAL-07, D-126, D-131). contracts calls validation's internal run route over
    # the compose network. DC_VALIDATION_URL is its base URL (never the gateway); a publish waits at
    # most DC_VALIDATION_PUBLISH_TIMEOUT_S for the answer and is refused with 503 when it is late.
    # DC_VALIDATION_SAVE_TIMEOUT_S bounds a save-mode check (validate on save).
    validation_url: str = "http://validation:8000"
    validation_publish_timeout_s: float = 12.0
    validation_save_timeout_s: float = 3.0

    # Rule impact (RULE-10, RULE-11, D-158 .. D-161). DC_IMPACT_REQUEST_MAX_BYTES caps the small
    # JSON body of a dry-run request, read only after authentication (413 above it).
    # DC_VALIDATION_IMPACT_TIMEOUT_S bounds one batch page call to validation (a page evaluates up
    # to 25 documents, so it gets longer than a single publish run).
    impact_request_max_bytes: int = 4096
    validation_impact_timeout_s: float = 30.0
    # One page sent to validation's batch routes (D-161) must stay inside validation's caps:
    # DC_IMPACT_PAGE_SIZE contracts (at most DC_MAX_BATCH_ITEMS, 25), DC_IMPACT_PAGE_MAX_FOLDERS
    # distinct folders (at most DC_MAX_BATCH_FOLDERS, 20) and DC_IMPACT_PAGE_MAX_BYTES of stored
    # document bytes (at most DC_MAX_BATCH_BYTES, 4 MiB). A page always holds at least one contract.
    impact_page_size: int = 25
    impact_page_max_folders: int = 20
    impact_page_max_bytes: int = 4 * 1024 * 1024

    @field_validator(
        "max_document_bytes",
        "max_drafts_per_contract",
        "max_attachments",
        "blocked_notice_cooldown_s",
        "webhook_secret_overlap_s",
        "notify_max_attempts",
        "notify_batch_size",
        "notify_lease_s",
        "notice_consumer_batch",
        "notice_consumer_max_deliver",
        "notice_consumer_ack_wait_s",
        "sunset_scan_interval_s",
        "impact_request_max_bytes",
    )
    @classmethod
    def _positive(cls, value: int, info: ValidationInfo) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be positive")
        return value

    @field_validator("impact_page_size", "impact_page_max_folders", "impact_page_max_bytes")
    @classmethod
    def _within_batch_caps(cls, value: int, info: ValidationInfo) -> int:
        # A larger page would be refused by validation with a 422, which contracts reports as 503.
        caps = VALIDATION_BATCH_CAPS
        if not 1 <= value <= caps[str(info.field_name)]:
            raise ValueError(
                f"{info.field_name} must be between 1 and {caps[str(info.field_name)]}"
            )
        return value

    @field_validator("notify_retry_schedule_s")
    @classmethod
    def _schedule(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(entry < 1 for entry in value):
            raise ValueError("notify_retry_schedule_s must be non-empty with positive entries")
        return value

    @field_validator("sunset_lead_days")
    @classmethod
    def _leads(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(entry < 1 for entry in value):
            raise ValueError("sunset_lead_days must be non-empty with positive entries")
        return tuple(sorted(set(value)))

    @field_validator(
        "notice_consumer_max_backoff_s",
        "webhook_timeout_s",
        "notify_poll_interval_s",
        "mail_timeout_s",
        "validation_publish_timeout_s",
        "validation_save_timeout_s",
        "validation_impact_timeout_s",
    )
    @classmethod
    def _positive_seconds(cls, value: float, info: ValidationInfo) -> float:
        if value <= 0:
            raise ValueError(f"{info.field_name} must be positive")
        return value

    @field_validator("webhook_secret_key")
    @classmethod
    def _fernet_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            try:
                Fernet(value.get_secret_value())
            except ValueError:
                # Never echo the key: it is a secret even when malformed.
                raise ValueError("webhook_secret_key is not a valid Fernet key") from None
        return value

    @field_validator("mail_api_key")
    @classmethod
    def _mail_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            key = value.get_secret_value()
            # Never echo the key: it goes into a header, so it must be one clean token.
            if not key or any(ch.isspace() or ord(ch) < 0x21 or ord(ch) > 0x7E for ch in key):
                raise ValueError("mail_api_key is not a valid key")
        return value

    @field_validator("webhook_allowed_private_hosts")
    @classmethod
    def _lower_hosts(cls, value: frozenset[str]) -> frozenset[str]:
        return frozenset(host.strip().lower() for host in value if host.strip())

    @field_validator("default_odcs_api_version")
    @classmethod
    def _default_is_supported(cls, value: str, info: ValidationInfo) -> str:
        supported = info.data.get("supported_odcs_versions")
        if supported is not None and value not in supported:
            raise ValueError(f"default_odcs_api_version must be one of {sorted(supported)}")
        return value

    @field_validator("supported_odcs_versions")
    @classmethod
    def _subset_of_vendored(cls, value: frozenset[str]) -> frozenset[str]:
        unknown = value - SUPPORTED_API_VERSIONS
        if unknown or not value:
            raise ValueError(
                "supported_odcs_versions must be a non-empty subset of "
                f"{sorted(SUPPORTED_API_VERSIONS)}"
            )
        return value
