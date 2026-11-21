"""Mail service settings (``DC_`` environment variables, ``smtp_password`` from a secret file)."""

import ipaddress
import re
from typing import Annotated, Any, Literal, Self

from dc_core.settings import CoreSettings
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
)


class _EnvWithoutSmtpPassword(EnvSettingsSource):
    """The environment source, minus the SMTP password: that one comes from a secret file only."""

    def __call__(self) -> dict[str, Any]:
        data = super().__call__()
        data.pop("smtp_password", None)
        return data


_HOST_CHARS = re.compile(r"[a-z0-9.-]+")


def is_internal_smtp_host(host: str) -> bool:
    """May plaintext SMTP (``DC_SMTP_TLS=none``) be used towards ``host``?

    True for ``localhost``, a loopback IP literal, a single-label name (a compose or Kubernetes
    service name such as ``mailpit``) and a cluster name ending in ``.svc`` or
    ``.svc.cluster.local``. Everything else is False: any other IP literal (private ones too),
    any other dotted name, a trailing-dot name. The check is on the name syntax, not on what the
    name resolves to: a single-label name relies on the resolver search list.
    """
    name = host.strip().lower()
    try:
        address = ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        pass
    else:
        return address.is_loopback
    if not name or _HOST_CHARS.fullmatch(name) is None:
        return False
    if name.startswith((".", "-")) or name.endswith((".", "-")) or ".." in name:
        return False
    if name == "localhost" or "." not in name:
        return True
    return name.endswith((".svc", ".svc.cluster.local"))


class MailSettings(CoreSettings):
    """SMTP connection settings. The mail service has no database and no queue."""

    service: str = "mail"
    smtp_host: str | None = None
    smtp_port: Annotated[int, Field(ge=1, le=65535)] = 25
    smtp_tls: Literal["none", "starttls", "implicit"] = "starttls"
    smtp_username: str | None = None
    # Read only from /run/secrets/smtp_password: never from the environment, never a default.
    smtp_password: SecretStr | None = None
    smtp_from: str = "contractum@localhost"
    smtp_timeout_s: Annotated[float, Field(gt=0)] = 10.0
    # How long the stored sender (platform settings, section ``notifications``) is cached before
    # identity is asked again (quick 261120-0yd, D-26). DC_SETTINGS_TTL_S.
    settings_ttl_s: Annotated[float, Field(gt=0)] = 30.0

    @model_validator(mode="after")
    def _login_is_all_or_nothing(self) -> Self:
        """A half-configured login would silently send unauthenticated: refuse it at startup."""
        has_user = bool(self.smtp_username)
        has_password = self.smtp_password is not None and bool(
            self.smtp_password.get_secret_value()
        )
        if has_user != has_password:
            raise ValueError(
                "smtp_username and smtp_password must be set together "
                "(the password is read from /run/secrets/smtp_password)"
            )
        return self

    @model_validator(mode="after")
    def _plaintext_only_for_internal_hosts(self) -> Self:
        """Plaintext SMTP is a dev mode (Mailpit): not towards a named relay, not with a login."""
        if self.smtp_tls != "none":
            return self
        if self.smtp_username:
            raise ValueError("SMTP credentials are never sent without TLS")
        if self.smtp_host and not is_internal_smtp_host(self.smtp_host):
            raise ValueError(
                "DC_SMTP_TLS=none is allowed only for a loopback, single-label or cluster .svc "
                "SMTP host; use starttls or implicit"
            )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources = super().settings_customise_sources(
            settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
        )
        return tuple(
            _EnvWithoutSmtpPassword(settings_cls) if source is env_settings else source
            for source in sources
        )
