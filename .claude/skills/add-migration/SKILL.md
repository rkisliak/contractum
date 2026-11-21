---
name: add-migration
description: Add a database migration to one service with Alembic by autogenerating against that service's alembic.ini, keeping its own version table, and verifying with the service's migrate check command.
---

# add-migration

Use when a service's models change. Every service owns its own database, `alembic.ini`,
`migrations/` directory and version table; never edit another service's database.

1. **Change the model** in `<service>/src/dc_<service>/models.py`. Use the service's declarative
   base (its metadata comes from `dc_core.db.new_metadata()`, which sets the constraint naming
   convention, so keep it). Audited tables use `AuditColumnsMixin`; add `__audit_fields__` for
   every audited model.
2. **Start a scratch database** the service can reach, for example
   `docker compose up -d postgres`, and export its URL (the services' own roles are created by
   `infra/postgres/init`; any reachable empty database works for autogenerate).
3. **Autogenerate** with the module's `alembic.ini`:
   `DC_DATABASE_URL=postgresql+psycopg://<user>@<host>/<db> uv run alembic -c
   <service>/alembic.ini revision --autogenerate -m "<what changed>"`. Put the secret in the
   environment or a file, never on a command line you commit. The new file lands in
   `<service>/src/dc_<service>/migrations/versions/` with the next sequential prefix
   (`0003_...`).
4. **Review the generated file by hand.** Autogenerate misses data migrations, server defaults
   and Postgres-specific types (`ltree`, partial indexes). For the audit and outbox column shapes
   use the helpers in `dc_core.migrations` (`audit_columns()`, `outbox_table(op)`) so the
   migration matches the mixins exactly.
5. **Version table naming.** Each service records its revision in its own table:
   `alembic_version_identity`, `alembic_version_contracts`, `alembic_version_validation`,
   `alembic_version_audit` (set in `dc_<service>/migrate.py` and read by `dc_core.migrations.run_env`, the one body each
   service's `migrations/env.py` calls with its metadata, default version table and settings URL).
   A new service needs both the module and the table name.
6. **Verify the migration**: `python -m dc_<service>.migrate upgrade head` and then
   `python -m dc_<service>.migrate check` (Alembic `check` fails when the models and the
   migrations disagree). Use the service's settings (`DC_DB_*` variables or `DC_DATABASE_URL`).
   The integration tests build a fresh Postgres 18 database from these migrations, so
   `uv run pytest -m integration <service>` is the proof.
7. **Deployment path.** In compose, `<service>-migrate` runs `python -m dc_<service>.migrate
   upgrade head` as a one-shot job and the service `depends_on` it with
   `service_completed_successfully`; do not run migrations from the app process. Check it with
   `docker compose up -d --build --wait` and `bash scripts/e2e.sh tests/e2e/test_stack_up.py`.
8. **Never rewrite a released migration.** Add a new revision. Downgrades are for development
   (`python -m dc_<service>.migrate downgrade <revision>`).
