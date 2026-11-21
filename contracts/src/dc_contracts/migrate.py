"""``python -m dc_contracts.migrate upgrade|downgrade|check|current [revision]``."""

from dc_core.migrations import migration_cli

from dc_contracts.settings import ContractsSettings

SCRIPT_LOCATION = "dc_contracts:migrations"
VERSION_TABLE = "alembic_version_contracts"


def main() -> None:
    migration_cli(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        settings_factory=ContractsSettings,
    )


if __name__ == "__main__":
    main()
