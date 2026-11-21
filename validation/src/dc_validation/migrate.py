"""``python -m dc_validation.migrate upgrade|downgrade|check|current [revision]``."""

from dc_core.migrations import migration_cli

from dc_validation.settings import ValidationSettings

SCRIPT_LOCATION = "dc_validation:migrations"
VERSION_TABLE = "alembic_version_validation"


def main() -> None:
    migration_cli(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        settings_factory=ValidationSettings,
    )


if __name__ == "__main__":
    main()
