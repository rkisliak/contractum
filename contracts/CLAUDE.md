# contracts (package `dc_contracts`, workspace member `dc-contracts`)

Stores ODCS data contracts inside folders of any kind (ROOT, Organization, Project, Team) and serves them back, byte for byte.

## What this module owns

- Contract creation (`POST /api/v1/contracts/folders/{folder_id}/contracts`), metadata, the
  document endpoint, list and `q` search (keyset pagination, no totals).
- Any-level placement (quick 261119-0v3): create routes check only `require_writable_folder` (archived subtree 409), no folder-kind gate. The ODCS id scope is `repo.odcs_scope_id`: the Organization id for a contract at or below an Organization, the ROOT folder id for a contract directly at ROOT, stored in `contract.organization_id` (the existing unique constraint is the rule, no migration); the API exposes `organization_id` null for a ROOT contract (`public_organization_id`).
- Templates (`/api/v1/contracts/folders/{folder_id}/templates`, `/templates/...`): an editable draft
  plus immutable numbered published versions that can be deprecated; availability to the folders
  below (`available-templates`) and create-from-template (`.../contracts/from-template`).
- List order: `GET /api/v1/contracts/contracts` takes `sort=id` (default) or `sort=name` (the Folders tree's per-folder
  query). The name key is `left(lower(coalesce(nullif(name,''), odcs_id)), 128)` then the id (`search.py`
  `name_sort_key`); the `n:`-prefixed cursor (`encode_name_cursor`, `decode_name_cursor`) carries that key, so the next
  page takes the key from the cursor only and never looks the row up. It is strict (one position, one cursor) and a cursor
  of the other sort is 422. The list row's `version.deprecated` (required) is true when the head version is deprecated and
  is also returned to metadata-only callers. The per-folder tree statement is backed by `ix_contract_folder_id`
  (`folder_id`, `id`; migration 0019): the recorded EXPLAIN was a Seq Scan without it, and `test_tree_query_plan.py` pins the index.
- Metadata-only reads for Policy Managers (`contract:read-metadata`, content fields redacted) and
  lists trimmed to the folders the caller can see (empty page, never 403).
- Drafts with revisions (`If-Match`/ETag), publishing a draft in place as an immutable version, the
  version lifecycle (Latest, Preview, older, unpublish, republish, promote, deprecate with sunset)
  and consumer fetch by `latest` or SemVer with conditional requests (`ETag`, 304,
  `Deprecation`/`Sunset`).
- Soft delete and restore of a contract, and the reference guard that refuses a delete while another
  contract's published version links to it.
- The `contract` and `contract_version` tables: the raw submitted bytes are the source of truth;
  the parsed view and search fields are derived. Published rows are immutable by database trigger.
  Publishing goes through the validation service (`ValidationPublishGate`, Phase 5): a version records
  `validation_run_id`, `pinned_rule_sets`, `engine_version` and `schema_sha` in the publish
  transaction, frozen afterwards by the guard trigger (migration 0014 names them in
  `publish_mutable`; a new pin column needs a new migration that adds it there). The lock order of
  `versions.publish_draft`: `If-Match`, then the gate (with the `GateContext` and the attachments read
  just before) before any lock, then the contract lock, the draft lock, the revision and content hash
  re-check (412), the `attachments_revision` check (409 `rules-changed`), the SemVer checks, the baseline re-check
  (409 `baseline-changed`: `compat.baseline_key` must equal the baseline id and stable major floor the gate
  was given), `index_references`, the link-facts re-check (`link_facts.recheck_link_facts`: target version
  rows locked `FOR KEY SHARE`, facts recomputed with the pre-gate scopes; a fact that got worse is also
  409 `baseline-changed`, a better one is accepted, IN-03) and the write. The gate is
  fail-closed: an outage, timeout, 5xx or a different hash is 503 `validation-unavailable`, a verdict
  that is not publishable is 422 `contract-validation-failed`; nothing publishes without a verdict.
- Subscriber cards (`/api/v1/contracts/folders/{folder_id}/cards`, `/cards/{card_id}`): a card is
  owned by a folder of any kind (Root, Organization, Project or Team; `organization_id` is null for a Root card, migration 0018, quick 261119-wo3), created by a Consumer or an admin above, edited and archived by
  its creator or the folder's admins (`card:manage`), and unknown to everyone who cannot read the
  folder (uniform 404, also during identity failures). Table `subscriber_card` (migration 0009; 0018 made `organization_id` nullable).
- All-cards list (`GET /api/v1/contracts/cards`, `card_index.py`): `card:read` gate, rows trimmed in SQL to
  `accessible_folders(folder:read)` plus the caller's grantees in `card_member` (the card-read rule), so totals
  (`total_cards`, `total_folders`) count only visible cards; `folder_id` + `subtree`, `q`, `include_archived`,
  `has_active_subscriptions`; at most `PIN_PREVIEW_LIMIT` active pins per card, the contract name only inside the
  caller's contract list scopes. 503 on an identity outage, never an unfiltered page.
- Card members (`/cards/{card_id}/members`): a manager links local groups, directory groups and
  service accounts after identity's `members/check` agrees (422 `invalid-member`); members read the
  card (`card_has_member`, via `GranteeAuthorizer.grantees`) but are never managers (403 on writes).
  Table `card_member` (migration 0010).
- Subscriptions (`/cards/{card_id}/subscriptions`, `.../move`, `.../unsubscribe`): a card manager or
  linked member pins concrete published versions with the acting principal's own read access (D-86);
  pins record dependency, never access, and there is no card-scoped fetch (D-88). Table
  `card_subscription` (migration 0011): rows are history, never deleted, and only ended once, by
  trigger. Archiving a card ends its pins. `subscriptions.CARD_GRANTS_ACCESS` and
  `contract.subscription_policy` are the switched-off D-83 seams.
- Rule-set attachments (`attachments.py`, `api/attachments.py`, RULE-06, D-123): `GET|PUT|DELETE
  /contracts/{contract_id}/rule-sets[/{rule_set_id}]`. Visibility is decided by validation with the
  caller's own bearer (`ValidationClient.available_rule_set`) before any lock (422
  `rule-set-not-attachable`, 503 on an outage); every attach, re-pin and detach takes the contract
  lock, bumps `contract.attachments_revision` and records `contract.ruleset_attached` or
  `contract.ruleset_detached`. A mandatory set is a 200 `mandatory` no-op, never a row. Publish sends
  the attachments and the revision and answers 409 `rules-changed` when it changed before the lock.
  Table `contract_ruleset_attachment` (migration 0015); `locks.lock_contract` (re-exported by `versions`).
- Owner side (`GET /contracts/{contract_id}/subscribers`, `POST /contracts/{contract_id}/subscriptions/{subscription_id}/remove`):
  `contract:read` holders page through the cards with active pins per version (name, description,
  contacts, pin time, pinner and the pin's subscription id only; never members, channels or other pins, D-102); any
  `contract:publish` holder removes a pin with a required reason, and the removal event is the
  card's `card.subscription.removed` notice carrier (D-94, D-99). Both live on `api/versions.py`.
- Lifecycle guards (SUB-06): `versions.unpublish_version` and `versions.delete_contract` collect
  every blocker (latest, referenced, pinned) and raise one `LifecycleBlocked` 409 with a `reasons`
  array (D-106); pinned cards are named across organizations (D-95). A refused unpublish rolls its
  unit back and then records `contract.version_unpublish_blocked` (never inside the locked unit),
  which the notice consumer throttles per card and version (`DC_BLOCKED_NOTICE_COOLDOWN_S`).
- Card notices (`GET /cards/{card_id}/notices`): lifecycle events (deprecation, publish, Latest
  change) carry `notify_card_ids` in their audit event; `notices.handle_payload` records one
  `card_notice` per card and event with per-channel `card_notice_delivery` rows, exactly once.
  Tables `card_notice` and `card_notice_delivery` (migration 0012). A publish also records
  `contract.version.breaking_published` (SUB-08, migration 0016) for cards pinned to a version it
  breaks: the publish event's `notify_pins` snapshot, diffs in the consumer, dedupe key
  `event:<id>:breaking`; handlers return a list of `NoticePlan`s, each with its own dedupe key.
- Webhook channel configuration (`webhooks.py`, SUB-07): `PATCH /cards/{card_id}` sets `webhook_url`
  (https, no userinfo, globally routable addresses only unless allow-listed) and `webhook_enabled`;
  enabling creates a `whsec_` secret, Fernet-encrypted in `card_webhook_secret` (migration 0013, key
  from the `webhook_secret_key` Docker secret) and returned once; `POST /cards/{card_id}/webhook-secret`
  rotates it with an overlap window. Both need an interactive sign-in (`card:manage-secret`, D-79).
  Only managers see the full URL on a read. Audit actions `card.webhook_secret_rotated`; a card
  update carries `webhook_host` and `webhook_secret_generation`, never the URL or a secret.
- Webhook delivery (`webhooks.py` signer half, `delivery.py`, SUB-07): `CardNotifier` is the in-service
  worker (`app.state.card_notifier`, started by `notifier_hook` only when `DC_NOTIFY_WORKER_ENABLED`).
  One replica delivers (session advisory lock `dc-card-notifier`); due `card_notice_delivery` rows are
  claimed with `FOR UPDATE SKIP LOCKED` and leased, sent outside any transaction as a Standard
  Webhooks request (`webhook-id` `msg_<delivery id>`, one `v1` signature per valid secret) and the
  result recorded in a second short transaction. Failures retry on `DC_NOTIFY_RETRY_SCHEDULE_S` with
  jitter up to `DC_NOTIFY_MAX_ATTEMPTS`, then `failed`; the worker never writes a card's channel
  flag, and `last_error` is a code from a fixed list. Lifecycle writes never call it. Every attempt
  resolves the host again (`check_resolved_host`); a non-global address (multicast included) is
  `ssrf-blocked`, an unresolvable host `dns-error`, neither opens a connection, and the request is
  pinned to the validated address with `Host` and the TLS SNI kept on the hostname
  (`pinned_request_parts`). Allow-listed hosts connect by name.
- Notification settings (quick 261120-0yd): `runtime_settings.py` (`effective_notifications`,
  `worker_notifications`, `settings_client_hook`) merges the Global admin's stored `notifications`
  section (identity, section of `dc_core.platform_settings`) over the `ContractsSettings` env values
  through `app.state.settings_client` (a `RuntimeSettingsClient`, 30 s cache, last good value). The
  delivery worker and the sunset scheduler read it once per pass or scan with the notifier key
  (`mail_api_key`) as bearer (`CardNotifier.settings_client`, `SunsetScheduler.settings_client`);
  the card PATCH reads it with the caller's bearer. Stored: email templates, retry schedule, max
  attempts, sunset leads, allowed webhook hosts. With no `mail_api_key` the worker makes no read and
  uses the env values, so it allows no webhook host. `webhook_allowed_hosts` is mandatory since quick
  261120-s0p (an empty list allows nothing): PATCH with a host that is not listed is 422
  `webhook-host-not-allowed` (after the URL shape check, before DNS; the detail points at Settings >
  Notifications; an IP-literal URL is `invalid-webhook-url`), a delivery to one ends terminal `failed`
  with the same code and no request; when the list was never read, or the one-time seed is not
  confirmed yet, PATCH is 503 `settings-unavailable` and the delivery is deferred 60 s with
  `last_error` `settings-unavailable`, no attempt used. A webhook URL edit invalidates the settings
  client first, so a host the admin just added is accepted at once. A private address is never
  admitted by the list (`DC_WEBHOOK_ALLOWED_PRIVATE_HOSTS` stays Helm-only). `webhook_host_seed.py`
  (`WebhookHostSeed.done`, `configured_hosts`, `run_seed`, `webhook_host_seed_hook` after
  `settings_client_hook`, `DC_WEBHOOK_HOST_SEED_ENABLED`): at start-up the distinct normalised hosts
  of non-archived cards with a `webhook_url` are posted to identity's internal
  `/internal/v1/settings/webhook-hosts/seed` with the notifier key (retries with backoff up to 300 s;
  logs `webhook_host_seed_done` or `webhook_host_seed_skipped`; no key or seeding off is done at once). `api/settings.py`:
  `POST /api/v1/contracts/settings/test-email` (`settings:write`, interactive, 5 per 10 min per
  principal and 30 per hour per process in `app.state.test_email_limiter`, the caller's own bearer to
  the mail service, one `settings.test_email_sent` outbox event with the recipient domain only).
  Contracts' about reports the notifications defaults (`setting_defaults`).
- Governance settings (quick 261120-0ye): `runtime_settings.effective_governance` (stored `governance` over `ContractsSettings`
  `default_odcs_api_version` / `require_lifecycle_reason`, i.e. `DC_DEFAULT_ODCS_API_VERSION` and `DC_REQUIRE_LIFECYCLE_REASON`, with
  the caller's bearer; a stored version this deployment does not support falls back to the env one). The starter route builds its
  document with the effective apiVersion. Deprecate (`reason?` in the body) and unpublish (optional body `{reason}`) take a
  1 to 500 character reason; with the requirement on a missing one is 422 `lifecycle-reason-required` before any lock is taken
  (`api/versions._require_reason`). The reason goes into the event's `after` and the deprecated notice payload (`{reason}`).
- Email delivery (`mailer.py`, SUB-07, D-105): the worker's `email` branch renders the subject and
  plain text (`render_email(notice, card, templates)`, defaults or the stored override rendered by
  `dc_core.platform_settings.render_template`: plain `{placeholder}` substitution of sanitised,
  bounded values, never `str.format`; the rendered subject is sanitised again; the golden test
  `tests/unit/test_email_golden.py` pins the defaults byte for byte) and POSTs one `dc_core.mail.MailSendIn` to
  `DC_MAIL_URL` + `/internal/v1/mail/send` with the `mail_api_key` Docker secret (the notifier
  service account's key) for every email contact read at send time. Same lease, retry schedule and
  terminal state as webhooks; the mail service never retries; contracts never speaks SMTP.
  `last_error` codes: `mail-not-configured`, `mail-http-<status>`, `mail-timeout`,
  `mail-connect-error`, `mail-bad-response`, `mail-invalid-message`,
  `mail-recipients-refused[:n]` (partial permanent refusal is `delivered`),
  `mail-recipients-deferred` (a 4xx refusal, retried), `no-recipients`,
  `channel-disabled`, `card-archived` (the card was archived after the notice), `webhook-host-not-allowed`, `settings-unavailable`, `internal-error` (an exception in the send step, counted as a failed attempt).
- Sunset notices (`scheduler.py`, SUB-07, D-96): `SunsetScheduler` (`app.state.sunset_scheduler`,
  started by `scheduler_hook` only when `DC_SUNSET_SCHEDULER_ENABLED`) scans every
  `DC_SUNSET_SCAN_INTERVAL_S` under the session advisory lock `dc-card-sunset` and records one
  `contract.version.sunset_approaching` notice per active pin per lead in `DC_SUNSET_LEAD_DAYS`
  through `notices.notify_card` (dedupe key `sunset:<subscription_id>:<lead>:<sunset_date>`; UTC
  dates; `pick_lead` is the smallest lead with `days_left <= lead`). No outbox event.
- Folder usage and purge (quick 261119-0v1; `folder_purge.py`, `api/folder_usage.py`): identity calls
  `GET /internal/v1/contracts/folders/{id}/usage` (counts only: live contracts, templates and live cards
  block; soft-deleted contracts and archived cards are leftovers) and `POST .../purge` with the caller's
  forwarded bearer (`folder:delete`, Global admin only; internal, never in OpenAPI). The purge removes the
  leftovers and their history in one transaction under the transaction-local `dc.folder_purge` setting that
  migration 0017 teaches the version and pin delete triggers (honoured only for soft-deleted contracts and
  ended pins of archived cards or deleted contracts); it refuses 409 while a blocker is left; one
  counts-only `folder.purged` event.
- Database `contracts` (role `contracts`), version table `alembic_version_contracts`.

## What it must never import

Services import only the `dc_*` libraries (`dc_core`, `dc_auth`, `dc_odcs`). This module must not
import `dc_identity`, `dc_validation` or `dc_audit` (and has no use for `dc_rules`: validation's report
is passed through as JSON); folder facts and permission decisions arrive
over HTTP through `RemoteAuthorizer`. The `dc_*` libraries never import services.
`uv run lint-imports` enforces it.

## Key files and entry points

- `src/dc_contracts/main.py`: `create_contracts_app`
  (`uvicorn --factory dc_contracts.main:create_contracts_app`).
- `src/dc_contracts/api/contracts.py`: the contract routes (create, create-from-template, list,
  metadata, document), response models, `contract_folder` target resolver (unknown contract ->
  same 404 as an unreadable one, D-08).
- `src/dc_contracts/api/templates.py` and `templates.py`: template routes and domain service
  (draft, publish, deprecate, availability, `build_from_template`).
- `src/dc_contracts/ingest.py` (capped body read, parse and structural report off the event loop),
  `repo.py`, `search.py` (`scope_clause`), `convert.py` (YAML/JSON conversion on `Accept`), `models.py`,
  `settings.py` (`DC_MAX_DOCUMENT_BYTES`, default 1048576).
- `src/dc_contracts/drafts.py` (draft save under `If-Match`), `versions.py` (publish, create draft,
  promote, unpublish, republish, deprecate, delete and restore, lock order, `PublishGate`),
  `lifecycle.py` (SemVer rules, derived status), `notation.py` (`get_adapter`, in-place `copy_with`),
  `http_cache.py` (ETags, `If-Match`/`If-None-Match`, deprecation headers), `references.py`
  (reference index and delete guard).
- `src/dc_contracts/api/drafts.py` and `api/versions.py`: draft, publish, version fetch and
  lifecycle routes.
- `src/dc_contracts/validation_client.py` (Plan 05-15, VAL-07): `GateContext`, `GateResult`,
  `ValidationClient` (the internal run call with the caller's own bearer, status mapping),
  `ValidationPublishGate`, `FailClosedGate` and `validation_hook` (installs the real gate on
  `app.state.publish_gate` unconditionally; `DC_VALIDATION_URL`, `DC_VALIDATION_PUBLISH_TIMEOUT_S`).
  Blocking findings are 422 `contract-validation-failed` (report and `run_id` in the body); an
  unreachable, slow, failing or disagreeing validation service is 503 `validation-unavailable`. Never
  import `dc_validation` or `dc_rules` here. Plan 05-19 (VAL-06, VAL-08): `save_mode_embed` (after the
  commit, or after the transaction of an identical, no-op save is rolled back so its draft lock is
  released, attachments read in a fresh short session, never raises, `{"status": "unavailable"}` on any
  doubt) puts `validation` into the six draft-producing responses; `validate_draft` backs `POST
  .../drafts/{draft_id}/validate` (`contract:read`, 503 on an outage, nothing stored).
- `src/dc_contracts/compat.py` (Plan 06-08, VAL-02, VAL-03): `select_baseline` (highest-precedence
  published version by `lifecycle.compare_semver`; floor = highest stable major), `baseline_and_floor`
  (id, bytes), `baseline_key` (cheap, under the lock). Every run (publish, save, draft validate) sends
  `baseline`, `stable_major_floor` and `ref_facts`; there is no override for a breaking change.
- `src/dc_contracts/link_facts.py` (D-153): `resolve_link_facts` / `draft_link_resolver`, caller-scoped
  link states (`published`, `unpublished_or_draft_only`, `not_found`; a contract the caller cannot read
  is `not_found` like a missing one), at most 200 links, read in the unlocked block next to the baseline.
  An identity outage while reading the scopes sends no facts for save and draft validate (advisory)
  but is 503 `service-unavailable` for publish (`draft_link_resolver(..., strict=True)` in
  `api/drafts.py`; WR-02): never let publish run on `unverifiable` links.
  A pinned version above `dc_odcs.MAX_VERSION_CHARS` is reported as no pin (`pinned_version_of`),
  and `compat.baseline_payload` sends a longer baseline `semver` as `null`: validation's caps are
  the same constant, so no published version can make a later run a 422 (CR-02, WR-01).
- Report fetch and changelog (Plan 06-10, VAL-09): `GET .../versions/{ref}/validation-report` on
  `api/versions.py` (version-fetch policy and visibility, then `ValidationClient.get_run` with the
  caller's bearer; validation re-authorises the run read; 404 `validation-report-not-recorded` for a
  version without a run; ETag `"run-<id>"`) and `GET .../contracts/{contract_id}/diff`
  (`diffs.py`: refs are SemVer, `latest` or a draft id for `contract:read` holders, computed in a
  thread, offset cursor, never stored).
- Rule impact (Plan 06-12, RULE-10, RULE-11, D-158 .. D-161): `impact.py` (keyset pages of
  non-deleted contracts with a Latest whose `ancestor_ids` contain the scope folders, filtered by
  the caller's route-permission scope, cut by `DC_IMPACT_PAGE_SIZE`, `_MAX_FOLDERS` and `_MAX_BYTES`
  within validation's batch caps; names only inside the `contract:read-metadata` scope, others
  counted as `hidden`) and `api/governance.py`: `POST /governance/rule-set-dry-runs`
  (`ruleset:dry-run` at the rule set's folder from `ValidationClient.rule_set_folder`; the 4 KiB body
  is read inside the target resolver, after authentication) and `GET /governance/drift?folder_id=`
  (`governance:drift-read` at the query folder). No state change and no notice, but one
  counts-only audit event per answered page (`governance.rule_set_dry_run` on the rule set,
  `governance.drift_read` on the folder; CR-01). JSON Pointers only for contracts in the caller's
  `contract:read` scope (`impact.visible_rules`; otherwise emptied with `pointers_withheld: true`,
  D-36); the residual verdict oracle is AR-6-03 in `06-SECURITY.md`. Settings:
  `DC_IMPACT_REQUEST_MAX_BYTES` (4096), `DC_IMPACT_PAGE_SIZE` (25), `DC_IMPACT_PAGE_MAX_FOLDERS` (20),
  `DC_IMPACT_PAGE_MAX_BYTES` (4 MiB; each refused at start-up above `settings.VALIDATION_BATCH_CAPS`,
  a copy of validation's default `DC_MAX_BATCH_*` pinned by `tests/repo/test_batch_caps_coupling.py`;
  lower a validation cap only together with the matching contracts setting, IN-06) and
  `DC_VALIDATION_IMPACT_TIMEOUT_S` (30).
- `src/dc_contracts/cards.py` and `api/cards.py`: subscriber card service (`card_capability`) and
  routes. For `need` `read` and `subscribe`, `card_capability` asks membership first and answers a
  linked member whom `accessible_folders(folder:read)` excludes without the `folder:read` check,
  so a successful member read records no false denial (WR-02); `_managed_card` makes that check
  for a member's refused write so the refusal is recorded. Residual: a member covered only by a
  global scope with excluded subtrees still gets the folder check (and a denial when it fails).
- `src/dc_contracts/subscriptions.py` (subscribe, move, unsubscribe under the contract lock) and
  `pins.py` (leaf pin queries; never imports `versions`).
- `src/dc_contracts/notices.py` (audience queries, `handle_payload`, `materialise`, `list_notices`;
  never imports `versions`, which imports it).
- `src/dc_contracts/notice_consumer.py` (the durable JetStream consumer `contracts-notices` on stream
  AUDIT, filter `audit.contracts.>`; feeds `notices.handle_payload`; never touches the stream; its
  broker permissions are pinned in `infra/nats/nats.conf.tmpl`).
- `src/dc_contracts/scheduler.py` (`pick_lead`, `SunsetScheduler`, `scheduler_hook`).
- `src/dc_contracts/access_events.py` (`record_content_read`, `ACCESS_SUBJECT`, `DETAIL_KEYS`).
- `src/dc_contracts/mailer.py` (`EMAIL_SUBJECTS`, `render_email`, `send_email`, `EmailOutcome`).
- `src/dc_contracts/runtime_settings.py` and `src/dc_contracts/api/settings.py` (quick 261120-0yd, see above).
- `src/dc_contracts/delivery.py` (`CardNotifier`, `retry_delay`, `notifier_hook`; webhook sending
  goes through `dc_core.http.make_client`).
- `src/dc_contracts/migrate.py` and `migrations/`.

## Tests

- Unit (no Docker): `uv run pytest -m unit contracts`
- Integration (needs Docker): `uv run pytest -m integration contracts`
- Route policy walker: `contracts/tests/unit/test_route_policies.py`.
- Notification settings: `contracts/tests/unit/test_runtime_settings.py`, `test_email_golden.py`, and the integration module `contracts/tests/integration/test_settings_runtime.py`.
- The Phase 6 journeys: `bash scripts/e2e.sh tests/e2e/test_phase6_semantic_journey.py
  tests/e2e/test_phase6_rule_impact_journey.py`.
- The Phase 7 journey (E2E-01): `bash scripts/e2e.sh tests/e2e/test_phase7_engineer_journey.py`
  (create from a template, a refused publish recorded as `contract.version.publish_refused`, fix,
  publish, other-team and service-account reads recorded as access events, a refused write
  recorded as a denial; local and Entra authors).
- Whole repo gate: `bash scripts/ci.sh`.

## Platform rules that apply here

- Every route is declared with `require(...)` or `public()` on a `PolicyRouter` and listed in the
  walker test. The create route reads the body only after the policy dependency authorised the
  caller (no Pydantic body parameter, so unauthenticated callers never make the service parse).
- One `uow.audit(...)` per business write (`contract.created`, `contract.draft_created`,
  `contract.draft_updated`, `contract.draft_deleted`, `contract.version_published`,
  `contract.version_promoted`, `contract.version_unpublished`, `contract.version_republished`,
  `contract.version_deprecated`, `contract.deleted`, `contract.restored`, `template.created`,
  `template.draft_updated`, `template.published`, `template.version_deprecated`, `card.created`,
  `card.updated`, `card.archived`, `card.member_added`, `card.member_removed`,
  `card.subscribed`, `card.subscription_moved`, `card.unsubscribed`,
  `card.subscription_removed`, `contract.version_unpublish_blocked`,
  `card.webhook_secret_rotated`, `contract.ruleset_attached`, `contract.ruleset_detached`,
  `contract.version.publish_refused`); the event carries
  metadata, the content hash and the size only, never document content (`__audit_fields__`
  allow-list). Publish, promote, deprecate and `contract.version_unpublish_blocked` events also
  carry a `notify_card_ids` key (card ids only), read under the lifecycle locks; the publish event
  also carries `notify_pins` (`[{card_id, version_id}]`, for breaking notices); the notice
  consumer turns them into notices later, so no lifecycle write ever calls a channel.
- `contract.version.publish_refused` (D-175, Phase 7) is recorded by `api/drafts.py`
  `_record_refused_publish` only for the gate's 422 `contract-validation-failed`, after the refused
  unit is rolled back, in its own transaction, with the contract's `ancestor_ids` as scope. Its
  `after` is `{draft_id, semver (declared version, at most 256 chars), run_id, blocking}`, where
  `blocking` is at most 50 `{rule_key, code}` pairs of blocking findings (`code` = `error_code`, else
  `outcome`; `odcs-json-schema`/`structural` first for a schema failure); never a message, pointer or
  document value. A recording failure is logged (ids and error class) and swallowed: it never
  changes the 422. The notice consumer ignores it.
- Access events (Phase 7, AUD-04, D-171): every 200 that returns contract document content
  calls `access_events.record_content_read` just before it returns: `GET
  .../contracts/{contract_id}/versions/{ref}` (Latest, SemVer, older, Preview, converted
  renderings; `ref` is `latest` or the resolved SemVer, never the raw input), `GET
  .../contracts/{contract_id}/document` (`ref` `head`) and `GET
  .../contracts/{contract_id}/drafts/{draft_id}/document` (`ref` `draft`), plus the 201 of `POST
  .../folders/{folder_id}/contracts/from-version`, which records the **source** contract (its
  SemVer as `ref` with `version_id`, or `ref` `draft` with `draft_id`) with `scope_path` the source
  folder's ancestors from `_check_source`'s decision (WR-04). One best-effort event
  on `access.contracts.contract.read` (stream ACCESS): aggregate the contract, action `read`,
  category `access`, `before`/`after` null, scope the folder's `ancestor_ids`, the caller as actor,
  the correlation id, and `detail` only from `DETAIL_KEYS` (`contract_id`, `version_id` or
  `draft_id`, `semver`, `ref`, `media_type`; strings at most 512 characters), never content. No
  event for a 304, any 4xx/5xx, or the metadata, versions list, search, diff, validation-report,
  draft-metadata and template-document routes. The helper never awaits and never raises (a failure
  is logged as `access_event_not_recorded` with the error class); `dc_core.access.access_log_hook`
  installs `app.state.access_log` only when `DC_NATS_URL` is set, otherwise it is None and the
  helper does nothing. Loss during a broker outage is accepted; blocking a read is not. The
  contracts NATS user may publish `access.contracts.>` and no other `access.` subject.
- Denials (Phase 7, AUD-04): contracts records none itself. Every `require()` check and every
  in-handler probe check (the source-contract check of create-from-version, the card
  `folder:read` check, subscription read checks) goes to identity through
  `RemoteAuthorizer`, which sends `service` `contracts`, the matched route template and the
  caller's `X-Request-ID`; identity writes the denial row before it answers, and an unrecordable
  denial reaches contracts as 503 (`AuthorizerUnavailable`). A refused write such as a Consumer's
  `POST .../contracts/{contract_id}/drafts` is therefore a 404 to the caller and one `not_found`
  denial at the contract's folder. A 404 a handler derives from `Decision.permissions` (a
  published-only reader on a contract without a Latest, an unpublished version by fetch, diff or
  create-from-version source, a draft id in a diff, a never-published template, the metadata of a
  draft-only contract) is raised as `await dc_auth.deps.recorded_refusal(request, <decisive
  permission>)`, which asks the PDP for that permission first so the denial is recorded (WR-01);
  never `raise _not_found()` for such a branch. The one exception is the pin of an unpublished
  version (`subscriptions.py`), refused under the contract lock where no identity call is made.
- No override of the compatibility gate: a breaking change publishes only with a declared major
  above the stable major floor (validation's native `semver-breaking-change` finding); never add a
  field, header, setting or role around it.
- Errors are RFC 9457 problems with `urn:dc:problem:<slug>` types listed in
  `docs/user/api-guide.md` ("Errors: RFC 9457 problem details").
  `odcs-id-mismatch` (draft save and publish, `drafts.odcs_id_mismatch`) names the contract's id and the
  document's id in `detail` and in the `expected_odcs_id` / `found_odcs_id` members (quick 261119-wk8; neither
  is secret). Structural unknown-property errors (`dc_odcs.validate`) carry the offending property keys (keys
  only, at most 5, 64 characters each, control characters removed) in `StructuralError.unexpected`.
- Own migrations and version table; never touch another service's database.

## Skills

- `.claude/skills/contract-ingest/SKILL.md`: the dc_odcs pipeline, templates, create-from-template and redaction.
- `.claude/skills/contract-lifecycle/SKILL.md`: drafts, publish, Latest, promote, unpublish, deprecate, consumer fetch, lock order and the delete guard.
- `.claude/skills/subscriber-cards/SKILL.md`: cards, members, pins, the owner view, lifecycle guards, notices, webhook and email delivery, secrets and the sunset scheduler.
- The mail service (`mail/CLAUDE.md`, `mail/.claude/skills/mail-service/SKILL.md`) owns SMTP; this module only calls it.
- Repo-level skills (run-stack, add-route, add-migration) live in the root `.claude/skills/`.
