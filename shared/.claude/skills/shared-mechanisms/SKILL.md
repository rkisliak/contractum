---
name: shared-mechanisms
description: Understand and safely change the public contracts of dc-core, dc-auth and dc-odcs that every service depends on (app factory, policies, problems, unit of work, auth, ODCS ingest).
---

# shared-mechanisms

Use before changing anything under `shared/`.

1. **dc-core public contracts**
   - `create_app(service=, settings=, routers=, internal_routers=, session_factory=, outbox_model=,
     lifespan_hooks=)` builds every service with the same pipeline: `CorrelationIdMiddleware`
     outermost, then `EncodedPathGuard` (400 `invalid-path` for raw `%2F`, `%5C`, `%00`, `%3B`),
     problem handlers, routers, internal routers (excluded from OpenAPI), `/healthz`, `/readyz`,
     and the outbox relay when NATS and an outbox model are configured.
   - `dc_core.openapi.build_openapi(app, service=)` is installed by `create_app` as a cached
     `app.openapi` (PLAT-06, D-179). Per operation it defaults `tags` to the service name, sets
     `security` from `find_policy(route.dependant)` (`[]` for `public()`, `[{"bearerAuth": []}]`
     otherwise), adds the common statuses (400, 429; 401, 403, 503 when guarded; 404 with a
     path parameter; 422 with parameters or a body; 413 with a body) and rewrites every 4xx/5xx
     to `application/problem+json` with `$ref` `Problem`, keeping the route's description; it
     removes FastAPI's `HTTPValidationError`/`ValidationError`. `PROBLEM_SCHEMA` and
     `BEARER_SCHEME` are module data and must stay byte-identical across services, so the
     gateway merge de-duplicates them (D-177): any change to them is a change to every service
     document and to the merged one. `tests/repo/test_openapi_completeness.py` (fixture
     `service_apps` in `tests/repo/conftest.py`) checks every service document.
   - `dc_core.nul` (Phase 7 post-fix): `PolicyRouter`'s default route class is `NulGuardRoute`,
     which answers 422 `validation-error` (`errors[{pointer, detail}]`, never the value) for a NUL
     (U+0000) or a lone UTF-16 surrogate (U+D800 to U+DFFF, e.g. JSON `"\ud800"`) in any query
     parameter or in a JSON body FastAPI parses, before authentication and the database
     (PostgreSQL text and jsonb cannot hold a NUL; psycopg cannot UTF-8-encode a surrogate). The
     `detail` is `NUL_DETAIL` or `SURROGATE_DETAIL`. The body is walked only when its raw bytes
     hold `\u0000`, a `\ud8`..`\udf` escape (any case) or the bytes ED A0..BF (`json.loads`
     decodes bytes with `surrogatepass`), or are not UTF-8 (UTF-16/32 JSON is always walked).
     Raw-body routes call `find_unstorable` after their own parse (`found = find_unstorable(data)`
     then `nul_problem(found[0], slug="malformed-document", detail=found[1])`); `find_nul` is the
     pointer-only form. `NoNulStr` (`reject_nul`) refuses both, for DTOs validated by hand. The
     names keep `nul`; every helper covers both cases. Contract ingest refuses both in the
     `dc_odcs` gate (`malformed-document`).
   - `PolicyRouter` refuses a route without `require()` or `public()` at definition time;
     `assert_all_routes_have_policy` is the walker backstop and refuses a vacuous walk
     (`min_routes`). `assert_all_routes_have_policy(known_permissions=frozenset(PERMISSIONS))` also fails
     on a `require()` permission string that is not a registered key.
   - `ProblemError(status, slug, title, detail, **extensions)` renders
     `application/problem+json` with `type` `urn:dc:problem:<slug>`; 422 bodies carry
     `errors[{pointer, detail}]` and never echo input.
   - `UnitOfWork` (`audit()`, `commit()`) plus `AuditColumnsMixin` and `OutboxMixin`: audit
     columns come from the actor, snapshots from `__audit_fields__`, and the tripwire raises on an
     audited write without an event.
   - `EventPublisher`, `build_envelope`, `subject_for`, `OutboxRelay`, `migration_cli`.
   - Access events (Phase 7, AUD-04, D-171, D-185): `dc_core.events.build_access_envelope(*,
     service, aggregate_type, aggregate_id, action, actor, scope_path, request_id, detail)` builds a
     JSON-safe envelope (UUIDv7 `event_id`, UTC `occurred_at`, `category` `access`, `before` and
     `after` always None, `schema_version` 1); `detail` must already be flat and allow-listed by the
     producer (the audit ingest caps it: at most 16 keys `^[a-z_]{1,32}$`, strings at most 512
     characters, and refuses content). Subjects are `subject_for(service, aggregate_type, action,
     prefix="access")`, so `access.<service>.<aggregate_type>.<action>` on stream `ACCESS`; the
     service's NATS user needs `access.<service>.>` in `infra/nats/nats.conf.tmpl` (pinned by
     `tests/repo/test_nats_permissions.py`). `dc_core.access.AccessLog(publisher, *,
     capacity=10_000, batch=100, flush_s=1.0, publish_timeout_s=10.0, max_backoff_s=30.0)`: a route
     calls only the synchronous `record(subject, envelope)`, which is O(1), never awaits and never
     raises; a `deque(maxlen)` drops the oldest when full. `dropped` and `dropped_by_reason`
     (`queue_full`, `publish_failed`, `invalid`, `shutdown`) count losses, with a warning
     `access_events_dropped` on the first drop and every 1000th. `run()` publishes batches with
     `msg_id` = `event_id`; a failed or timed-out batch is dropped (never re-queued) and the loop
     backs off. `flush(timeout_s)` returns within its deadline even with a hanging publisher.
     `access_log_hook(settings)` sets `app.state.access_log` (None without `nats_url`) over a
     `JetStreamPublisher(..., stream=ACCESS_STREAM)`, flushes best-effort on shutdown and closes
     the publisher. `dc_core.access` imports no NATS module and no other `dc_*` library, so the
     actor dict (`dc_auth.principal_to_actor`) is built by the producer service
     (`dc_contracts.access_events.record_content_read`). Never await I/O on the read path.
   - `dc_core.conditional` (`parse_etag_list`, `if_match_ok`, `precondition_required`,
     `require_if_match`: strong `If-Match` evaluation, 428 when absent, 412 when stale, never
     echoing the header) and `dc_core.body` (`read_body_capped(request, max_bytes, *, detail=None)`:
     413 `payload-too-large` on `Content-Length` and again on the stream). Services reuse them;
     never import another service's helper. `dc_contracts.http_cache` and `dc_contracts.ingest`
     re-export the same names for their existing callers. The mail service keeps its own reader
     (different 422 semantics).
2. **dc-auth public contracts**
   - `IssuerRegistry`/`IssuerConfig` (issuer, JWKS URI, audiences, algorithms, namespace,
     `kind`, `tenant_id`, required claims) and `TokenVerifier` produce a `Principal(id, type, ...)`; algorithm allow-list, required claims
     and a generic 401 for every failure. Claims never grant authority. A generic `oidc` issuer
     yields `<namespace>:<sub>`; the `entra` kind (strict v2 mapper, one issuer entry per tenant)
     yields `entra:<tenant guid>:<oid>` and its type (`user` or `service_principal`) comes from
     `idtyp`/`scp`, never from a `ptype`, `roles` or `groups` claim. The `entra` and `sa`
     namespaces are reserved: no generic issuer may mint them, and each tenant may appear in only
     one Entra entry (`ENTRA_PREFIX` makes every Entra id start with `entra:` whatever the entry's
     namespace is). Identity's
     `dc_identity.principal_ids.parse_principal_id` is the one parser of `local:`, `entra:` and
     `sa:` ids (canonical lowercase only); identity's `PrincipalGate` admits Entra principals by
     registration before any decision.
   - `dc_auth.roles` is the Phase 2 authorization model as frozen data: `Role`, `ROLE_LEVELS`
     (folder kinds a role may be granted at), `PERMISSIONS` (the registry, each a `PermissionSpec`
     with `target`, `access` (required, `read` or `write`), `evaluated_at`, `visible_via` and
     `interactive_only`), `READ_PERMISSIONS` (derived from `access`; all a read-only PAT holds),
     `ROLE_PERMISSIONS` and the `GRANTABLE` ceiling. A permission cannot be registered without its
     `access`, and the golden sets in `shared/dc-auth/tests/unit/test_roles_matrix.py` pin the
     `interactive_only` ones. No code branches on a role name; a model change is a diff to this file.
     `dc_auth.oracle` is the brute-force reference implementation (pure Python) that identity's SQL
     evaluator and the Hypothesis tests are proven against; keep it a literal reading of the rules.
   - Phase 5 adds `ruleset:read`, `ruleset:read-published`, `ruleset:create`, `ruleset:update`,
     `ruleset:publish`, `ruleset:deprecate`, `ruleset:mandate` and `contract:validate` (read-class). The
     constant `_FOLDER_READ` pairs `folder:read` with `ruleset:read-published` so no role gets one
     without the other; use it wherever a role lists `folder:read`.
   - `Decision.permissions` is the set of permissions the caller holds at the target folder
     (empty unless allowed); handlers use it for redaction (D-36). `FolderScope` is
     `all` minus `excluded_subtrees` plus exact `folder_ids`; `covers(folder_id, ancestor_ids)`
     answers whether a folder is visible (ancestor ids root-first, including the folder itself).
   - `Authorizer` protocol (`check`, `accessible_folders`), `RemoteAuthorizer` (forwards the
     caller's bearer token to identity). Identity's 401 and its 403 `principal-not-registered` and
     `interactive-session-required` answers pass through unchanged (`authz_wire.passthrough_problem`,
     `PASSTHROUGH_403_SLUGS`); only transport errors, timeouts and any other unexpected answer
     become 503, and nothing ever becomes ALLOW. `remote_authorizer_factory` builds each
     `RemoteAuthorizer` with `check_context(request)` (`app.state.service` and the matched route
     template, `unknown` when absent), sent as `service`/`route` in the check body so identity can
     record who asked on a denial (D-173); every call also forwards the caller's `X-Request-ID`
     (the correlation id) so the denial row carries the originating request id. `AuthzCheckIn`
     turns a malformed `service`/`route` into None, never a 422. `require(permission, target=,
     allow_restricted=)`, `unit_of_work`, `auth_lifespan`.
   - `dc_auth.opaque` resolves opaque `dcp_` bearer tokens (PATs and service-account keys):
     `parse_dcp`, `OPAQUE_ISSUER` and `RemoteOpaqueResolver`, which asks identity's
     `/internal/v1/authz/principal` on every request (never cached). `auth_lifespan(...,
     opaque_resolver=)` defaults to that remote resolver; identity itself injects its local one.
     `Principal` carries `via` (`jwt`, `pat`, `api_key` or `system`), `username_hint` (display
     only) and `issued_at` (the verified iat, used only as a session cutoff).
   - `dc_core.logging` redacts `dcp_` token values (`DCP_TOKEN_RE`) and `whsec_` webhook secrets
     (`WHSEC_RE`) wherever they appear in a log record, besides credential-looking keys.
   - `dc_auth.testing.FakeIssuer` mints expired, wrong-audience, wrong-issuer and forged tokens
     for unit tests; the root `conftest.py` loads it with `dc_core.testing`.
3. **dc-odcs public contracts** (`dc_odcs.__all__`): `parse_contract`, `ParsedContract`,
   `ContractRejected`, `validate_structure`, `StructuralReport`, `extract_search_fields`,
   `load_yaml`, `load_json`, `SUPPORTED_API_VERSIONS`, `ALLOWED_MEDIA_TYPES`, `MAX_ODCS_ID_LENGTH`
   (255, the one bound on an ODCS id: the gate and every contracts API model use it, quick
   261120-lbz). Schemas are vendored
   per version under `schemas/` with `MANIFEST.json`; never fetch a "latest" schema.
   **`dc_odcs.diff` (Phase 6)** is the one breaking-change classifier; contracts and validation
   both import it, never a second implementation. Public names: `compare(before, after, *,
   max_changes)` -> `DiffResult` (`changes`, `counts`, `changes_total`, `truncated`,
   `classifier_version`, `as_binding()`), `Change`, `required_bump(result, *, declared,
   stable_major_floor)` -> `BumpVerdict`, `CLASSIFIER_VERSION` (`odcs-compat-1`) and
   `ALLOWED_VALUE_ATTRIBUTES`. Rules for changing it:
   - The package is stdlib-only (an acceptance grep enforces it): no `jsonschema_rs`, no
     `deepdiff`, no `jsonpatch`. Never compare documents with deepdiff or jsonpatch: they key
     arrays by index, so a reorder is noise and a rename is invisible; use `compare`.
   - Editing any row of `diff/table.py` (or the pairing in `diff/normalise.py`) changes stored
     report semantics: bump `CLASSIFIER_VERSION` in the same commit. Unmatched differences are
     `unclassified` warnings by design (D-145).
   - Pairing is tiered, never keyed per side: equal `id` (a different name is a rename), equal
     natural key (different ids are `id-changed`), equal content, then quality check fields.
     Duplicate natural keys get `name`, `name~2`, `name~3`; a removed or added subtree is one
     change at its root; the declared `version` token is excluded; the walk is iterative.
   - `Change.before/after` carry values only for `ALLOWED_VALUE_ATTRIBUTES` (strings capped at
     200 characters); descriptions, examples, quality SQL and custom property values never leave
     the classifier, so reports and notices built from it cannot leak document text.
   - Every attribute of both vendored ODCS schemas needs a decision: a row naming it, an entry in
     `table.METADATA_ONLY` (info), a structural role in the walk, or (servers) the
     `CONNECTION_WILDCARD` row. `tests/unit/test_diff_completeness.py` walks the schemas and fails
     on a new attribute; after a schema vendoring, add the row and a case to
     `tests/golden/compat/*.yaml` (the golden corpus, at least 50 cases, runs in the unit gate and
     must exercise every table code). `tests/oracle/` compares the table with datacontract-cli
     (`uv run pytest -m oracle shared/dc-odcs`, dev-only, network, never in CI or the lock); an
     intentional difference is an `allowlist.yaml` row with a reason.
   - `required_bump` enforces only breaking-needs-major above the stable major floor (D-146;
     `0.x` is not special, a floor of `None` means no stable release yet).
4. **Changing a contract.** Search for every caller (`grep -rn "<symbol>" identity contracts
   validation audit`), change callers in the same commit, keep the import direction (`dc_*`
   never import services), and run `bash scripts/ci.sh`, which also runs `lint-imports`.
5. **Adding a dependency** to a shared library changes `uv.lock`; the lock is the approved set.
   Do not add a package without review. Both Dockerfiles copy every member's `pyproject.toml`
   (`tests/repo/test_dockerfile_members.py` fails otherwise).
