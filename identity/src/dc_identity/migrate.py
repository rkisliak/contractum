"""``python -m dc_identity.migrate upgrade|downgrade|check|current [revision]``."""

from dc_core.migrations import migration_cli

from dc_identity.settings import IdentitySettings

SCRIPT_LOCATION = "dc_identity:migrations"
VERSION_TABLE = "alembic_version_identity"


def main() -> None:
    migration_cli(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        settings_factory=IdentitySettings,
    )


if __name__ == "__main__":
    main()
