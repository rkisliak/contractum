---
name: validation-service
description: Add or change routes, tables, limits, the report shape or the platform rule set of the validation service (Phase 5) inside the platform rules, keeping the web process free of the rule engine and every outcome fail-closed.
---

# validation-service

Use when adding anything to `validation/src/dc_validation/`.

1. **Start state.** `create_validation_app` passes `routers=[rulesets_router, runs_router]` and
   `internal_routers=[internal_router, impact_router]` to `dc_core.app.create_app`, wires
   `auth_lifespan` with `remote_authorizer_factory` and `sandbox_hook(settings)`, and owns an outbox
   model. The service owns `rule_set`, `rule_set_version`, `validation_run` (migrations 0002 to
   0009; 0005 seeds `odcs-structural`, 0006 the semantic sets `odcs-consistency` and
   `odcs-references`, 0009 the publication set `odcs-publication`). The gateway forwards `/api/v1/validation/` to it; the internal routes (the
   run, the stored-run read and the two batch routes) are never routed. `api/rulesets.py` is the
   model for new routes.
2. **Add a router** exactly as in the root `add-route` skill: a `PolicyRouter(prefix=
   "/api/v1/validation/...")`, every route with `require("<resource>:<action>", target=...)` or
   `public()`, included through the `routers=[...]` list in `main.py`. Real routes carry the
   `/api/v1/validation` prefix themselves; there is no gateway strip-prefix.
3. **Add the route to `EXPECTED`** (method, path, permission) in
   `validation/tests/unit/test_route_policies.py`; `min_routes` is `len(EXPECTED)` and the exact
   table test fails on any route that is missing or has another permission. Add every deliberately
   public route to `ALLOWED_PUBLIC`. A vacuous walk is a failure.
4. **Add a rule-set route.** A route addressed by `{rule_set_id}` takes
   `require("ruleset:<action>", target=rule_set_folder)` (`rulesets.rule_set_folder`): the resolver
   returns the folder that holds the set, and an unknown id and a **platform** set (no folder, so no
   permission can exist on it) both answer the uniform 404 `not-found`. That is how
   `odcs-structural` stays uneditable through the API; never add a route that bypasses the resolver
   for authoring. Folder-addressed routes use the folder from the path (the PDP supplies its kind
   and ancestry, never the body). Reads of published rules for contract authors use
   `GET /folders/{folder_id}/available-rule-sets...` with `ruleset:read-published` at the caller's own
   folder and filter by that folder's `ancestor_ids` plus the platform scope; never compute
   descendants. Writes go through `Depends(unit_of_work)` with one `uow.audit(...)` each and
   `__audit_fields__` on every audited model; a draft write goes through `_store_draft`. If a new
   permission string is needed, add it to `dc_auth.roles.PERMISSIONS` and the role matrix (one
   place) and cover it in identity's tests; never decide authority from a token claim.
5. **Change the report shape.** The report is built in `report.py` and is the contract with
   contracts, which passes it through as JSON and embeds it in a 422 problem. A new field needs
   `report.py`, the unit tests in `tests/unit/test_report.py`, `docs/user/api-guide.md`
   ("Validation reports") and `docs/dev/api-design-decisions.md` ("Validation runs and reports",
   including "Compatibility and the native findings": the docs are the contract) and a check of the contracts tests
   that read the report. Every text in a report is a rule author's static message or a fixed text
   per error code or structural keyword; never put a document value, an engine message or a
   condition into it. A new error code is added to `dc_rules.ports.ErrorCode` and `ERROR_MESSAGES`.
6. **Add a limit.** One path: a field in `ValidationSettings` (`settings.py`, named like the
   `DC_` variable) -> for rule limits the same-named field of `dc_rules.ports.Limits` and
   `ValidationSettings.limits()` -> the sandbox sends it to the worker (`LimitsWire`) and the check
   in `dc_rules.checks` enforces it. Sandbox sizing (`eval_workers`, `max_concurrent_runs`,
   `rule_timeout_ms`, `run_budget_s`, `worker_memory_bytes`, ...) lives only in settings and
   `sandbox.py`. Document the variable in `validation/CLAUDE.md` and, when it is deployed, in
   `compose.yaml`. An exceeded limit is a refusal or an `error` outcome, never a skipped rule.
7. **Keep the web process free of the engine.** `dc_validation` imports `dc_rules.model`,
   `ports`, `protocol` and `checks` only; evaluation happens in the `python -m dc_rules.worker`
   subprocess through `sandbox.py`. `uv run lint-imports` (contract "The validation web process never
   loads the rule engine") and the subprocess test in `tests/unit/test_sandbox.py` fail on an import
   of `cel_engine`, `targets`, `evaluator` or `worker`. Take a sandbox slot (`SandboxBusy` -> 503
   `validation-unavailable` with `Retry-After: 1`) for any new evaluation and hold no session or lock
   while it runs.
8. **Rules are data.** Conditions are CEL behind the `ExpressionEngine` port, targets are RFC 9535
   JSONPath; see the `rules-engine` skill. Never `eval`, never load code a user supplied. An
   evaluation error is its own outcome and blocks wherever a failure would block (fail closed).
9. **Evolve the platform rule sets.** `seed.py` holds `ODCS_STRUCTURAL_V1` (migration 0005,
   enforced from the start) and the cel-2 sets `ODCS_CONSISTENCY_V1` and `ODCS_REFERENCES_V1`
   (migration 0006, mandatory in warn grace: `enforce_at = now() + DC_MANDATORY_WARN_DAYS`, read from
   `ValidationSettings` at migration time, so the variable must be set on `validation-migrate` too
   whenever it is set for `validation`), each pinned by a unit test on its hash. Never edit them. A
   change is a new constant (`ODCS_STRUCTURAL_V2`), a new migration that inserts that version (and
   stamps `enforce_at`) and a new fixture-checked test; existing contract versions keep their pins.
   Seeded rules guard every field type, and a missing reference passes: only `not_found` fails.
   Platform sets hold no folder, so their schedule changes only through the ROOT routes
   `POST /folders/{root_folder_id}/platform-rule-sets/{rule_set_id}/versions/{n}/enforce-now`,
   `PUT .../versions/{n}/enforce-at` and `PUT .../paused` (`ruleset:mandate` at ROOT; anything else
   is the uniform 404). A paused set resolves as non-blocking `warn` with `paused: true`.
   `ODCS_PUBLICATION_V1` (migration 0009, `odcs-publication`, same warn grace) holds the publish-only
   checks: today one **warning** rule `published-status-active`, which reads `ctx.operation` (`runs.rule_ctx`
   binds the run mode; batch binds `publish`; cel-1 never sees it). **A warning-severity platform rule
   never blocks** (D-124 blocks only error-severity findings), in grace or after `enforce_at`, so it
   is the right tool for advice that must not stop a publish; a blocking check would need severity
   `error` and a deliberate decision. Its message is static and guards make it error-free.
10. **Compatibility and native findings (Phase 6).** A run request may carry a `baseline` and
   `stable_major_floor` (sent by contracts). `execute_run` parses the baseline like the document,
   diffs it with `dc_odcs.diff.compare` in a thread, and `compat.build_compatibility` builds the
   report's `compatibility` section and the **native** findings `semver-breaking-change` (a breaking
   change without a declared major above the floor) and, in publish mode only,
   `compatibility-unavailable` (an unusable baseline fails closed). Native findings have
   `source: "native"`, a null `rule_set`, are listed first and are always blocking; never add a
   request field, setting or role that skips or downgrades them. cel-2 rules read the same diff as
   `previous` and `changes`; fixture `changes` come from `rulesets.fixture_changes`.
11. **Reference facts (Phase 6).** `facts.build_refs` assembles the cel-2 `refs` binding once per run
   before any rule runs: elements and servers from the document (`dc_odcs.element_refs`), contract
   links only from the request's `ref_facts`, people and teams from identity through
   `authorizer.resolve_principals` (the caller's token) and the per-caller `PrincipalCache`. Values
   are tri-state and `unverifiable` never fails a rule; the stored report gets counts only. A new
   kind of reference needs a map here, a cap (`DC_MAX_REFS_PER_KIND`) and docs; validation still
   never calls contracts.
12. **Batch evaluation (Phase 6, rule impact).** `batch.py` and `api/impact.py` serve the internal
   dry-run (`ruleset:dry-run` at the set's folder; a draft also needs `ruleset:read`) and drift
   (`governance:drift-read`) routes. Check the caps (`DC_MAX_BATCH_ITEMS`, `DC_MAX_BATCH_BYTES`,
   `DC_MAX_BATCH_FOLDERS`) before anything else, PDP-check every distinct item folder and require it
   inside the scope subtree, hold `Sandbox.batch_slot()` (sized by `DC_BATCH_SLOTS`, always below
   `DC_EVAL_WORKERS` and the run slots: the settings validator refuses anything else) for the page
   and a run slot only per document, and call `PageClock.check()` (the `DC_BATCH_PAGE_BUDGET_S`
   budget and `request.is_disconnected`) before each document, so publishes are never starved and
   a page never outlives its caller (WR-03). Results are value-free (rule keys, fixed texts, at
   most 20 pointers per rule; contracts strips pointers for callers without `contract:read`);
   nothing is stored, audited or notified here.
13. **The stored-run read.** `GET /internal/v1/validation/runs/{run_id}?contract_id=` re-checks
   `contract:read-published` at the run's stored folder (`runs.run_folder`) with the caller's
   forwarded token; another contract id, a non-publishable run or an unknown id is the uniform 404.
14. **Calls to other services** use the `dc_core.http` client factory. Validation does not call
    contracts: a run is a pure function of the bytes and attachments it is sent.
15. **Migrations**: follow the root `add-migration` skill with `validation/alembic.ini` and
    `python -m dc_validation.migrate check`.
16. Verify: `uv run pytest -m unit validation`, `uv run pytest -m integration validation`,
    `uv run lint-imports`, then `bash scripts/ci.sh`; for sandbox or limit changes also
    `bash scripts/e2e.sh tests/e2e/test_phase5_hostile_rules.py`; for compatibility, seeded sets or
    batch routes also the Phase 6 journeys (`tests/e2e/test_phase6_semantic_journey.py`,
    `tests/e2e/test_phase6_rule_impact_journey.py`).
