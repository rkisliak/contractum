"""Identity service settings (``DC_`` environment variables plus Docker secret files)."""

from pathlib import Path
from typing import Self

from dc_core.settings import CoreSettings
from pydantic import model_validator


class IdentitySettings(CoreSettings):
    """Adds the bootstrap, signing-key and token settings to the shared core settings.

    The bootstrap secret and the signing key are *paths* to Docker secret files, never values:
    the secret is never read from an environment variable and never has a default (D-01).
    """

    service: str = "identity"
    bootstrap_admin_username: str = "admin"
    bootstrap_secret_file: Path = Path("/run/secrets/bootstrap_admin_secret")
    jwt_signing_key_file: Path = Path("/run/secrets/jwt_signing_key.pem")
    jwt_previous_public_key_file: Path | None = None
    local_issuer: str = "http://identity:8000"
    token_audience: str = "contractum"  # noqa: S105 - an audience name, not a secret
    access_token_ttl_s: int = 900
    restricted_token_ttl_s: int = 600
    # Idle timeout of a refresh token (it slides on every rotation); the stored ``idle_timeout_s``
    # setting overrides it (quick 261120-0ye, S3).
    refresh_token_ttl_s: int = 43200
    # Absolute session length counted from the sign-in (D-39); the stored ``session_max_s`` setting
    # overrides it. Before 261120-0ye a session had no absolute cap, so this default is stricter.
    session_max_s: int = 604800
    # Break-glass recovery (D-37, DC_SIGNIN_LOCAL_FORCE_ENABLED): forces local sign-in on whatever
    # the stored setting says, so a mistaken change can always be undone. Helm: identity extraEnv.
    signin_local_force_enabled: bool = False
    # Login backoff (D-04): a key locks for min(2 ** (failures - threshold), max) seconds once its
    # failure count reaches its threshold. The IP threshold is higher because of shared NAT egress.
    throttle_account_threshold: int = 5
    throttle_ip_threshold: int = 20
    throttle_max_lock_s: int = 900
    # Sliding window for the failure count (CR-01): a failure more than this long after the key's
    # previous one restarts the count. Keep it above throttle_max_lock_s so escalation continues.
    throttle_window_s: int = 1800

    # Microsoft Graph (D-60 to D-62): the directory behind every Entra decision. The client secret
    # is a Docker secret file, never an environment value. One multi-tenant Graph application is
    # consented in each Entra tenant, so a further tenant needs configuration only (AUTH-03).
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    graph_token_url_template: str = (
        "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"  # noqa: S105
    )
    graph_client_id: str | None = None
    graph_client_secret_file: Path = Path("/run/secrets/graph_client_secret")
    graph_scope: str = "https://graph.microsoft.com/.default"
    graph_timeout_s: float = 5.0
    graph_max_pages: int = 20
    # Directory answers are cached this long (D-61); 0 disables the cache (tests do).
    directory_cache_ttl_s: float = 60.0
    directory_cache_size: int = 10000

    # Personal access tokens (D-67): an expiry is mandatory and may be at most this many days away.
    credential_max_ttl_days: int = 365
    # Active (unrevoked, unexpired) access tokens one owner may hold (quick 261120-lbz, D-03); the
    # stored `security.max_active_tokens` setting overrides it. Bounds 1 to 1000.
    max_active_tokens: int = 10
    # Concurrent sessions (refresh-token families) one local user may hold (quick 261120-s0p, D-09);
    # signing in beyond it ends the oldest. The stored `security.max_sessions_per_user` overrides
    # it. Bounds 1 to 100.
    max_sessions_per_user: int = 5
    # A service-account key listing flags an active key expiring within this many days (D-70).
    credential_expiry_warning_days: int = 14
    # last_used_at is flushed in one batched UPDATE this often (never written on the request path).
    credential_usage_flush_s: float = 60.0

    # D-105: the contracts delivery worker's platform identity. The credential file holds one line
    # "<16 hex public id>:<64 hex SHA-256 of the secret>"; identity never sees the plaintext key.
    notifier_credential_file: Path = Path("/run/secrets/notifier_credential")
    notifier_account_name: str = "contracts-notifier"
    # The provisioned key's lifetime; a start renews it in place when it has lapsed or is close.
    notifier_key_lifetime_days: int = 365
    # How often a running identity re-checks the notifier key and renews it inside the warning
    # window, so a process that never restarts cannot let the key lapse (WR-05).
    notifier_renewal_interval_s: float = 3600.0

    # D-153 / Pitfall 12: how many names one principal may submit to principals/resolve per rolling
    # minute (in process memory per identity replica).
    principal_resolve_names_per_minute: int = 1000

    # Quick 261119-0v1: deleting a folder asks contracts and validation (internal usage and purge
    # routes, the caller's own bearer) what the folder still holds. Request time only; identity
    # does not depend on either service to start.
    contracts_url: str = "http://contracts:8000"
    validation_url: str = "http://validation:8000"
    # Quick 261120-0yc: Admin > Settings > About asks audit and mail (internal /about) with the
    # caller's own bearer. These are the compose and Helm Service names; set env only to differ.
    audit_url: str = "http://audit:8000"
    mail_url: str = "http://mail:8000"
    folder_usage_timeout_s: float = 5.0

    @model_validator(mode="after")
    def _check_graph(self) -> Self:
        if "{tenant_id}" not in self.graph_token_url_template:
            raise ValueError("graph_token_url_template must contain {tenant_id}")
        if self.directory_cache_ttl_s < 0:
            raise ValueError("directory_cache_ttl_s must be >= 0")
        if self.graph_max_pages < 1 or self.directory_cache_size < 1:
            raise ValueError("graph_max_pages and directory_cache_size must be >= 1")
        if self.session_max_s < 3600:
            raise ValueError("session_max_s must be >= 3600")
        if self.credential_max_ttl_days < 1:
            raise ValueError("credential_max_ttl_days must be >= 1")
        if not 1 <= self.max_active_tokens <= 1000:
            raise ValueError("max_active_tokens must be between 1 and 1000")
        if not 1 <= self.max_sessions_per_user <= 100:
            raise ValueError("max_sessions_per_user must be between 1 and 100")
        if self.credential_expiry_warning_days < 0:
            raise ValueError("credential_expiry_warning_days must be >= 0")
        if self.credential_usage_flush_s <= 0:
            raise ValueError("credential_usage_flush_s must be > 0")
        if self.notifier_key_lifetime_days < 1:
            raise ValueError("notifier_key_lifetime_days must be >= 1")
        if self.notifier_renewal_interval_s <= 0:
            raise ValueError("notifier_renewal_interval_s must be > 0")
        if not self.notifier_account_name.strip() or len(self.notifier_account_name) > 100:
            raise ValueError("notifier_account_name must be 1 to 100 characters")
        if self.principal_resolve_names_per_minute < 1:
            raise ValueError("principal_resolve_names_per_minute must be >= 1")
        return self
