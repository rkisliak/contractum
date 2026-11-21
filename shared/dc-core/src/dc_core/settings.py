"""Per-service settings: environment (DC_ prefix) plus Docker secret files."""

from pathlib import Path
from typing import ClassVar

from pydantic import SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
)
from sqlalchemy.engine import URL


class CoreSettings(BaseSettings):
    """Env vars use the ``DC_`` prefix. Secrets are read from files named after the field
    (``db_password``, ``nats_password``) under ``/run/secrets`` when that directory exists.
    """

    model_config = SettingsConfigDict(env_prefix="DC_", extra="ignore")

    secrets_dir: ClassVar[str] = "/run/secrets"

    service: str
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str | None = None
    db_user: str | None = None
    db_password: SecretStr | None = None
    database_url: SecretStr | None = None
    nats_url: str | None = None
    nats_user: str | None = None
    nats_password: SecretStr | None = None
    log_level: str = "INFO"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if Path(cls.secrets_dir).is_dir():
            # No env prefix for file names: the secret file is `db_password`, not `dc_db_password`.
            sources.append(
                SecretsSettingsSource(settings_cls, secrets_dir=cls.secrets_dir, env_prefix="")
            )
        return tuple(sources)

    def sqlalchemy_url(self) -> str:
        """``postgresql+psycopg://...`` for SQLAlchemy and Alembic (same driver, D-stack)."""
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        url = URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value() if self.db_password else None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
        return url.render_as_string(hide_password=False)
