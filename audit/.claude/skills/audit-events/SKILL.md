---
name: audit-events
description: Emit, add or change audit events (envelope fields, new actions, NATS subjects and permissions, the transport seam, idempotent ingest, dead letter, the public audit query and the internal read).
---

# audit-events

Use when adding a new audited write in any service, or changing `dc_core.events`, the relay or
`audit/src/dc_audit/`.

1. **One event per business operation.** In a handler, call `uow.audit(action, aggregate_type,
   aggregate_id, before=..., after=..., scope_path=..., aggregate_version=...)` on the unit of
   work from `Depends(unit_of_work)`, then `await uow.commit()`. The domain rows and the outbox
   row commit in one transaction; a `before_commit` tripwire raises when audited rows are written
   without an event. The audit columns (`created_by/at`, `updated_by/at`) are stamped from the
   actor, never from the client.
2. **Snapshots are allow-listed.** `before`/`after` come from `UnitOfWork.snapshot(obj)`, which
   reads only the model's `__audit_fields__`. Add a field there deliberately; never add secret
   columns, document content or token material (AUD-03).
3. **Envelope fields** (`dc_core.events.build_envelope`): `event_id`, `occurred_at`, `service`,
   `aggregate_type`, `aggregate_id`, `aggregate_version`, `scope_path` (root-first folder ids),
   `action`, `actor` (typed: `user`, `service_principal`, `pat`, `service_account` or `system`, with `id`, `display`, `via`), `request_id`, `before`,
   `after`, `schema_version`. The audit store adds `received_at` and a computed RFC 6902 `changes`
   patch. Two optional fields were added in Phase 7 (`schema_version` stays 1): `category`
   (`change`, `access` or `denial`; ingest derives it and refuses a claim that disagrees) and
   `detail`, a flat object of at most 16 keys matching `^[a-z_]{1,32}$` whose values are strings
   (at most 512 characters), integers, booleans or null. Nested objects, lists and floats are
   refused. `build_envelope` adds `detail` only when the outbox row has a non-null `detail`
   attribute (identity's denial rows), so other producers' envelopes are unchanged. Producers
   allow-list their detail keys; ingest only caps the shape.
4. **Adding an action.** Actions are dotted (`widget.created`) and every dot-separated segment
   becomes its own NATS subject token: `audit.<service>.<aggregate_type>.<action tokens>`. So
   stream subjects (`audit.>`) and each producer's publish permission use the multi-token wildcard
   `>`, never `*`. Each token must match `SUBJECT_TOKEN`; an invalid action raises at publish time
   and stalls that service's relay (known limitation in `deferred-items.md`), so test the name.
5. **Transport seam (D-21).** Producers depend only on `EventPublisher` (`dc_core.events`);
   `JetStreamPublisher` is the NATS adapter and `OutboxRelay` drains the outbox with
   `FOR UPDATE SKIP LOCKED`. On the audit side only `dc_audit.consumer` knows NATS and hands each
   message to `dc_audit.ingest.handle_payload`. A replacement transport (HTTP push, Postgres
   queue) adds adapter classes only.
6. **Idempotent ingest.** `event_id` is unique in `audit_event`; redelivery is a no-op. The
   envelope must match its subject exactly (`subject_for(service, aggregate_type, action,
   prefix=<first subject token>)`), otherwise it is rejected as spoofed. `category_for` then
   derives the stored category: the `access.` prefix means `access`, the triple
   (`identity`, `authz`, `check.denied`) on `audit.` means `denial`, everything else is `change`.
   Three refusals raise `SubjectMismatchError` (dead letter after `max_deliver`, never the store):
   a claimed `category` that disagrees with the subject (for example `denial` from contracts, or
   `access` on an `audit.` subject), a `denial` claim from any other triple, and an access or
   denial envelope whose `before` or `after` is not null.
7. **Dead letter.** A message that fails `max_deliver` times is written to `audit_dead_letter` and
   acknowledged so it never blocks the stream.
8. **Reading events.** The supported way is the public query `GET /api/v1/audit/events`
   (`dc_audit.api`, through the gateway), gated by the principal-level `audit:list` and trimmed
   in SQL to `accessible_folders(caller, "audit:query")` (`dc_audit.query.visible`, a local copy
   of contracts' `scope_clause`, so `excluded_subtrees` cut blocked subtrees). Filters
   `aggregate_type`, `aggregate_id`, `actor_id`, `service`, `action` (a trailing dot is an escaped
   LIKE prefix), `category`, `from`/`to` (half-open, offset required); `limit` 1..200 and an
   opaque canonical keyset `cursor` over `(occurred_at, id)`; every category unless filtered.
   `GET /api/v1/audit/events/{event_id}` answers one event or the uniform 404. Events with an
   empty or `[root]` `scope_path` are visible only to `audit:query` holders at ROOT. Never
   post-filter a page in Python and never fall back to an unfiltered query: an identity outage is
   503. A change to `visible()` must keep `audit/tests/integration/test_visibility_property.py`
   (D-186) green. Both reads return `dc_audit.dto.AuditEventOut`.
   The internal read `GET /internal/v1/events?aggregate_id=&service=&action=&category=&limit=`
   stays for compose-network polling (`http://audit:8000`, a global admin's bearer, `audit:read`).
   E2E tests poll it with `wait_for`. It returns only `change` rows by default, so journeys that
   pin an exact list of change events per aggregate are not disturbed by reads or denials; pass
   `category=access` or `category=denial` to read those. Rows include `category` and `detail`.
9. **Contracts consumes its own events (card notices, D-99).** The contracts service reads
   `audit.contracts.>` from stream `AUDIT` through its own durable pull consumer `contracts-notices`
   (`dc_contracts.notice_consumer`) and hands each message to `notices.handle_payload`. The AUDIT
   stream stays owned by the audit service: contracts never creates, edits, purges or deletes it.
   The filter is enforced by the NATS server, not by the client: the contracts NATS user may only
   create the consumer through the filtered create subject
   `$JS.API.CONSUMER.CREATE.AUDIT.contracts-notices.audit.contracts.>`, and the server refuses a
   body filter that differs from the subject's filter token. Never give that user a create subject
   without the filter token or `$JS.API.>`. The exact permission list is pinned by
   `tests/repo/test_nats_permissions.py` and proven refusal by refusal against a real server in
   `contracts/tests/integration/test_notice_consumer_nats.py`; a new consumer or subject for any
   service starts with a failing case there.
10. **Access events and denials (Phase 7).** Access events use the subject family
   `access.<service>.<aggregate_type>.<action>` (`subject_for(..., prefix="access")`) on stream
   `ACCESS` (`dc_core.events.ACCESS_STREAM`; subjects `access.>`, file storage, 120 s duplicate
   window, 7-day max age, at most `DC_ACCESS_MAX_BYTES` = 1 GiB, discard old; WR-03: best-effort
   reads must never fill the store AUDIT shares). Publish with
   `JetStreamPublisher(..., stream=ACCESS_STREAM)`.
   The audit consumer `audit-access-writer` (batch 200) stores them through the same
   `handle_payload`. Denials stay on `audit.identity.authz.check.denied` in stream `AUDIT`.
   **Ordering rule:** an ingest change that accepts a new envelope field or subject family must
   deploy before any producer sends it. `AuditEnvelope` is `extra="forbid"`, so an early producer's
   events are dead-lettered after `max_deliver` and a "guaranteed" denial is lost. `ensure_stream`
   updates only the limits it is asked for on an existing stream (`max_age`, `max_bytes`,
   `discard` for ACCESS) with `update_stream`; it never edits subjects, storage or the duplicate
   window, and never touches AUDIT (no limits requested). Any other stream change on a warm stack
   needs `docker compose down -v` (or a manual stream edit).
11. **Producers of access and denial events.** Contracts records content reads through
   `dc_contracts.access_events.record_content_read` on a `dc_core.access.AccessLog` (bounded,
   drop-oldest, batched, never raises; a 304 or an error records nothing); identity records
   denials through `dc_identity.denials.DenialRecorder` in its outbox (fail closed: 503 when the
   row cannot be written). Each producer's `DETAIL_KEYS` is pinned, together with every
   `__audit_fields__`, by `tests/repo/test_audit_no_secrets.py`; a new detail key or snapshot
   field changes that test and `docs/dev/api-design-decisions.md` ("Audit trail and the API
   surface") in the same change.
12. Verify: `uv run pytest -m unit audit shared/dc-core tests/repo/test_audit_no_secrets.py`,
   `uv run pytest -m integration audit shared/dc-core`, then `bash scripts/e2e.sh
   tests/e2e/test_audit_resilience.py tests/e2e/test_phase7_engineer_journey.py`.
