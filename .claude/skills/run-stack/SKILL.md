---
name: run-stack
description: Start, smoke-test, e2e-test, reset and tear down the whole platform with docker compose, and find where the generated development secrets live.
---

# run-stack

Use whenever you need the running platform (manual checks, e2e tests, demos).

1. **Cold start, one command.** From the repo root: `docker compose up -d --build --wait
   --wait-timeout 300`. The `dev-init` one-shot generates every secret and certificate on first
   start, each `<service>-migrate` job runs `upgrade head`, then the services start and report
   healthy. Only Traefik publishes a host port: `https://localhost:8443`.
2. **Where the dev secrets live.** Inside the `dev-secrets` volume, one subdirectory per
   consumer, mounted read-only at `/run/secrets` in that consumer only. The engineer-facing
   subset is mirrored to `./.dev/` (gitignored): `ca.crt` (trust it with `curl --cacert
   .dev/ca.crt`) and `bootstrap_admin_secret`. After the first rotation `.dev/admin_password`
   also appears (mode 600). Nothing in the repository holds a secret value, and
   `tests/repo/test_no_default_secrets.py` keeps it that way.
3. **Smoke test.** `bash scripts/skeleton-smoke.sh` logs in as `admin` through the gateway,
   runs the bootstrap flow (secret login, password rotation) when no working password is stored,
   and waits until the audited rotation event appears exactly once. It prints `skeleton OK`.
4. **E2E tests on a warm stack.** `bash scripts/e2e.sh tests/e2e` (or any pytest paths and
   args, for example `bash scripts/e2e.sh tests/e2e/test_gateway.py -k cors`). It brings the
   stack up incrementally and runs the `e2e` container on the compose networks, so `https://traefik`
   (the gateway) and `http://identity:8000` (a service, bypassing the gateway) are both
   reachable. It never removes containers or volumes.
5. **Cold start plus e2e in one go.** `CI_E2E=1 bash scripts/ci.sh` tears the stack down with
   volumes, rebuilds, starts and runs the whole `tests/e2e` suite after the unit and integration
   steps. Use it once per phase, not per edit.
6. **Reset.** `docker compose down -v --remove-orphans` removes containers, networks and the
   `dev-secrets`, `pgdata` and `natsdata` volumes: the next `up` regenerates every secret and a
   fresh bootstrap secret, so an old `.dev/admin_password` no longer works (the smoke test and
   the e2e fixtures re-run the bootstrap flow automatically). Delete `./.dev/` too for a
   completely clean slate.
7. **Logs and health.** `docker compose ps`, `docker compose logs -f <service>`. The services
   log JSON; `Authorization` is scrubbed. A service reports healthy through `/healthz`
   (liveness) and `/readyz` (database reachable) inside the compose network only.
8. **Entra personas and the fake Graph (Phase 4).** The dev stack trusts an Entra-shaped mock
   issuer (`mock-oidc`, issuer `https://localhost:8443/entra`, tenant
   `11111111-2222-4333-8444-555555555555`; the one registry entry is re-pointed to the
   browser-visible issuer, and the gateway's dev-only `gateway/dynamic/dev-mock-oidc.yml` router
   serves the mock at `/entra/`, so the discovery document is
   `https://localhost:8443/entra/.well-known/openid-configuration`) and
   identity's real Graph client talks to a compose-only `fake-graph` service (backend network
   only, no published port, seed in `infra/fake-graph/directory.json`). Personas: `alice` (member of
   the `data-eng` group), `bob` (no groups), `carol` (data-eng plus 250 generated groups),
   `pipeline` (a service principal) and two bad tokens, `alice-idtoken` (no `scp`) and
   `alice-wrong-aud`. Get a persona token from inside the compose network (the e2e container, or
   `docker compose exec identity ...`) with a form `POST http://mock-oidc:8080/entra/token`,
   `grant_type=client_credentials`, `client_id=<persona>`, `client_secret=<anything>` and
   `scope=<persona>`, **and the two headers `Host: localhost:8443` and `X-Forwarded-Proto: https`**:
   the mock builds `iss` from those headers, so without them `iss` is `http://mock-oidc:8080/entra`
   and every service refuses the token. It picks the persona's claims by `scope`, so a request
   without it yields a token with no persona claims. The SPA personas `ui-alice` and `ui-bob` are
   picked by `client_id` instead (browser PKCE flow, `interactiveLogin` stays false). After
   editing `infra/mock-oidc/config.json`, run `docker compose restart mock-oidc`: the file is read
   at start, and `docker compose up -d` does not restart the container for a changed bind mount. The e2e fixtures are `entra_token("alice")` and
   `fake_graph`. The fake Graph's test mutators (`POST http://fake-graph:8080/_fake/principals/{oid}`
   with `accountEnabled`, `groups`, `deleted` or `kind`, `POST /_fake/groups/{id}` with
   `displayName`, and `POST /_fake/reset` to restore the seed) are for tests only; always call
   `/_fake/reset` afterwards, because the state is shared by every e2e test. Identity keeps its
   directory cache (60 s), so a disabled or reset principal is seen after up to a minute.
9. **Mail, Mailpit and the webhook sink (Phase 04.1).** The `mail` service (notices by email,
   authorized through the identity PDP) sends to `mailpit`, a compose-only catcher, and card
   webhooks go to `webhook-sink`, a compose-only receiver that can be told to fail the next
   deliveries. All three live on the backend network only: no published port, no gateway route,
   reachable from the e2e container as `http://mailpit:8025` and `http://webhook-sink:8080`. The
   e2e fixtures `mailpit` and `webhook_sink` empty their service before and after the test, so use
   them instead of calling the services by hand and reset after any manual use (`DELETE
   /api/v1/messages` on Mailpit, `POST /_sink/reset` on the sink); queued failures and messages
   are shared by every e2e test. Notices are **asynchronous** (outbox, NATS, the contracts
   consumer, then the delivery worker), so poll with the `wait_for` fixture and never assert right
   after the lifecycle call. Three generated secrets matter: `webhook_secret_key` (the Fernet key
   that encrypts card webhook secrets) and `mail_api_key` (the notifier service account's API key)
   are mounted only into `contracts`, and `notifier_credential` (that key's public id and hash,
   never the secret) only into `identity`. They live in the `dev-secrets` volume under the
   `contracts/` and `identity/` subpaths written by `dev-init`, and are never mirrored to `./.dev/`
   or the host. Journey: `bash scripts/e2e.sh tests/e2e/test_phase41_subscriptions_journey.py`.
10. **Validation, rule workers and the publish gate (Phase 5).** Every contract publish calls
   `validation` on the compose network (`DC_VALIDATION_URL=http://validation:8000` on `contracts`,
   never through Traefik), and every draft save asks it for an advisory report. `validation` runs
   rules in `python -m dc_rules.worker` subprocesses inside its own container (at most
   `DC_EVAL_WORKERS`, default 2, spawned lazily on the first rule), each limited to
   `DC_WORKER_MEMORY_BYTES` (1 GiB, pinned in `compose.yaml`; the limit is enforced on Linux only, so
   the memory case of the hostile suite needs this stack, not a macOS host). Do not set a container
   memory limit below that plus the service itself, or a worker will be killed instead of answering
   `memory_exceeded`. When **publish answers 503 `validation-unavailable`**: check `docker compose ps
   validation` and `docker compose logs validation` (look for `rule_worker_not_ready`, a sandbox
   that is busy, answers 503 with `Retry-After: 1`, and OOM kills), then `docker compose logs
   contracts` for `validation_unreachable`, `validation_bad_status`, `validation_bad_answer` or
   `validation_hash_mismatch`; a draft save during the same outage still succeeds and only reports
   `validation: {"status": "unavailable"}`. The platform rule set `odcs-structural` v1 is created by
   `validation-migrate` (migration 0005) and is enforced from the first publish, so a fresh stack
   needs no setup. A rule set published at ROOT and made mandatory applies to every later publish on
   the stack and published rule sets cannot be deleted: a test that governs ROOT must key its rules
   on a per-run marker and clear the mandatory flag in a `finally` (see
   `tests/e2e/test_phase5_rules_journey.py`). Journeys: `bash scripts/e2e.sh
   tests/e2e/test_phase5_rules_journey.py` and `tests/e2e/test_phase5_hostile_rules.py`.
   Rule impact couples the two services' page limits: contracts' `DC_IMPACT_PAGE_SIZE`,
   `DC_IMPACT_PAGE_MAX_FOLDERS` and `DC_IMPACT_PAGE_MAX_BYTES` must not exceed validation's
   `DC_MAX_BATCH_ITEMS`, `DC_MAX_BATCH_FOLDERS` and `DC_MAX_BATCH_BYTES`. Contracts refuses values
   above validation's defaults at start-up but cannot see a lowered validation cap, so lower both
   together; if every dry-run and drift page answers 503 `validation-unavailable`, look for a 422
   at `/items` in `docker compose logs validation`. Keep validation's `DC_BATCH_PAGE_BUDGET_S` below
   contracts' `DC_VALIDATION_IMPACT_TIMEOUT_S`.
11. **API docs (Phase 7).** One merged OpenAPI document of every service's public API is at
   `https://localhost:8443/api/v1/openapi.json` and Swagger UI at
   `https://localhost:8443/api/v1/docs/` (the dropdown also lists each service's own document).
   Both come from the `api-docs` container, which fetches the four services' documents on the
   compose network at start and merges them once; compose restarts it only when compose itself
   restarts or re-creates one of those services (`docker compose up`/`restart`), not when a
   container restarts on its own, so after any other service change run
   `docker compose restart api-docs`. While a merge collision exists (the same schema name with
   different content, a
   duplicate `operationId`), `api-docs` stays **unhealthy**, `/api/v1/openapi.json` answers 503
   problem+json and `docker compose logs api-docs` names both services; `scripts/e2e.sh` then
   waits for it and fails. `uv run pytest -m unit tests/repo/test_openapi_merge.py` reproduces the
   same merge without Docker. Fix it by renaming the model in a service.
12. **Schemathesis conformance (Phase 7).** `bash scripts/e2e.sh
   tests/e2e/test_phase7_schemathesis.py` fetches the merged document from
   `https://traefik/api/v1/openapi.json` and sends generated requests for every operation through
   the gateway as the Global admin, with the checks `not_a_server_error`,
   `status_code_conformance`, `content_type_conformance` and `response_schema_conformance`. It is
   bounded at 5 fuzzed examples per operation and 30 requests per second (below the gateway's
   `rl-default` 50/s), and takes about 3 minutes. It **excludes** `/api/v1/identity/auth/*` (a
   generated body would rotate the admin password and trip the login throttle) and
   `POST /api/v1/identity/users` (users are permanent); the reasons are in `EXCLUDED` in the
   module, and nothing else may be excluded to make it pass. Every `folder_id` path parameter and
   `parent_id` body field is pointed at a fresh per-run Organization (`p7st-<run>`), so the
   generated writes land there. A failure names the operation and the check: fix an
   undocumented status every route can produce in `dc_core/openapi.py`, and a route-specific one
   (412, 428, 406) in that route's `responses=`; never weaken a check. Schemathesis is a test-only
   dependency (dev and e2e groups), never in a service image. NATS on a warm stack: dev-init
   rewrites `nats.conf` whenever `infra/nats/nats.conf.tmpl` changes, so new permissions apply on
   the next `docker compose up`. ACCESS's age, size (`DC_ACCESS_MAX_BYTES`, 1 GiB) and discard
   limits are brought up to date in place when audit starts; any other **stream** change still
   needs `docker compose down -v`, because `ensure_stream` edits nothing else.
13. **Audit, denial and load harnesses (Phase 7).** The E2E-01 journey `bash scripts/e2e.sh
   tests/e2e/test_phase7_engineer_journey.py` (two runs, local and Entra author, about 10 s) reads
   the trail through `GET /api/v1/audit/events`; access events arrive batched (about a second) and
   every change through the outbox, so poll with `wait_for`. `test_phase7_denial_guarantee.py`
   sends `DC_E2E_DENIALS` (default 2 000) denied requests at `DC_E2E_DENIAL_CONCURRENCY` (default
   25, the identity capacity limit of G-07-11-2) on the backend network, stops NATS for the second half and restarts identity, then expects
   every request id exactly once; it restarts what it stopped in a `finally`. `test_phase7_authz_load.py`
   seeds `DC_E2E_LOAD_ORGS` (default 50) Organizations with about `DC_E2E_LOAD_GRANTS` (5 000)
   grants and sends `DC_E2E_LOAD_REQUESTS` (3 000) requests at `DC_E2E_LOAD_CONCURRENCY` (default
   25: one identity worker saturates near 45 checks per second, so 50 makes audit queries fail
   closed with 503, gap G-07-09-1); it writes `.dev/phase7-load.json` and takes about 3.5 minutes.
   Both harnesses call `http://identity:8000` and `http://audit:8000` directly, because the
   gateway's rate limit would turn the burst into 429s. Each run leaves per-run folders, grants and
   users behind: harmless, but a long-lived warm stack grows; `docker compose down -v` resets it.
14. **Operation coverage gate (Phase 7).** `tests/e2e/test_zz_operation_coverage.py` runs last and
   writes `.dev/phase7-operation-coverage.json` (covered, uncovered, refused-only, exempt and
   stale-exempt operationIds). Its gate fails only in a run that collected every
   `tests/e2e/test_phase*.py` module (`bash scripts/e2e.sh tests/e2e` or the cold gate): every
   public `operationId` must be answered with a 2xx or 3xx through the `gateway` or `internal`
   fixture in some test (a 4xx or 5xx is a refusal and never counts, WR-05), or be listed with a
   reason in `tests/e2e/operation_exemptions.json` (also an operation deliberately only ever
   refused). When it fails, read `uncovered_not_exempt` and `refused_only` in the report and add a
   successful call to a journey; a stale exemption is reported, never a failure.
15. **The Web UI and its e2e (Phase 8).** The `ui` compose service (nginx serving the built SPA,
   `infra/docker/ui.Dockerfile` target `runtime`, image `contractum/ui:dev`, no published port)
   sits behind Traefik at **`https://localhost:8443/`**; its `config.json` is written at start from the
   `DC_UI_*` variables in `compose.yaml` (public identifiers only). Sign in locally as `admin` with
   the rotated password in `.dev/admin_password` (shared by the smoke test, the Python e2e and the UI
   e2e), or with **Sign in with Microsoft** as the mock persona Alice (`ui-alice`). After a change
   under `ui/`, rebuild only that container with `docker compose up -d --build ui` (no cold restart).
   The UI e2e seeds the demo data it reads through `ui/e2e/demo-seed.ts` (non-admin personas in
   `ui/e2e/personas.ts`), so it also runs on a cold stack.
   `bash scripts/e2e-ui.sh e2e/smoke.spec.ts` brings the stack up
   incrementally and runs Playwright in the pinned `ui-e2e` runner (image
   `mcr.microsoft.com/playwright:v<@playwright/test version>-noble`, edge network only) against
   `https://localhost:8443`. Both runners are non-root: always start them through
   `scripts/e2e.sh`, `scripts/e2e-ui.sh` or `run.sh`, which source `scripts/e2e-runner-env.sh`
   (your uid and gid, the e2e-secrets group 10001 and, for `e2e` only, the Docker socket's group). Chromium maps that origin to `traefik:443`
   (`--host-resolver-rules` in `ui/playwright.config.ts`), so the page origin is the production
   one; only browser traffic is mapped, so fetch from inside the page (`page.evaluate`), not with
   the Node-side `request` fixture. Pass any Playwright args after the spec, for example `-g axe`
   to filter by title; a leading `ui/` on a path is accepted. Traces and failure artifacts are in
   `ui/test-results` (gitignored): `pnpm exec playwright show-trace <trace.zip>`. Journeys import
   `test`, `expect`, `expectNoSeriousA11yViolations` and `expectNoHorizontalOverflow` from
   `ui/e2e/fixtures.ts`; its automatic collector fails any test that triggers a CSP violation. The
   runner mounts the `e2e` secrets read-only and `./.dev` at `/dev-out`, so UI and Python e2e share
   `.dev/admin_password`. `CI_E2E=1 bash scripts/ci.sh` runs the UI e2e on the cold stack before
   `tests/e2e`; `CI_UI=0` skips it.
16. **The full gate (the most comprehensive run).** `CI_FULL=1 bash scripts/ci.sh` (or
   `bash scripts/ci.sh --full`) is never part of the default run: use it once at the end of a phase
   or before a release (about an hour; it tears the stack down with its volumes like `CI_E2E=1`).
   It refuses `CI_E2E=0`, `CI_INTEGRATION=0` and `CI_UI=0`. On top of the default steps it runs the
   unit tests under line+branch coverage with the backend operation tracer (`DC_OP_TRACE`, root
   `conftest.py`), the `timing`-marked tests again without tracing (CPU and wall-clock budgets of the
   rule worker), the integration tests appended to the same data, `scripts/coverage_gate.py` (each
   workspace member at or above its floor in `[tool.dc-coverage.thresholds]`), the UI `ci-full`
   Docker stage (`pnpm ci:full`: Vitest V8 coverage with the thresholds of `ui/vitest.config.ts`),
   the cold stack with every Playwright spec and project (all personas, the layout audit, dark; the
   `apiCalls` fixture of `ui/e2e/fixtures.ts` records which operations each journey reached), the
   whole `tests/e2e` suite (Schemathesis, the API-04 principal matrix, the e2e operation-coverage
   gate) and finally `scripts/operation_coverage.py --gate`: every OpenAPI operation needs a backend
   2xx (`backend_ok`), a backend 401/403/404 (`backend_refused`), a row in
   `tests/repo/authz_matrix.golden.json` (`authz_matrix`) and a UI call site in `ui/src`
   (`ui_client`), or an exemption with a reason per column in
   `tests/repo/operation_coverage_exemptions.json`. Tests marked `performance` (throughput and scale
   budgets) are skipped in every step of `scripts/ci.sh` and in `scripts/e2e.sh` unless
   `DC_PERFORMANCE=1`. Reports land in `.coverage-data/`
   (gitignored); the floors live in `[tool.dc-coverage.thresholds]` and `ui/vitest.config.ts`. Pieces on
   their own: `uv run pytest -m unit tests/repo/test_authz_route_matrix.py` (the generated role x
   operation matrix; `uv run python tests/repo/test_authz_route_matrix.py --update` after a reviewed
   permission change), `DC_OP_TRACE=.coverage-data/optrace.jsonl uv run pytest -m integration ...`
   then `uv run python scripts/operation_coverage.py`, and `pnpm test:coverage` in `ui/`.
17. **Tear down when you are done** with `docker compose down -v` so ports and volumes are free
   for the next run.
