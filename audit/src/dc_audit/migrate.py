"""``python -m dc_audit.migrate upgrade|downgrade|check|current [revision]``."""

from dc_core.migrations import migration_cli

from dc_audit.settings import AuditSettings

SCRIPT_LOCATION = "dc_audit:migrations"
VERSION_TABLE = "alembic_version_audit"


def main() -> None:
    migration_cli(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        settings_factory=AuditSettings,
    )


if __name__ == "__main__":
    main()
