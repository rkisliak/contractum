# validation (package `dc_validation`, workspace member `dc-validation`)

Reusable, versioned validation rules and the checks that gate publishing. Policy Managers author
declarative rule sets (an editable draft plus immutable numbered versions) and make them mandatory
down the hierarchy; the service runs every rule out of process in a bounded sandbox and answers the
publish gate, on-demand validation and the rule-set check. The app is fail-closed: it never answers
"valid" without a verdict.

## What this module owns

- The validation service process, its database `validation` (role `validation`) and version table
  `alembic_version_validation`, and its outbox.
- Rule sets and their versions (`rule_set`, `rule_set_version`), the mandatory flag and
  `enforce_at` per version, the immutable validation runs (`validation_run`) and the platform rule
  set `odcs-structural` (plus the semantic and publication platform sets).
- The rule sandbox (`sandbox.py`): worker subprocesses that evaluate rules, with a per-rule
  deadline, a per-run budget, a memory limit and bounded concurrency.
- The validation run pipeline and the one report shape (`runs.py`, `report.py`), behind the same
  platform rules as every other service.

## What it must never import

Services import only the `dc_*` libraries (`dc_core`, `dc_auth`, `dc_odcs`, `dc_rules`). This
module must not import `dc_identity`, `dc_contracts` or `dc_audit`; it asks identity for decisions
through `RemoteAuthorizer` and receives contracts over HTTP (in fact a run is a pure function of the
request bytes: validation never calls contracts). The `dc_*` libraries never import services.

**The web process never imports the engine.** `dc_validation` may import `dc_rules.model`,
`dc_rules.ports`, `dc_rules.protocol` and `dc_rules.checks`, but never
`dc_rules.cel_engine`, `dc_rules.targets`, `dc_rules.evaluator` or `dc_rules.worker`. Only the
worker subprocess (`python -m dc_rules.worker`, started by `sandbox.py`) loads the CEL engine,
because evaluation cannot be cancelled and its memory is unbounded. The import-linter contract
"The validation web process never loads the rule engine" enforces it, and a subprocess test checks
that the app's `sys.modules` never holds the engine. `uv run lint-imports` enforces both.

## Key files and entry points

- `src/dc_validation/main.py`: `create_validation_app`
  (`uvicorn --factory dc_validation.main:create_validation_app`; `http_client=` is a test seam).
- `src/dc_validation/api/rulesets.py` (Plan 05-07): the authoring routes under
  `/api/v1/validation` and their DTOs (`RuleSetOut`, `RuleSetDraftOut`, `RuleSetVersionOut`). The
  draft PUT has no Pydantic body parameter: it reads `max_draft_bytes` after the policy passed and
  parses JSON or YAML with the dc_odcs strict loaders. Publish takes no unit of work; it calls
  `publish_rule_set` with the app's session factory and sandbox. Plan 05-08 adds the per-rule
  `PUT|DELETE .../draft/rules/{rule_key}` (path key wins), author lists, deprecate and delete, and
  `GET /folders/{folder_id}/available-rule-sets...`: `ruleset:read-published` is checked at the
  caller's own folder and the result is limited to sets held by that folder's `ancestor_ids` or the
  platform scope (never compute descendants; drafts never appear).
- `src/dc_validation/models.py` (`RuleSet`, `RuleSetVersion`, `ValidationRun`, outbox model),
  `migrate.py`, `migrations/` (0001 baseline, 0002 rule sets, 0003 mandatory, 0004 validation runs,
  0005 platform seed, 0006 semantic platform sets, 0007 pause, 0008 folder purge guard, 0009
  publication platform set, 0010 publication set version 2).
- `src/dc_validation/sandbox.py` (Plans 05-03, 05-04): `Sandbox`, `sandbox_hook` (the lifespan
  hook), `SandboxBusy` (503 `validation-unavailable`, `Retry-After: 1`) and `SandboxUnavailable`.
  Workers spawn lazily, speak JSON lines (`dc_rules.protocol`), are recycled after
  `worker_max_jobs` jobs and run under `RLIMIT_AS` (`worker_memory_bytes`, Linux). The parent kills
  a worker that burns `rule_timeout_ms` of user CPU time without answering (read from
  `/proc/<pid>/stat`; a worker that is only starved or stalled is waited for up to a hard wall cap
  of 8 x the timeout; without `/proc` the deadline is wall-clock), marks that unit `timeout` and
  resends the rest. A worker that fails before it starts any unit is retried once on a fresh
  worker instead of blaming rule 1, and the wait for a worker shares the `queue_wait_s` budget with
  the run slot (503 on expiry). A spent `run_budget_s` marks every unevaluated unit
  `run_budget_exceeded`: a rule is never skipped.
- `src/dc_validation/report.py` (Plan 05-10): the report model and builder; every text in it is a
  rule author's static message or a fixed text per error code or structural keyword, never a
  document value. Changing the report shape means changing `docs/user/api-guide.md` ("Validation
  reports") and `docs/dev/api-design-decisions.md` ("Validation runs and reports"); contracts passes
  the report through as JSON.
- `src/dc_validation/settings.py`: `ValidationSettings` (`DC_` prefix, one field per variable).
  Sandbox: `DC_EVAL_WORKERS` (2), `DC_MAX_CONCURRENT_RUNS` (4), `DC_QUEUE_WAIT_S` (2),
  `DC_RULE_TIMEOUT_MS` (500), `DC_RUN_BUDGET_S` (5), `DC_CHECK_BUDGET_S` (15),
  `DC_WORKER_MEMORY_BYTES` (1 GiB, pinned in `compose.yaml`), `DC_WORKER_MAX_JOBS` (200), `DC_WORKER_READY_TIMEOUT_S` (5).
  Batch pool (D-161, WR-03): `DC_BATCH_SLOTS` (unset: half of `DC_MAX_CONCURRENT_RUNS` capped at
  `DC_EVAL_WORKERS - 1`, at least 1; refused at start-up when it is not below `DC_EVAL_WORKERS`
  (more than one worker) or `DC_MAX_CONCURRENT_RUNS`, so a worker is always left for runs; with a
  single worker, a development setting, the worker is shared), `DC_BATCH_PAGE_BUDGET_S` (25; a page
  stops before its next document once the budget is spent or the caller disconnected, 503; keep
  it below contracts' `DC_VALIDATION_IMPACT_TIMEOUT_S`),
  page caps `DC_MAX_BATCH_ITEMS` (25), `DC_MAX_BATCH_BYTES` (4 MiB decoded), `DC_MAX_BATCH_FOLDERS` (20).
  Reference facts (Plan 06-07): `DC_MAX_REFS_PER_KIND` (200 references resolved per kind, the rest
  `unverifiable`), `DC_PRINCIPAL_CACHE_TTL_S` (45; identity's definite answers per caller).
  Governance: `DC_MANDATORY_WARN_DAYS` (14; `0` enforces at once; negative is refused at start-up;
  also read by `validation-migrate`, because migrations 0006, 0009 and 0010 stamp the seeded sets'
  `enforce_at` with it, so set it on both or on neither).
  Sizes: `DC_MAX_DRAFT_BYTES`, `DC_MAX_DOCUMENT_BYTES`, `DC_MAX_FINDINGS`, `DC_MAX_ATTACHMENTS`,
  `DC_MAX_ADHOC_RULE_SETS`. Rule limits are the `dc_rules.ports.Limits` fields under the same names
  (`max_expression_bytes`, `max_target_chars`, `max_rules_per_set`, `max_compiled_bytes`,
  `max_matches`, `max_matches_with_contract`, `max_fixtures_per_kind`, `max_fixture_bytes`);
  `ValidationSettings.limits()` builds them.
- `src/dc_validation/rulesets.py` (Plan 05-06): the rule-set authoring service. `create_rule_set`
  and `save_draft` run inside the caller's `UnitOfWork`; `publish_rule_set` checks the draft in the
  sandbox with no session or lock held, then locks the row and publishes only if the draft is still
  the checked content (If-Match plus content hash, 412 otherwise). Tables `rule_set` (folder or
  platform `scope`) and `rule_set_version` (migration 0002; content columns immutable by trigger,
  no delete). Audit actions `ruleset.created`, `ruleset.draft_updated`, `ruleset.version_published`,
  `ruleset.version_deprecated` and `ruleset.deleted` carry rule keys and content hashes only.
  Every draft write (whole draft or one rule) goes through `_store_draft`.
- `src/dc_validation/resolution.py` (Plan 05-09): the pure mandatory resolution with an injected
  clock (`resolve_mandatory`, `restamp_on_set`, `enforced_version_no`); it never reads the clock or
  the database. `rulesets.py` adds `db_now`, `load_mandatory`, `set_mandatory` and `enforce_now`
  around it (migration 0003: `rule_set.mandatory*`, `rule_set_version.enforce_at`). Mode is computed
  from the database clock, `enforce_at` moves either way through `enforce-at` (`enforce-now` only
  earlier), clear records the enforced version and set restores it (D-142). `set_paused` pauses or
  resumes a platform set (an overlay in resolution: same versions, all non-blocking `warn`,
  `paused: true`; `enforce_at` untouched) and `reschedule_enforcement` moves a version's date to any
  timestamp (a past date becomes the database now); both are quick 261118-vpa and reverse D-120's
  earlier-only rule and D-130/D-150. Audit actions `ruleset.mandatory_set`,
  `ruleset.mandatory_cleared`, `ruleset.enforced_early`, `ruleset.paused`, `ruleset.resumed` and
  `ruleset.enforcement_rescheduled`.
- `src/dc_validation/runs.py`, `ingest.py`, `api/runs.py` (Plan 05-11): the validation run.
  `POST /internal/v1/validation/folders/{folder_id}/runs` (internal, never in OpenAPI) takes the
  caller's forwarded bearer, checks `contract:validate` itself and reads the capped body after the
  policy. `execute_run` re-parses the bytes (`ingest.parse_document`; problem details are fixed per
  slug, never the `dc_odcs` text), resolves mandatory sets (`resolve_mandatory`) and attachments
  (`resolve_explicit`, unresolvable ones become blocking findings), evaluates with no session open,
  builds the Plan 05-10 report and stores publish runs in `validation_run`. It never calls contracts.
  `runs.rule_ctx(facts, operation)` builds the rule `ctx`: `folder_kind`, `folder_path`,
  `organization_id` and **`operation`** (`save` or `publish`, quick 261119-rpi), always the run's
  server-side mode and never a caller value (the public validate route is always `save`). Only cel-2
  units see `ctx.operation`; the worker strips it for cel-1.
- Stored-run read (Plan 06-10, D-155): `GET /internal/v1/validation/runs/{run_id}?contract_id=`
  (internal) serves `runs.read_publishable_run` to the contracts report fetch. The target resolver
  `runs.run_folder` returns the run's stored folder, where `contract:read-published` is checked with
  the caller's forwarded token; another contract id, a non-publishable run or an unknown id is the
  uniform 404 (a run id is never an IDOR). Validation cannot tell which publishable runs became
  versions: a passed run whose publish contracts refused at the lock stays readable by id (OBS-1,
  informational; contracts only ever follows `version.validation_run_id`).
- Rule impact (Plan 06-11, RULE-10, RULE-11): `batch.py` and `api/impact.py`, two internal routes
  contracts calls with the caller's forwarded bearer (Plan 06-12 pages the documents).
  `POST /internal/v1/validation/rule-sets/{rule_set_id}/dry-run` (`ruleset:dry-run` at the rule
  set's folder; a draft also needs `ruleset:read`, refused through
  `dc_auth.deps.recorded_refusal` so the missing `ruleset:read` is a recorded denial, WR-01)
  evaluates one version as if enforced;
  `POST /internal/v1/validation/folders/{folder_id}/drift` (`governance:drift-read`) evaluates each
  document against its own folder's current mandatory resolution (`drifted`, `at_risk`,
  `compliant`). Every distinct item folder gets a PDP check with the route's permission and must lie
  in the scope subtree (else the uniform 404); caps are checked before anything else; a page holds
  `Sandbox.batch_slot()` and a run slot only per document, and checks its `PageClock` (budget and
  disconnect) before each document. Results are value-free (rule keys, fixed texts, at most 20
  pointers per rule; contracts forwards pointers only to `contract:read` holders); nothing is
  stored, audited or notified here (contracts audits each page). Batch binds `ctx.operation =
  'publish'` (`rule_ctx(facts, "publish")`): it judges stored published versions as the gate would.
- cel-2 adoption (Plan 06-05): `_units` stamps each `EvalUnit.dsl_version` from the resolved
  version, so a published cel-1 version still compiles in the cel-1 environment and a cel-2 one in
  cel-2 within one run. `Sandbox.evaluate(..., previous=, changes=, refs=)` puts those bindings on
  the job only when a unit is cel-2; `previous`, `changes` and `refs` come from the baseline (Plan 06-06) and the reference facts
  (Plan 06-07). New drafts always publish as cel-2.
  `rulesets.fixture_changes` is the injected `changes_for` (`dc_odcs.diff.compare(...).as_binding()`).
- Compatibility (Plan 06-06): the run request takes an optional `baseline` (`ValidationBaselineIn`;
  its display-only `semver` is optional and capped at `dc_odcs.MAX_VERSION_CHARS`, the same value
  contracts uses, never a smaller literal: a cap below what contracts publishes is a permanent 503
  for that contract, CR-02) and `stable_major_floor`; `execute_run` parses the baseline like the document, diffs it with
  `dc_odcs.diff.compare` in a thread and `compat.build_compatibility` returns the report's
  `compatibility` section plus the **native** findings (`source: "native"`, blocking, no rule set):
  `semver-breaking-change` (breaking change without a major above the floor) and, publish mode
  only, `compatibility-unavailable` (unusable baseline: fail closed). Nothing can disable them.
  The on-demand route also takes `application/vnd.dc.validate-pair+json` (floor derived from the
  previous document). `previous`/`changes` of cel-2 rules come from the same diff.
- Reference facts (Plan 06-07): `facts.build_refs` assembles the cel-2 `refs` binding once per run
  (`dc_odcs.element_refs` for elements and servers, the request's `ref_facts` for contract links,
  `authorizer.resolve_principals` plus a per-process `PrincipalCache` for people and teams; the
  routes pass `request.app.state.authorizer_factory(request)` so identity sees the caller's token).
  Tri-state values, `max_refs_per_kind` cap, `unverifiable` never fails a rule; the stored report
  gets counts only (`report.ref_facts`). Validation still never calls contracts.
- `src/dc_validation/api/runs.py` public `router` (Plan 05-12, VAL-08): `POST
  /api/v1/validation/folders/{folder_id}/validate` takes a raw YAML or JSON body (no Pydantic body
  parameter; `read_body_capped` at `max_document_bytes` after the policy), runs `execute_run` in
  save mode (nothing stored) and answers `ValidationOnDemandOut`. `?rule_set=<id>[@<n>]`
  (`parse_adhoc`, at most `max_adhoc_rule_sets`) become `adhoc=` attachments; an ad-hoc set that
  does not resolve is 422 `rule-set-not-attachable` raised inside `execute_run` before any sandbox
  work (`runs.not_attachable` is reused by the attach route).
- `src/dc_validation/seed.py` and `migrations/versions/0005_seed_platform_rule_set.py` (Plan 05-13,
  VAL-01): the platform rule set `odcs-structural` v1 (four guarded CEL rules with fixtures), fixed
  ids, actor `system:validation-seed`. The migration inserts it idempotently (`seed_statements()`,
  `ON CONFLICT DO NOTHING`), mandatory with `enforce_at = now()`, plus one outbox event; the
  downgrade lifts the version delete trigger for itself only. Never edit `ODCS_STRUCTURAL_V1` (a
  unit test pins its hash): a change is a new constant and a new migration. Integration tests start
  without the seed (`drop_platform_seed` in `tests/integration/conftest.py`); pass
  `make_stack(platform_seed=True)` to keep it.
- Semantic platform sets (Plan 06-09, VAL-04, VAL-05): `seed.ODCS_CONSISTENCY_V1` and
  `ODCS_REFERENCES_V1` (cel-2, mandatory, platform scope) are inserted by migration
  `0006_seed_semantic_rule_sets` with `enforce_at = now() + DC_MANDATORY_WARN_DAYS` (read from
  `ValidationSettings` at migration time, so set it on `validation-migrate` too), i.e. in warn
  grace; never edit the constants (hashes pinned in `test_seed_rules.py`). The ROOT routes under
  `/folders/{root_folder_id}/platform-rule-sets/{rule_set_id}` (`ruleset:mandate` at ROOT; any other
  folder or non-platform set is the uniform 404) are `POST .../versions/{n}/enforce-now` (the audited
  `enforce_now`), `PUT .../versions/{n}/enforce-at` (`reschedule_enforcement`) and `PUT .../paused`
  (`set_paused`); they are the only way to change a platform set's schedule. The server-reference
  rule checks the `customProperties` `server` convention of Plan 06-07.
- Publication platform set (quick 261119-rpi): `seed.ODCS_PUBLICATION_V1` (`odcs-publication`, cel-2,
  mandatory, platform scope, migration `0009_seed_publication_rule_set`, `enforce_at = now() +
  DC_MANDATORY_WARN_DAYS`, so set that on `validation-migrate` too) holds one **warning** rule,
  `published-status-active`: it fires only when `ctx.operation == 'publish'` and the document's ODCS
  `status` is not the string `active` (Latest and Preview publishes alike; a draft save, on-demand
  validation and the draft validate route never show it). A warning-severity finding never blocks
  (D-124), in warn grace and after `enforce_at` alike, so enforcing it only changes the finding's
  mode; ROOT pause and reschedule apply as for the other platform sets. The message is static text.
  Never edit the constant (hash pinned in `test_seed_rules.py`).
  Version 2 (quick 261128-rsa): `seed.ODCS_PUBLICATION_V2`, migration `0010_publication_rule_set_v2`,
  rolled out like any new version of a mandatory set (`publish_counter` 2, the draft becomes the V2
  content, `enforce_at = now() + DC_MANDATORY_WARN_DAYS`, one `ruleset.version_published` event; the
  set description is updated too). It adds the **error** rule `released-status-active`: at publish, a
  released version (no prerelease part) needs ODCS `status: active`, a missing status included.
  `published-status-active` stays a warning and judges only prerelease (Preview) publishes. Both read
  the document's own ODCS `version`, which is the SemVer contracts publishes (it gates the exact
  stored bytes and re-checks their hash under the lock), so no ctx field was added; only a string
  shaped like a SemVer prerelease (`^[0-9]+[.][0-9]+[.][0-9]+-`) counts as a Preview, anything else
  is judged as a release (fail closed). In grace v1 and v2 both report (non-blocking `warn`); once v2
  is enforced v1 drops out and the error blocks. Never edit the constant (hash pinned in
  `test_seed_rules.py`).

- Folder usage and purge (quick 261119-0v1; `folder_purge.py`, `api/folder_usage.py`): identity calls
  `GET /internal/v1/validation/folders/{id}/usage` (counts only: mandatory folder rule sets are in use and
  block a folder delete, the others are unused) and `POST .../purge` with the caller's forwarded bearer
  (`folder:delete`, Global admin only; internal). The purge removes the unused sets and all their versions
  under the transaction-local `dc.folder_purge` setting that migration 0008 teaches the version delete
  trigger (honoured only for non-mandatory folder sets); 409 while a mandatory set is left; one counts-only
  `folder.purged` event. `validation_run` rows stay.
- Grace period at runtime (quick 261120-0ye, D-34): `runtime_settings.py` (`effective_warn_days`, `settings_client_hook`) reads the
  stored `governance.mandatory_warn_days` with the caller's bearer (30 s cache, last good value, else `DC_MANDATORY_WARN_DAYS`) at
  the two runtime call sites (`PUT /rule-sets/{id}/mandatory` and publishing a version of a mandatory set, `publish_rule_set(...,
  warn_days=)`). Migrations 0006, 0009 and 0010 keep reading the env value. About reports the env default.

## Tests

- Unit (no Docker): `uv run pytest -m unit validation` (the report, resolution, sandbox with a fake
  worker, seed rules and ad-hoc parameter tests).
- Integration (needs Docker): `uv run pytest -m integration validation`
- Hostile rules: `validation/tests/integration/test_hostile_rules.py` (hostile published versions
  bounded and failing closed through the real routes, an exhausted run budget never skips a rule),
  `shared/dc-rules/tests/unit/test_hostile_engine.py` (engine level) and, on the Linux stack,
  `bash scripts/e2e.sh tests/e2e/test_phase5_hostile_rules.py` (a memory bomb is refused with
  `memory_exceeded`, never `timeout`).
- The Phase 5 goal journey through the gateway: `bash scripts/e2e.sh
  tests/e2e/test_phase5_rules_journey.py`.
- The Phase 6 journeys through the gateway: `bash scripts/e2e.sh
  tests/e2e/test_phase6_semantic_journey.py tests/e2e/test_phase6_rule_impact_journey.py` (a
  breaking change refused with `semver-breaking-change` until a major bump, the seeded semantic
  sets warning in grace, a dry-run and a drift report).
- Classifier and corpus: `shared/dc-odcs/tests/unit` (the golden corpus and the rule-table
  completeness gate); the datacontract-cli oracle is opt-in and never part of the gate.
- Route policy walker: `validation/tests/unit/test_route_policies.py` (an exact `EXPECTED` table of
  method, path and permission; `min_routes` is its size).
- Integration stack: `make_stack()` in `validation/tests/integration/conftest.py` runs identity and
  validation in one process (modules need `pytest.mark.asyncio(loop_scope="session")`).
- Whole repo gate: `bash scripts/ci.sh`.

## Platform rules that apply here

- Every route is declared with `require(...)` or `public()` on a `PolicyRouter` and listed in the
  walker test. Authorization goes through identity's PDP (`remote_authorizer_factory`); failure to
  get a decision answers 503, never allow.
- One `uow.audit(...)` per business write with allowlisted `__audit_fields__`.
- Errors are RFC 9457 problems with `urn:dc:problem:<slug>` types listed in
  `docs/user/api-guide.md` ("Errors: RFC 9457 problem details").
- Own migrations and version table; never touch another service's database.
- Rules are declarative data only; no user-supplied code is ever executed (project constraint).

## Skills

- `.claude/skills/validation-service/SKILL.md`: how to add rule-set routes, change the report, add a limit and evolve the platform rule set.
- `shared/.claude/skills/rules-engine/SKILL.md`: rule fields, bindings and the `cel-1` surface.
- Repo-level skills (run-stack, add-route, add-migration) live in the root `.claude/skills/`.
