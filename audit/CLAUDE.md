# audit (package `dc_audit`, workspace member `dc-audit`)

Consumes the platform's audit events from NATS JetStream and stores them, idempotently.

## What this module owns

- Two durable pull consumers: `audit-writer` on stream `AUDIT` (subjects `audit.>`, change events
  from the outbox relays) and `audit-access-writer` on stream `ACCESS` (subjects `access.>`,
  best-effort access events, 7-day max age, at most `DC_ACCESS_MAX_BYTES` (default 1 GiB),
  discard old, batch 200). The size cap keeps a wedged access consumer from filling the JetStream
  store that the guaranteed AUDIT stream shares (WR-03). The audit service creates both streams
  when missing; on an existing stream it updates only the limits it asks for (ACCESS's age, size
  and discard policy), never subjects, storage or the duplicate window, and it never edits AUDIT.
- The public audit query (AUD-05, Phase 7): `GET /api/v1/audit/events` (filters, keyset cursor,
  newest first) and `GET /api/v1/audit/events/{event_id}` (uniform 404), gated by `audit:list`
  and trimmed in one SQL predicate to identity's `accessible_folders(caller, "audit:query")`
  (Global admin, Global reader, Org admin, Project admin, Team admin). Events with an empty or
  `[root]` scope path are visible only at ROOT; block flags cut like any permission; an empty
  scope is an empty page and an identity outage is 503, never an unfiltered page.
- The idempotent ingest (`event_id` is unique), the dead-letter table for poison messages, and the
  internal read `GET /internal/v1/events` (global admins only, never routed by the gateway, not in
  OpenAPI; change rows only unless `category` asks for another).
- Three event categories in `audit_event.category`, derived at ingest and never trusted from the
  producer: `access` (subject prefix `access.`), `denial` (only identity's
  `audit.identity.authz.check.denied`) and `change` (everything else, and every row written before
  migration `0002_audit_query`). Access and denial rows carry a flat `detail` and no snapshots.
- Database `audit` (role `audit`), version table `alembic_version_audit`. The audit service emits
  no events itself, so it has no outbox table; its rows are written by the system actor
  `system:audit-ingest`.

## What it must never import

Services import only the `dc_*` libraries (`dc_core`, `dc_auth`, `dc_odcs`). This module must not
import `dc_identity`, `dc_contracts` or `dc_validation`. The event envelope and subject scheme live
in `dc_core.events`, shared with every producer. The `dc_*` libraries never import services.
`uv run lint-imports` enforces it.

## Key files and entry points

- `src/dc_audit/main.py`: `create_audit_app` (`uvicorn --factory dc_audit.main:create_audit_app`).
- `src/dc_audit/consumer.py`: the only NATS-aware audit module (stream creation, pull loops for
  `AUDIT` and `ACCESS`, dead letter after `max_deliver`).
- `src/dc_audit/ingest.py`: `handle_payload`, the transport-agnostic entry (D-21), and
  `category_for`, the category derivation and spoof refusals.
- `src/dc_audit/api.py`: the public router (`/api/v1/audit`); `query.py`: `make_filters`,
  `encode_cursor`/`decode_cursor` (canonical or 422), `visible(scope)` (the scope predicate, a
  local copy of contracts' `scope_clause`) and `build_statement`; `dto.py`: `AuditEventOut` and
  `AuditEventPageOut`, shared with the internal read.
- `src/dc_audit/internal_api.py`, `models.py` (`AuditEvent`, `AuditDeadLetter`), `settings.py`.
- `src/dc_audit/migrate.py` and `migrations/`.
- Audit retention (quick 261120-0ye, D-33, D-42): `retention.py` (`RetentionJob`, `retention_hook`, `effective_days`) deletes events
  older than the effective retention in batches (`DC_RETENTION_BATCH` 5000, `DC_RETENTION_MAX_BATCHES` 100 per run,
  every `DC_RETENTION_INTERVAL_S` 3600 s) under the session advisory lock `dc-audit-retention` (a second run skips). The value
  is event-carried: the newest identity `settings.updated` or `settings.imported` event whose `after` has `platform`
  (`audit_retention_days`: absent means `DC_RETENTION_DAYS`, unset keeps everything; `0` keeps for ever), never below
  `DC_RETENTION_MIN_DAYS` (90; start-up refuses below 30). `aggregate_type` `platform_settings` and `audit_retention` rows are
  never deleted; a run that deleted rows inserts one `audit.retention_applied` row (service `audit`, actor `system:audit-retention`,
  empty scope, `after` `{retention_days, cutoff, deleted}`). The delete rides `ix_audit_event_time` (occurred_at leading), so no
  migration was needed. About reports `platform.audit_retention_days`.

## Tests

- Unit (no Docker): `uv run pytest -m unit audit`
- Integration (needs Docker; real Postgres and NATS): `uv run pytest -m integration audit`
- Route policy walker: `audit/tests/unit/test_route_policies.py`.
- Public query: `audit/tests/unit/test_query.py`, `audit/tests/integration/test_public_query.py`
  and the D-186 visibility property test `audit/tests/integration/test_visibility_property.py`
  (identity's real scope SQL against `dc_auth.oracle`).
- Categories, spoof refusals, detail caps and the ACCESS stream:
  `audit/tests/integration/test_categories.py`.
- Outage behaviour: `bash scripts/e2e.sh tests/e2e/test_audit_resilience.py`.
- Phase 7 on the running stack: `bash scripts/e2e.sh tests/e2e/test_phase7_engineer_journey.py`
  (E2E-01: an Org admin reads a contract's full trail, access events and the refused write through
  the public query; a Team B admin gets empty pages), `tests/e2e/test_phase7_denial_guarantee.py`
  (every denial recorded exactly once, matched by `X-Request-ID`, across a NATS outage and an
  identity restart) and `tests/e2e/test_phase7_authz_load.py` (the D-187 load profile).
- No secrets in snapshots or details: `tests/repo/test_audit_no_secrets.py` pins every service's
  `__audit_fields__` and the denial and access `DETAIL_KEYS`.
- Retention: `audit/tests/integration/test_retention.py`, `audit/tests/unit/test_retention_settings.py`.
- Whole repo gate: `bash scripts/ci.sh`.

## Platform rules that apply here

- Every route is declared with `require(...)` or `public()` on a `PolicyRouter` and listed in the
  walker test; the public query is `require("audit:list")`, the internal read
  `require("audit:read")`.
- Events never carry secrets: producers snapshot only `__audit_fields__` and emit only their
  allow-listed `detail` keys; the audit store keeps what it receives and adds nothing.
- Errors are RFC 9457 problems with `urn:dc:problem:<slug>` types listed in
  `docs/user/api-guide.md` ("Errors: RFC 9457 problem details").
- Own migrations and version table; never touch another service's database.

## Skills

- `.claude/skills/audit-events/SKILL.md`: envelope, adding an action, transport seam, ingest.
- Repo-level skills (run-stack, add-route, add-migration) live in the root `.claude/skills/`.
