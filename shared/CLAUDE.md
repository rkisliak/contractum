# shared (libraries `dc-core`, `dc-auth`, `dc-odcs`, `dc-rules`)

The platform libraries every service builds on. Each is its own uv workspace member under
`shared/` with its own `pyproject.toml`, `src/` and `tests/`.

## What this module owns

- `dc-core` (`dc_core`): service factory and middleware, route policies, problem details, unit of
  work and audit tripwire, outbox relay, event envelope and transport seam, Alembic helpers,
  settings, logging, HTTP client factory, shared pytest plugin, the capped raw-body reader
  (`body.py`) and the strong `If-Match` evaluation (`conditional.py`).
- `dc-auth` (`dc_auth`): issuer registry, async JWKS cache, token verification into a typed
  `Principal`, the `Authorizer` protocol and its remote client, the `require()` route policy,
  the in-process fake issuer for tests, the Entra issuer kind (`IssuerConfig.kind`, `tenant_id`),
  opaque `dcp_` token resolution (`opaque.py`), `roles.py` (the Phase 2 role, permission and
  grant-ceiling tables as frozen data, with `access`, `interactive_only` and `READ_PERMISSIONS`) and `oracle.py` (the brute-force access reference used to prove
  identity's SQL evaluator).
- `dc-odcs` (`dc_odcs`): strict ODCS parsing, the `apiVersion`/`kind`/`id` gate, vendored ODCS
  JSON Schemas (v3.1.0 and v3.2.0, with a manifest recording their source) and the structural
  report; the breaking-change classifier `dc_odcs.diff` (Phase 6, pure stdlib: normalise keyed
  collections, walk, classify through the rule table; `compare`, `required_bump`, `Change`,
  `DiffResult`, `BumpVerdict`, `CLASSIFIER_VERSION` = `odcs-compat-1`, at most `MAX_DIFF_CHANGES`
  10,000 changes, `before`/`after` only for `value_allowed` attributes) and `element_refs`
  (`reference_inputs`, `resolve_internal`: the elements and servers a document references,
  resolved against the document itself, tri-state) and `pointer.string_at(document, pointer)` (the
  one RFC 6901 string lookup contracts' `link_facts` and validation's `facts` share; never raises).
- `dc-rules` (`dc_rules`): the declarative rule engine core, pure and with no I/O: the rule and
  rule-set model (`RuleDef`, `RuleSetContent`, canonical JSON and content hash), the
  `ExpressionEngine` and `Targeter` ports with `Limits` and error codes, the CEL adapter
  (`cel-expr-python`, the `cel-1` and `cel-2` surfaces), the strict RFC 9535 JSONPath adapter, `evaluate_rule`
  and `normalize_document`; the wire `protocol` (JSON-line job, result and marker models and
  `LimitsWire`), the rule-set `checks` (static problems, fixture units, `assemble_check`) and the
  `worker` (`python -m dc_rules.worker`, the subprocess main loop with its memory limit). `cel-2`
  (Phase 6) binds `previous`, `changes` and `refs` on top of cel-1's `this`, `contract` and `ctx`
  and adds the pure helper `dc.type_coherent` (`functions.py`) and the cel-2-only `ctx.operation`
  (`save` or `publish`; the worker strips it for cel-1); a stored cel-1 version always
  compiles in the cel-1 environment, so its semantics never change. `checks.check_units` takes an
  injected `changes_for` so fixture `changes` come from the caller's classifier (`dc_rules` still
  imports no other `dc_*` package). Only the
  validation worker imports the engine modules (`cel_engine`, `targets`, `evaluator`); the validation
  web process uses `model`, `ports`, `protocol` and `checks` and loads no engine.

## What it must never import

The `dc_*` libraries never import a service (`dc_identity`, `dc_contracts`, `dc_validation`,
`dc_audit`); `uv run lint-imports` enforces it. `dc_core` imports no other `dc_*` library;
`dc_auth` may import `dc_core`. `dc_rules` imports no other `dc_*` package at all. Services import
only these libraries.

## Key files and entry points

- `dc-core/src/dc_core/openapi.py` (`build_openapi`, `PROBLEM_SCHEMA`, `BEARER_SCHEME`): the one
  OpenAPI post-processor `create_app` installs for every service (tags, policy-derived
  `bearerAuth` or `security: []`, common problem statuses, every 4xx/5xx as
  `application/problem+json` with the shared `Problem` schema). `PROBLEM_SCHEMA` must stay
  byte-identical across services; changing it changes every service document and the gateway
  merge. Checked by `tests/repo/test_openapi_completeness.py`.
- `dc-core/src/dc_core/app.py` (`create_app`, `EncodedPathGuard`, `read_session`), `policy.py`
  (`PolicyRouter`, `public`, `assert_all_routes_have_policy`), `problems.py` (`ProblemError`,
  `install_problem_handlers`), `uow.py` (`UnitOfWork`, `AuditColumnsMixin`, `OutboxMixin`),
  `events.py`, `relay.py`, `migrations.py` (`migration_cli`, `run_alembic`, `run_env`: the body of every service's `migrations/env.py`), `settings.py`.
  `security_headers.py` (`SecurityHeadersMiddleware`, outermost in `create_app`): `nosniff` and a default
  `Cache-Control: no-store` on every response, a handler's own value wins, the service's own
  `openapi.json` is exempt.
  `body.py` (`read_body_capped`: refuses early on `Content-Length`, aborts the stream past the cap,
  413 `payload-too-large`; call it only after the policy dependency passed) and `conditional.py`
  (`require_if_match`, `if_match_ok`, `parse_etag_list`: strong comparison, 428 without the header,
  412 on a stale or weak tag, `*` matches an existing resource; problems never echo the header).
  Contracts and validation both use them.
- Quick 261120-0yc (platform settings): `dc-core/src/dc_core/platform_settings.py` is the one place that knows the
  settings sections: Pydantic section models (`extra="forbid"`, every field optional: `EntraSettings`, `Banner`,
  `PlatformSettings`), the registries `SECTIONS` and `RUNTIME_SECTIONS`, `normalise_section`, the versioned export
  (`EXPORT_FORMAT` `contractum-settings`, `build_export`, `parse_export` raising `SettingsImportError` with a JSON
  pointer) and `RuntimeSettingsClient` (30 s cache, last good value on failure, `None` only when a section was never
  read; the caller then uses its Helm/env default; a setting that narrows a security control must fail closed
  instead). `dc-auth/src/dc_auth/about.py` is the internal `GET /internal/v1/about` factory
  (`about_router(service, session_factory=, version_table=, setting_defaults=)`, `settings:read`) that all five
  services include. Permissions `settings:read`/`settings:write` (ROOT, Global admin only, write interactive only)
  and `settings:read-public`/`settings:read-runtime` (principal-level) are in `roles.py`.
- Quick 261120-0yd (notifications settings): `platform_settings.py` also holds `NotificationsSettings`
  (`email_templates`, `sender`, `retry_schedule_s`, `max_attempts`, `sunset_lead_days`,
  `webhook_allowed_hosts`; registered in `SECTIONS` and `RUNTIME_SECTIONS`), the default email texts
  (`DEFAULT_EMAIL_SUBJECTS`, `DEFAULT_EMAIL_BODY`, byte for byte the old mailer texts) and the safe
  template functions `validate_template` / `render_template` (one tokenizer: `{{`, `}}` and allow-listed
  `{name}` only, no `str.format`, no attribute or index access; `SUBJECT_PLACEHOLDERS`,
  `BODY_PLACEHOLDERS`), `check_host_pattern` / `webhook_host_allowed` (the list is mandatory since quick 261120-s0p: an empty list allows nothing; it still only narrows the other webhook checks),
  `check_email_address`, `notifications_defaults` and `RuntimeSettingsClient.invalidate`.
- Quick 261120-0ye (governance and security settings): `platform_settings.py` also holds `GovernanceSettings`
  (`mandatory_warn_days` 0 to 365, `default_odcs_api_version` `^v\d+\.\d+\.\d+$`, `require_lifecycle_reason`; in `RUNTIME_SECTIONS`),
  `SecuritySettings` (identity only, registered in `SECTIONS`, never in `RUNTIME_SECTIONS`: sign-in switches, credential ceiling,
  session and idle limits with idle at most the session length, lockout thresholds, password length floor 12 and four character
  rules; strict booleans) and `PlatformSettings.audit_retention_days` (`0` or 90 to 36500). Every bound lives in the model; the
  checks that need identity data (supported ODCS versions, the lockout rules) stay in identity.
- Quick 261120-lbz: `dc-core/src/dc_core/names.py` is the one rule for user-entered names (`NameRule` Pydantic marker, `check_name`,
  `first_disallowed`, `code_point`, `NAME_RULE_HINT`; letters, marks, numbers, space and `- _ . , ( ) & ' / : # + @` only, 422 naming the
  first offending character as `U+XXXX`); `SecuritySettings.max_active_tokens` (1 to 1000); `dc_odcs.MAX_ODCS_ID_LENGTH` (255).
- Phase 7 additions to `dc-core`:
  - `dc-core/src/dc_core/events.py`: `ACCESS_STREAM` (`ACCESS`, subjects `access.>`, created by
    the audit service), `subject_for(..., prefix="access")` (`SUBJECT_PREFIXES` = `audit`,
    `access`), `build_access_envelope` (category `access`, `before`/`after` None, a uuid7
    `event_id` that is also the `Nats-Msg-Id`) and an optional flat `detail` in `build_envelope`
    (added only when the outbox row has a non-null `detail`, today identity's denial rows).
  - `dc-core/src/dc_core/access.py` (`dc_core.access`): `AccessLog`, the best-effort side channel
    for access events. `record()` is sync, O(1) and never raises; the queue is a bounded
    drop-oldest deque (default 10 000) flushed in batches of 100 at least every second, each
    publish bounded by a timeout with backoff after a failure, every loss counted by reason
    (`queue_full`, `publish_failed`, `invalid`, `shutdown`) and warned on the first and every
    1000th drop. `access_log_hook(settings)` runs it only when `nats_url` is set (otherwise
    `app.state.access_log` is None and producers do nothing) and flushes for 2 s on shutdown.
  - `dc-core/src/dc_core/openapi.py`: see `build_openapi` above.
  - `dc-core/src/dc_core/nul_guard.py`: `NulGuardRoute` (the default route class of `PolicyRouter`)
    refuses a NUL or a lone UTF-16 surrogate (U+D800 to U+DFFF) in query parameters and parsed
    JSON bodies with a 422 before authentication; `find_unstorable` (pointer and detail),
    `find_nul` (pointer only), `nul_problem(..., detail=)` and `NoNulStr` for raw-body routes and
    hand-validated DTOs. The names keep `nul` but every helper covers both cases.
- Phase 7 additions to `dc-auth`:
  - `authz_wire.py`: `CheckContext(service, route)`, `UNKNOWN_CONTEXT` and `check_context(request)`
    (the app's `service` and the matched route template, never a raw URL); `AuthzCheckIn.service`
    and `.route` are lenient (an invalid value becomes None and is recorded as `unknown`, never a
    422).
  - `remote.py`: `RemoteAuthorizer(..., context=...)` sends `service` and `route` in every check
    body and forwards the caller's `X-Request-ID` on every post, so identity's denial row carries
    the caller's service, route template and request id.
  - `roles.py`: `audit:list` (principal-level read, the route gate of the public audit query; any
    non-restricted principal, PATs and keys included) and `audit:query` (folder-evaluated read,
    never a route gate: the audit scope, held by Global admin, Global reader, Org admin, Project
    admin and Team admin).
- `dc-auth/src/dc_auth/deps.py` (`authenticate`, `require`, `unit_of_work`, `auth_lifespan`,
  `recorded_refusal`: the uniform 404 for a refusal derived from `Decision.permissions`, after a
  PDP check of the decisive permission has recorded the denial; 503 when it cannot answer; WR-01),
  `verify.py` (including the Entra mapper), `issuers.py`, `jwks.py`, `opaque.py`,
  `authz_wire.py` (wire models, `MEMBER_TYPES`, `passthrough_problem` and Phase 6's
  `PrincipalResolveOut`), `RemoteAuthorizer.resolve_principals` (Phase 6: tri-state user and group
  existence from identity's `POST /internal/v1/authz/principals/resolve`; an identity problem is
  `unverifiable`, never a false `not_found`; `FakePrincipalDirectory` in `testing.py`), `principal.py`, `authorizer.py` (`GranteeAuthorizer` sub-protocol, `Decision.permissions`, `FolderScope` with
  `excluded_subtrees` and `covers()`), `roles.py`, `oracle.py`, `remote.py`, `testing.py`.
- `dc-odcs/src/dc_odcs/__init__.py` (the public surface), `gate.py`, `loader.py`, `validate.py`,
  `extract.py`, `schemas/`, `diff/` (`model.py`, `normalise.py`, `walk.py`, `table.py`,
  `bounds.py`; the golden corpus and the rule-table completeness gate are in `tests/unit`, the
  datacontract-cli oracle is opt-in and never in the lock), `element_refs.py`, `refs.py`
  (`extract_contract_refs`: `distinct="target"` for the reference index, `distinct="raw"` for
  link facts so each link string gets a fact, IN-04; `MAX_VERSION_CHARS` = 255 (the `semver` library's own limit), the one cap on version text that
  contracts and validation exchange: a baseline's `semver` and a link's pinned version; both
  services import it, never a literal).
- `dc-auth/src/dc_auth/roles.py` permissions added in Phase 6: `ruleset:dry-run` (Policy Manager,
  Global admin) and `governance:drift-read` (Policy Manager, Org admin, Global admin).
- `dc-rules/src/dc_rules/model.py`, `ports.py` (`Limits`, `ErrorCode`, ports), `cel_engine.py`,
  `targets.py`, `evaluator.py` (`evaluate_rule`), `normalize.py`, `checks.py` (the rule-set check:
  `static_problems`, `check_units`, `assemble_check`, `FIXTURE_CODES`; the worker answers its units),
  `protocol.py` (`LimitsWire`, `EvalUnit`, `CheckUnit`, `EvaluateJob`, `CheckJob`, `UnitResult`,
  start, done and end markers: the JSON-line wire between the sandbox and the worker) and
  `worker.py` (`serve`, `main`, the compile cache and the `RLIMIT_AS` limit).

## Tests

- Unit (no Docker): `uv run pytest -m unit shared`
- Integration (needs Docker): `uv run pytest -m integration shared`
- `shared/dc-core/tests/unit/test_route_policies.py` proves the walker fails on an undeclared
  route (a deliberately unguarded fixture app).
- The shared pytest plugin `dc_core.testing` also holds `release_library_caches()`: the root
  `conftest.py` runs it after every test, so FastAPI's and SQLAlchemy's process-wide caches let go
  of the test's apps, engines and dialects and the session's heap stays flat.
  `contracts/tests/integration/test_session_heap.py` fails when a closed stack leaves one alive.
- Whole repo gate: `bash scripts/ci.sh`.

## Platform rules that apply here

- Parsing depth cap: `dc_odcs.loader.MAX_NESTING_DEPTH` (128 nested mappings/sequences, a module
  constant) is enforced inside `load_yaml` (event-stream abort at the first over-deep collection,
  because PyYAML's scanner is super-linear in flow nesting) and `load_json` (linear string-aware
  bracket pre-scan). Over-deep input raises `NestingTooDeepError` (a `ValueError`, fixed detail)
  which every caller maps to 422 `malformed-document`. Any new code that parses user YAML/JSON must
  go through these loaders, never `yaml.load`/`json.loads` directly.
- A change to a public contract here is a change to every service: update the callers and run the
  whole suite (`bash scripts/ci.sh`).
- The route-policy mechanism (`require()` or `public()` on a `PolicyRouter`, walker test with an
  allow-list, `min_routes` and `known_permissions`) lives here; services only use it. Every
  permission string must be a key of `dc_auth.roles.PERMISSIONS`.
- One `uow.audit()` per business write with allowlisted `__audit_fields__` is enforced by the
  tripwire in `dc_core.uow`.
- Problems are RFC 9457 with `urn:dc:problem:<slug>` types listed in
  `docs/user/api-guide.md` ("Errors: RFC 9457 problem details").
- Migrations belong to services; the shared helpers only provide the common table shapes
  (`audit_columns`, `outbox_table`) and the CLI.

## Skills

- `.claude/skills/shared-mechanisms/SKILL.md`: the public contracts of dc-core, dc-auth (roles,
  oracle, scopes) and dc-odcs and how to change them.
- `.claude/skills/rules-engine/SKILL.md`: rule fields, bindings, the `cel-1` surface, authoring
  guards, error codes and the dc-rules import boundary.
- Repo-level skills (run-stack, add-route, add-migration) live in the root `.claude/skills/`.
