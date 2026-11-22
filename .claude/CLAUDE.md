<!-- GSD:project-start source:PROJECT.md -->

## Project

**Contractum**

A central data-contract service for a large company with many data-engineering teams. Teams author, version and publish ODCS data contracts inside a nested folder hierarchy (ROOT → Organization → Project → Team), and Policy Managers author reusable, versioned validation rules that check contracts structurally and semantically — a contract that breaks a mandatory rule cannot be published. Everything is exposed through an authenticated API so data engineers (and their pipelines) can fetch the latest published contract, older versions, or upcoming previews and test their own data against them locally. Backend first; UI is a later phase.

**Core Value:** A data engineer can trust that any *published* contract has passed every mandatory governance rule for its place in the hierarchy, and can reliably fetch exactly the version they need (latest, older, or upcoming preview) through the API — with every change and access audited.

### Constraints

- **Tech stack**: Python (FastAPI) + PostgreSQL — fits data engineers and the future Python SDK
- **Deployment**: Docker / docker compose only — infra team has not decided hosting; no k8s now
- **Identity**: Azure Entra ID and Local auth mandatory from the start; IdP layer pluggable
- **Contract format**: ODCS first
- **Gateway portability**: must be swappable (APIM, ingress, LB may be put in front later) — no auth logic only in the gateway
- **Security**: declarative rules only; no user-supplied code execution

<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->

## Technology Stack

## Headline Decisions (read this first)

| # | Decision | Confidence |
|---|----------|------------|
| 1 | One **uv workspace** (single `uv.lock`) with the four service folders plus `shared/*` libs as members. One pinned FastAPI, Pydantic and SQLAlchemy for the whole platform. | HIGH |
| 2 | **Pin FastAPI exactly** (`0.165.0`). FastAPI shipped about 12 breaking changes between 0.143 and 0.162 (Oct 2026), mostly request-parameter and body semantics. Upgrade deliberately, never by range. | HIGH |
| 3 | **SQLAlchemy 2.1 (async) + psycopg 3 + Alembic**. SQLModel is rejected. | MEDIUM-HIGH (2.1 is only about 6 weeks old; fallback is 2.0.57) |
| 4 | Store each contract version as **JSONB**. Validate against the **vendored official ODCS v3.2.0 JSON Schema** (draft 2019-09) with **`jsonschema-rs`**. Pure-Python `jsonschema` took 1.4 s on a 1000-property contract; `jsonschema-rs` took 5 ms (tested). | HIGH |
| 5 | Rule conditions use **CEL via `cel-expr-python`** (official CEL org, C++ runtime). Rule targets use **`python-jsonpath`** (RFC 9535). JSON-Logic is rejected. | MEDIUM-HIGH (library is 0.1.x; wrap it behind a port) |
| 6 | **Hand-roll hierarchical RBAC in Postgres** (`ltree` path + role-assignment table) behind an `Authorizer` interface. Do not run OpenFGA, SpiceDB or Cerbos in v1. | MEDIUM-HIGH |
| 7 | Tokens: **PyJWT + async JWKS cache** with an issuer registry. The local IdP issues its own JWTs with a JWKS endpoint. Entra uses standard OIDC validation plus Graph for the group-overage case. Opaque PATs and API keys are resolved via identity introspection. | HIGH |
| 8 | Audit and events: **transactional outbox in each service DB -> NATS JetStream -> audit consumer**, with idempotent inserts keyed on `event_id`. | MEDIUM |
| 9 | Gateway: **Traefik v3.7**, file-provider config only. OpenAPI aggregation is a small separate merge step, not a Traefik feature. | HIGH (Traefik), MEDIUM (aggregation approach) |
| 10 | Tests: **pytest 9 + pytest-asyncio + testcontainers + `navikt/mock-oauth2-server`** for compose and e2e. An in-process fake issuer (RSA key + JWKS) covers unit tests. **Schemathesis** and **Hypothesis** are added. | HIGH |

## Recommended Stack

### Runtime and Project Structure

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| Python | **3.13** (3.14 also viable) | Runtime | Every native dependency in this stack ships cp314 manylinux wheels (checked: asyncpg, pydantic-core, cryptography, argon2-cffi-bindings, psycopg-binary, jsonschema-rs, cel-expr-python, common-expression-language). 3.13 is the conservative pick; moving to 3.14 is a one-line change when wanted. SQLAlchemy 2.1, pydantic-settings and cel/jsonpath libs need >=3.11. | HIGH |
| uv | **0.13.7** | Package manager, workspaces, lockfile | One `uv.lock` for all services means one resolved FastAPI/Pydantic/SQLAlchemy and no version drift between services. `uv sync --package <svc> --frozen --no-editable --no-dev` gives small per-service images. | HIGH |
| Docker base image | `python:3.13-slim-trixie` | Service images | **Use glibc/Debian, not Alpine.** `cel-expr-python` and `jsonschema-rs` ship manylinux wheels only (see Pitfalls). | HIGH |
| Docker Compose | v2 (Docker 29.x) | Orchestration | Project constraint. Use `depends_on: condition: service_healthy` and a one-shot `migrate` job per service. | HIGH |

# root pyproject.toml

### Web / API Layer

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| FastAPI | **0.165.0** (pin `==`) | HTTP API, OpenAPI | Project constraint. Pin exactly: 0.145-0.162 contained breaking changes to request-parameter semantics (see Pitfalls). FastAPI also now has native OpenTelemetry support (0.142, opt-in auto-configure since 0.143). | HIGH |
| Starlette | 1.10.0 (transitive) | ASGI toolkit | Starlette is past 1.0. Do not pin separately; let the lock resolve it through FastAPI. | HIGH |
| Pydantic | **2.14.3** | Validation, DTOs, JSON | v2 only. Never use the `pydantic.v1` shim. API DTOs, DB models and the ODCS document stay separate types. | HIGH |
| pydantic-settings | 2.16.2 | Env/secret config | One `Settings` class per service. Supports `/run/secrets` files for Docker secrets. | HIGH |
| uvicorn | 0.54.0 | ASGI server | One process per container (scale with compose replicas), or `--workers N`. Gunicorn is unnecessary. | HIGH |
| httpx | **0.28.1** (pin) | Inter-service, JWKS and Graph calls | Mature, with first-class mocking via `respx` and `pytest-httpx`. **Caveat:** httpx has had no release since Dec 2024. Pydantic now stewards a successor, `httpx2` (2.16.0, May 2026), and FastAPI's `standard` extra now lists both. Keep all HTTP use behind one `dc_core.http` factory so a swap is a one-file change. Revisit when `respx` supports `httpx2`. | MEDIUM |
| structlog | 26.1.0 | Structured JSON logs | Bind `request_id`, `principal_id` and `service`. Pair with `asgi-correlation-id` 5.0.1. | HIGH |

### Database and Persistence

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| PostgreSQL | **18** (`postgres:18`, 18.6) | Primary store, one DB per service | 19 is RC1 only, so 18 is current GA. Native `uuidv7()` for time-ordered PKs, JSONB for contracts, **`ltree`** (bundled contrib) for the folder hierarchy, `FOR UPDATE SKIP LOCKED` for the outbox relay. One instance in dev, with a separate database and DB role per service (identity, contracts, validation, audit). | HIGH |
| SQLAlchemy | **2.1.8** (`sqlalchemy[asyncio]`) | ORM and Core | 2.1 (GA 2026-09-24, then 8 patch releases) makes psycopg 3 the default PG driver and requires Python >=3.11. Typed `Mapped[]` models. **Fallback: 2.0.57**, which is still maintained. Pin `>=2.1,<2.2`. Watch the 2.1 behaviour changes: unconditional autoflush, changed dataclass-default handling, and `greenlet` only installed via the `[asyncio]` extra. | MEDIUM-HIGH |
| psycopg | **3.3.8** (`psycopg[binary,pool]`) | Async + sync PG driver | Same driver for the async app and sync Alembic runs, and the SQLAlchemy 2.1 default. Native `LISTEN/NOTIFY`, COPY and pipeline mode. asyncpg (0.32.0) is faster but needs a second driver for migrations and has PgBouncer prepared-statement quirks. Speed is not the bottleneck for this workload. | MEDIUM-HIGH |
| Alembic | **1.20.1** | Migrations | One `alembic/` dir per service, with its own `version_table`. Set a `naming_convention` on `MetaData` from day one. Run migrations as a one-shot compose job (`migrate`) that the app `depends_on`. Run Alembic with the sync psycopg URL (simplest). | HIGH |
- Contract versions are immutable rows holding `document JSONB`, `odcs_api_version`, `semver`, `lifecycle_state` and `ruleset_pins JSONB`. Lifecycle state lives **outside** the ODCS document, because ODCS has its own `status` field (proposed/draft/active/deprecated/retired) that does not map 1:1 to Draft/Preview/Latest/Unpublished/Deprecated.
- Folders: `id, parent_id, kind, path ltree` with a GiST index. Ancestor lookups become `path @> :p`.
- `created_by/at` and `updated_by/at` come from a shared mixin in `dc-core`, populated from the request `Principal`, never from the client.

### ODCS and Contract Validation

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| ODCS (Bitol) spec | **v3.2.0** (released 2026-09-08) | Contract standard | Latest release. v3.1.0 (2025-12-08) introduced breaking JSON Schema changes (`team` became an object, `relationships`). 3.2.0 adds variables, `vector`/`map`/`enum` types, field deprecation and new server types. | HIGH |
| ODCS JSON Schema | `schema/odcs-json-schema-v3.2.0.json` | **Structural validation source of truth** | Official, JSON Schema **draft 2019-09**, uses `unevaluatedProperties`, required top-level fields `version, apiVersion, kind, id`. **Vendor the exact versioned file into `dc-odcs` (record the commit SHA). Never fetch `…-latest.json`, which is a moving target.** Also vendor v3.1.0 and v3.0.2 so contracts can be validated by their declared `apiVersion`. | HIGH |
| jsonschema-rs | **0.61.0** | JSON Schema validator for ODCS | Supports 2019-09 and `unevaluatedProperties`. 5 ms vs 1,396 ms (pure-Python `jsonschema` 4.26) on a 20-table x 50-column contract (tested). It runs on every save and publish, so this matters. Error output carries `instance_path` and a message, which is enough to map to rule results. It is 0.x, so pin exactly. Keep `jsonschema` 4.26.0 as a **test-only cross-check oracle**. | HIGH (perf, tested), MEDIUM (0.x API stability) |
| open-data-contract-standard | **3.2.0** | Typed Pydantic view of ODCS | Light (depends only on `pydantic>=2.8`, `pyyaml`), version tracks the spec (maps 3.2.0 to >=3.2.0). Use it for typed read access, YAML export and template scaffolding. **It sets `extra=forbid` (tested). Never persist a model round-trip; persist the original validated JSON.** It only knows its own spec version. | HIGH (what it is), MEDIUM (as the persistence model; do not use it as one) |
| PyYAML | 6.0.3 | YAML <-> dict | Always `yaml.safe_load`. Convert to JSON-native types (strings for dates) before JSONB storage and before validation. | HIGH |
| semver (python-semver) | 3.1.1 | Semver bump enforcement | Compare the declared contract `version` against the previous published one. | MEDIUM-HIGH |
- No mature ODCS-native library exists. `pyodcs` 0.9.1 is a single-author project whose 9 releases landed in about 2 days (Jul 2026), and its breaking-change rules are undocumented (LOW). `datacontract-cli` has `changelog` and `breaking` commands, but it is a heavy application (see "What NOT to Use").
- Implement a **normalise-then-classify** diff in `dc-odcs`: (1) convert `schema[]` and `properties[]` lists into dicts keyed by `name` (or `id`) so reordering is not a change and diffs are readable; (2) walk both versions; (3) classify each change with an explicit rule table.
- Use the published `datacontract-cli` rule semantics as the **specification to copy** (documented at cli.datacontract.com, an ERROR/WARNING/INFO model):
- Run `datacontract-cli` as a **dev-only oracle** in tests, via `uvx datacontract-cli`, in a separate tool env and not in the workspace lock.
- `deepdiff` 9.1.0 is a helper for generating test diffs if needed. It is not the classifier.

### Declarative Rule Engine

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| **cel-expr-python** | **0.1.4** (pin `==`) | CEL condition evaluation | Official CEL project (`github.com/cel-expr` org, Apache-2.0), a wrapper over cel-cpp. **Tested:** a `schema.all(...properties.all(...))` rule over 200 properties took **134 us**. The Rust binding took 1.7 ms, and pure-Python `cel-python` took 40 ms. **RE2 regex:** a catastrophic-backtracking pattern returned in 100 us (no ReDoS). **Cost control:** a nested 10^5-element `map` comprehension aborted with `Iteration budget exceeded`. Rust binding: the same expression ran 4.9 s unbounded. Compile-time type checking and serialisable compiled expressions allow rejecting bad rules at rule-publish time. **Maturity risk:** 0.1.x, repo created Dec 2025. The mitigation is the port below. | HIGH (behaviour tested), MEDIUM (maturity) |
| python-jsonpath | **2.2.2** | Target selection (RFC 9535) | Standards-compliant JSONPath (RFC 9535), zero dependencies, and `finditer()` returns the match location as a normalised path and a **JSON Pointer** (`/schema/0/properties/0`), which gives precise error locations in validation reports. 3.2 ms for a 500-property filter query (tested). | HIGH |
- Wrap in a `ExpressionEngine` port in `dc-rules` (`compile(expr, vars) -> Compiled`, `eval(compiled, bindings) -> bool | RuleError`) with a conformance test suite. Fallbacks `common-expression-language` 0.10.0 (Rust; has **no iteration budget**, so it needs process-level timeouts) and `cel-python` 0.5.0 (pure Python, 40 ms, portable) can slot in. The pure-Python one is also the portable option for the future Python SDK.
- Bindings: `this` (the selected node), `contract` (root document), `previous` (previous published version, for compatibility rules), `ctx` (folder level, owner info).
- A missing key raises `NOT_FOUND` rather than yielding false (tested: `c.a.y == 1`). Convention: author guards with `has()`; the engine must map an eval error to a distinct **rule-error** outcome (fail-closed for mandatory rules) rather than "pass".
- Guard rails: expression length cap, contract size cap, `asyncio.to_thread` so evaluation never blocks the event loop, and a wall-clock timeout in the validation service. **Verify in a spike** whether the C++ eval releases the GIL and whether the iteration budget is configurable (neither is documented).
- Cache compiled expressions per `(rule_id, rule_version)` in-process.

### Authentication

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| PyJWT (`pyjwt[crypto]`) | **2.15.1** | JWT validation and issuing | Small, audited and focused. Always pass an explicit `algorithms=[...]` allow-list and `options={"require": ["exp","iss","aud"]}`. | HIGH |
| cryptography | 50.0.2 | Keys, JWKS | Required by PyJWT for RS256/ES256/EdDSA. | HIGH |
| argon2-cffi | **25.1.0** | Local password hashing | Argon2id, the OWASP-preferred algorithm. `PasswordHasher.check_needs_rehash()` handles parameter upgrades. `passlib` is dead (last release 2020). `pwdlib` 0.3.1 is a fine thin wrapper but adds a layer; use it only if multiple hash formats must coexist. | HIGH |
| msal | **1.39.0** | Server-to-server token for Microsoft Graph | `ConfidentialClientApplication.acquire_token_for_client()` for Graph, used only to resolve **group overage** and to look up groups. Sync library, so call via `asyncio.to_thread` and cache. | MEDIUM-HIGH |
| authlib | 1.9.5 | *Defer* | Needed only when the UI phase adds a server-side authorization-code flow. Not needed for a token-validating API. | HIGH (deferral) |
- **Issuer registry** in `dc-auth`: each configured issuer has `issuer`, `jwks_uri`, `audiences`, and a claim-mapper that returns a normalised `Principal(id, type, groups, roles, ...)`. Entra, Local and (later) any OIDC IdP are entries, which makes the IdP layer pluggable without touching the services.
- **JWKS handling:** write a small **async JWKS cache** (httpx, TTL, refresh on unknown `kid` with a rate limit) and feed keys to PyJWT via `jwt.PyJWK`. Do **not** use `PyJWKClient` directly in async code, because it does blocking urllib I/O on a cache miss.
- **Entra specifics:**
- **Local IdP:** identity issues short-lived (about 15 min) RS256 or EdDSA access JWTs, signed with a mounted key, and exposes `/.well-known/jwks.json` with `kid` rotation. Refresh tokens are opaque, stored hashed and rotated on use. Rate-limit and back off failed logins **inside identity** (the gateway is replaceable, so no security control may live only there).
- **PATs / API keys (opaque):** format `dcp_<public_id>_<secret>` (greppable prefix for secret scanners), `secrets.token_urlsafe(32)` secret, store **SHA-256 (optionally HMAC with a pepper)** of the secret. High-entropy tokens do not need Argon2. Lookup by `public_id`, compare with `hmac.compare_digest`, with scopes, expiry, `last_used_at` and revocation. Other services resolve opaque tokens via identity introspection with a short TTL cache (30-60 s) so revocation is fast. Local service accounts use the same mechanism.
- Entra-only helper `fastapi-azure-auth` 5.3.0 is **not** adopted (see "What NOT to Use").

### Authorization

| Piece | Choice | Why |
|-------|--------|-----|
| Hierarchy | `folders.path ltree` (ROOT -> Org -> Project -> Team) + GiST index | The tree is only 4 levels deep and static in shape. Ancestor checks and "all folders under X" are single indexed predicates. |
| Grants | `role_assignments(folder_id, principal_type {user, local_group, entra_group, service_account}, principal_id, role)` | Matches the requirement exactly: inherited downward, assignable at any level, to groups or individuals. |
| Role -> permission map | Static Python/YAML matrix in `dc-auth`, versioned with the code | Roles per level are a fixed set (Global admin/reader, Org admin/reader, Project admin/editor, Team admin/editor/reader, Policy Manager). Auditable in code review. |
| Effective permission | One SQL query: assignments where `folder.path <@ granted_folder.path` for the principal and all their groups | Also yields **list filtering** for free: return the set of accessible folder path prefixes and let the owning service add `WHERE folder_path <@ ANY(:prefixes)` to its list queries. |
| Distribution | `identity` exposes `check` and `accessible-folders`; `dc-auth` provides a client with a short-TTL in-process cache (`cachetools`), invalidated by an `authz.changed` event | Matches "services call identity (cached) for authorization decisions". |
| Verification | Hypothesis property tests: for random trees, grants and principals, the SQL answer equals an in-memory reference implementation | The PROJECT.md requirement for "automated tests for RBAC inheritance" becomes a generated test suite. |
- **OpenFGA** (v1.22.1) and **SpiceDB** (v1.57.0) fit folder inheritance naturally, but each adds a stateful service (SpiceDB needs its own datastore) and consistency tokens or eventual-consistency semantics. "List everything this user can see" is the hard operation in Zanzibar-style systems (`ListObjects`/`LookupResources`, with caveats). Single-company, 4-level static RBAC does not justify that. I found no neutral list-objects benchmark (sources are vendor-authored; LOW).
- **Cerbos** (v0.57.0) is a stateless policy decision point and does not hold the hierarchy, so the tree would be passed as request attributes. **Casbin** (`pycasbin` 2.8.0, active) is workable, but its text model and policy files make "list what I can see" awkward and push logic out of SQL. **Oso** library: last PyPI release Jan 2024 and the vendor moved to a hosted product, so avoid it (MEDIUM).
- **Escape hatch:** because every service goes through the `Authorizer` interface, OpenFGA can replace the Postgres implementation later if rules become relationship-heavy (per-contract sharing, cross-org grants).

### Audit and Event Transport

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| Transactional outbox (hand-written, `dc-core`) | - | Atomic "write + audit event" | Insert the domain row **and** the `outbox` row in the same transaction. A relay task does `SELECT ... FOR UPDATE SKIP LOCKED`, publishes, and marks rows sent. This is the only way to guarantee "every write recorded" without distributed transactions. About 100 lines. | HIGH |
| NATS Server (JetStream) | **2.15.2** (`nats:2.15`, `-js`) | Event bus between services | A single small container, no stateful cluster needed. Durable pull consumers, per-message `Nats-Msg-Id` de-duplication on publish, replay, and subject-based routing (`audit.contracts.*`) leave room for notifications, webhooks and SIEM export later. Redis is **not otherwise needed** (authz cache is in-process, rate limiting is at the gateway), so choosing it just for streams adds a dependency. | MEDIUM |
| nats-py | **2.16.0** | Async NATS client | Official asyncio client. Use it directly, without a framework layer. | MEDIUM-HIGH |
| jsonpatch | **1.45** | RFC 6902 diff for audit | Store `before` and `after` snapshots (JSONB) **and** a `changes` RFC 6902 patch computed by `jsonpatch.make_patch`. It is a standard, language-neutral, replayable format that the future SDK and UI can consume. **Normalise first** (key `schema[]`/`properties[]` by `name`) so array reorders are not noise. | HIGH |
| deepdiff | 9.1.0 | Optional human-readable/typed diff | Only where type-aware diffs are wanted. Not the primary storage format. | MEDIUM |
- **Writes** go through the outbox (atomic, at-least-once). The audit consumer inserts with a **unique `event_id`** so redelivery is harmless.
- **Reads/access logs and permission decisions** are high volume and cannot sit in a business transaction. Publish them directly (fire-and-forget through a bounded in-process queue with batching), accepting that loss during a broker outage is tolerable (the requirement is audited access, not an immutable ledger, which PROJECT.md puts out of scope).
- **Synchronous calls stay synchronous:** `contracts -> validation` before publish is plain HTTP request/response, not an event.
- **If the infra team rejects another stateful service:** fall back to outbox -> HTTP push to `audit /internal/events` with retry (zero new infra, same idempotency key), or PGQueuer 2.0.0 / Procrastinate 3.10.0 as a Postgres-only queue. The outbox design makes the transport swappable.
- Do not add Celery or Redis for background work. In-service `asyncio` tasks guarded by `pg_try_advisory_lock` cover the outbox relay, token cleanup and retention jobs.

### Gateway

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| **Traefik** | **v3.7** (v3.7.17) | Reverse proxy, TLS, CORS, rate limit | Config-only (static `traefik.yml` + file-provider `dynamic/*.yml`). Built-in `rateLimit`, `headers` (CORS) and `stripPrefix` middlewares, file-based TLS certificates (for enterprise CA certs) with ACME optional, health checks, and a dashboard. It does no authentication, which keeps the "gateway is replaceable" rule trivial. | HIGH |
- Option chosen (MEDIUM): a ~100-line Python script, run at image build or container start, fetches each service's `/openapi.json`, prefixes paths under `/api/v1/<module>`, merges `components` (fail loudly on schema-name collisions), and writes one `openapi.json`. A static-file container serves it with Swagger UI or Scalar at `/api/v1/docs`, routed by Traefik.
- To avoid component-name collisions, give Pydantic models module-unique names (`IdentityUserOut`, `ContractVersionOut`) and keep `separate_input_output_schemas` consistent across services.
- `root_path` as a `FastAPI()` argument is **deprecated** (0.146). Mount each service's routes under their real prefix (`/api/v1/<module>`) in the app, or use `fastapi run --root-path`. Do not depend on a stripPrefix plus `root_path` combination. Keep real routes prefixed so the services stay gateway-independent.

### Testing and Tooling

| Tool | Version | Purpose | Notes | Confidence |
|------|---------|---------|-------|------------|
| pytest | 9.2.0 | Test runner | | HIGH |
| pytest-asyncio | 1.4.1.post1 | Async tests | Use `asyncio_mode = "auto"` with a session-scoped loop for DB fixtures. | HIGH |
| testcontainers | 4.15.0 | Real Postgres 18, NATS, mock IdP in tests | Prefer **real Postgres** over SQLite (ltree, JSONB, `SKIP LOCKED` are Postgres-specific). Use one container per session and a template DB or transaction rollback per test. | HIGH |
| **navikt/mock-oauth2-server** | **6.1.5** (`ghcr.io/navikt/mock-oauth2-server`) | Entra stand-in for compose and e2e | Supports client-credentials, auth-code with PKCE, JWT-bearer/OBO, and per-issuer discovery at `/{issuerId}/.well-known/openid-configuration`. Claims (`roles`, `groups`, `_claim_names` for the overage case, `tid`, `ver`) are configured via `JSON_CONFIG`/`tokenCallbacks`. It does not emulate Entra's URL layout, so make the issuer registry configurable per issuer (this is already the design). | MEDIUM-HIGH |
| In-process fake issuer | (own, about 60 lines) | Unit tests | Generate an RSA key, serve a JWKS through `respx`, mint tokens with PyJWT. Fast, no container, and covers expired, wrong-aud, wrong-iss and overage cases deterministically. | HIGH |
| respx | 0.23.1 | httpx mocking | Graph and JWKS stubs. | HIGH |
| Hypothesis | 6.170.15 | Property tests | RBAC inheritance equivalence, diff-classifier invariants, normalise/denormalise round-trips. | HIGH |
| Schemathesis | 4.33.1 | OpenAPI-driven API fuzz and conformance | Directly supports "every endpoint documented in OpenAPI": it fails when responses diverge from the spec. Run against each service in CI. | HIGH |
| polyfactory | 3.5.0 | Test data | Optional. | MEDIUM |
| ruff | 0.17.4 | Lint + format | | HIGH |
| mypy | 2.4.1 | Type checking | Use `strict` with the Pydantic plugin. SQLAlchemy 2.1 row typing needs mypy >=1.7. `ty` 0.0.99 is still pre-1.0, so do not gate CI on it. | MEDIUM-HIGH |
| Keycloak | 26.8.2 | *Optional* higher-fidelity IdP | Heavier (realm import, slow start). Use only if you need a realistic interactive login flow for the UI phase. | MEDIUM |

## Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| uuid-utils | 1.2.0 | UUIDv7 in Python | Pre-generating IDs (outbox `event_id`) on Python 3.13. Python 3.14 has `uuid.uuid7()` in the stdlib; PG 18 has `uuidv7()` for DB defaults. |
| cachetools | latest | In-process TTL caches | Authz decisions, JWKS, compiled CEL. |
| tenacity | 9.2.1 | Retries with backoff | Outbox relay, Graph and JWKS fetches. |
| email-validator | 2.3.0 | Email validation in DTOs | Local user registration. |
| python-multipart | 0.0.33 | Upload parsing | Only if contract file upload endpoints are added (validate-uploaded-contract). |
| fastapi-pagination | 0.16.2 | Pagination helpers | Optional. A hand-rolled keyset pagination helper in `dc-core` is also fine and keeps the OpenAPI shape consistent. |
| OpenTelemetry (FastAPI-native, `opentelemetry-sdk`) | 1.45.x | Traces/metrics | Opt-in. Auto-configure is off by default since FastAPI 0.143. Defer until after the core flows work. |
| slowapi | 0.1.10 | In-app rate limiting | Only for login and token endpoints as defense-in-depth. The gateway does the general rate limiting. |

## Alternatives Considered

| Category | Recommended | Alternative | Why Not (for this project) |
|----------|-------------|-------------|----------------------------|
| ORM | SQLAlchemy 2.1 + Alembic | SQLModel 0.0.48 | Pre-1.0, merges API and DB types, weaker for JSONB and immutable-version modelling. |
| PG driver | psycopg 3 | asyncpg 0.32.0 | Faster, but two drivers (async app, sync migrations), PgBouncer prepared-statement friction. Switch if profiling shows driver overhead. |
| SQLAlchemy version | 2.1.x | 2.0.57 | Safer and older. Use this fallback if 2.1 regressions or third-party incompatibility appear. |
| JSON Schema | jsonschema-rs | jsonschema 4.26 | 270x slower on realistic contracts (tested). Keep for test cross-checks. |
| Rule condition language | CEL | JSON-Logic | Python JSON-Logic ports are small and stale (`json-logic` last release 2015), with no type checking or cost model, and JSON-encoded expressions are hard to author and review. CEL is typed, non-Turing-complete, has linear-time RE2 regex, and runs in Go/Java/JS/Rust for future SDKs. |
| CEL runtime | cel-expr-python | common-expression-language 0.10.0 (Rust, hardbyte) | 13x slower here (1.7 ms vs 134 us) and no iteration budget (4.9 s unbounded comprehension). Use it as the fallback with process-level timeouts. |
| CEL runtime | cel-expr-python | cel-python 0.5.0 (pure Python, Cloud Custodian) | 40 ms per evaluation here. Good portable fallback and a candidate for the future SDK. |
| JSONPath | python-jsonpath | jsonpath-ng 1.16.0 | Actively maintained (released 2026-11-04) and faster on my test (1.2 ms vs 3.2 ms), but not RFC 9535-compliant in its extended parser and gives no JSON Pointer output. Compliance and error locations matter more for governance reporting than 2 ms. |
| Authorization | Postgres ltree + role table | OpenFGA / SpiceDB | Extra stateful service plus consistency/list-objects complexity for a 4-level static hierarchy. Revisit if per-object sharing arrives. |
| Authorization | Postgres ltree + role table | Cerbos / Casbin | Hierarchy and list-filtering do not map cleanly; logic leaves SQL. |
| Event transport | Outbox -> NATS JetStream | RabbitMQ 4.4.0 | Fine and widely known in enterprises, but heavier to operate, with more concepts for the same need. Choose it if the infra team already runs RabbitMQ. |
| Event transport | Outbox -> NATS JetStream | Redis Streams (Redis 8.10 / Valkey 9.1) | Works, but Redis is not otherwise needed and has licensing history. Choose it only if Redis is already standard. |
| Event transport | Outbox -> NATS JetStream | Postgres queue (PGQueuer / Procrastinate) | Zero new infra. Good fallback. Less natural fan-out to future consumers. |
| Gateway | Traefik | Caddy 2.11 | Simplest config, but rate limiting needs a third-party plugin (custom build), which defeats "thin, off-the-shelf". |
| Gateway | Traefik | Envoy 1.40 | Powerful and verbose. Overkill for a thin proxy. |
| Gateway | Traefik | Kong | OSS images appear frozen at 3.9.x and 3.10+ seems to require a license (community reports, MEDIUM). Avoid a licensing surprise. |
| Entra validation | PyJWT + own JWKS cache | fastapi-azure-auth 5.3.0 | Entra-only and tied to FastAPI OpenAPI security schemes, and the docs do not cover overage. Does not fit a pluggable IdP layer. |
| Password hashing | argon2-cffi | pwdlib 0.3.1 | Thin wrapper, fine. Use it if multiple hash schemes must be migrated. |
| Mock IdP | mock-oauth2-server + in-process fake | Keycloak 26.8 | Heavier. Use only for realistic interactive login later. |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `datacontract-cli` as a runtime dependency | Hard-depends on `boto3`, `jinja2`, `sqlglot`, `rich`; pins `pydantic<2.15`, `pyyaml~=6.0.1`, and (api extra) `fastapi<0.162`. It would fight the workspace lock. It is a CLI application, not a library. | The `open-data-contract-standard` model + own `dc-odcs` diff. Use the CLI as a dev-only oracle via `uvx`. |
| `pyodcs` 0.9.1 | Single-maintainer, 9 releases in about 2 days, undocumented rules (LOW). | Own classifier built on the documented datacontract-cli rule semantics. |
| `jsonschema` (pure Python) on the request path | 1.4 s per large contract (tested). | `jsonschema-rs` |
| ODCS `-latest.json` schema URL | Moving target, so validation results change without a code change. | Vendored `v3.2.0` file with recorded commit SHA, selected by the contract's `apiVersion`. |
| SQLModel | See above. | SQLAlchemy 2.1 typed models + Pydantic DTOs |
| `passlib` | Unmaintained since 2020 (1.7.4), and it breaks on newer `bcrypt`. | `argon2-cffi` |
| `python-jose` | Poor maintenance history and past algorithm-confusion CVEs. | `PyJWT` |
| JSON-Logic (`json-logic` etc.) | Abandoned or tiny ports, no type checking, no cost limits. | CEL |
| Any Python-expression evaluator (`simpleeval`, `asteval`, `eval`) | Code-execution attack surface. This contradicts the "declarative only" security constraint. | CEL |
| Oso library | Last release Jan 2024, vendor moved to a hosted product (MEDIUM). | Hand-rolled RBAC, or OpenFGA if an engine is needed |
| Alpine base images | `cel-expr-python` and `jsonschema-rs` wheels target manylinux (glibc). Building on musl means compiling C++ or Rust from source. | `python:3.13-slim-trixie` |
| Floating FastAPI version (`fastapi>=`, `fastapi[standard]` unpinned) | 0.143-0.162 shipped breaking changes in about 3 weeks. | `fastapi==0.165.0` with a deliberate upgrade phase |
| `FastAPI(root_path=...)` | Deprecated in 0.146. | Real route prefix, or `fastapi run --root-path` |
| `ORJSONResponse`/`UJSONResponse` | Unneeded: Pydantic v2 serialises in Rust. | Default responses |
| Celery or Redis for background work | Extra infra for a small job set. | Outbox relay and `asyncio` tasks with advisory locks |
| Pydantic v1 compat (`pydantic.v1`) | Dead end. | Pydantic 2.x only |

## Installation

# Workspace bootstrap (from repo root)

# Shared libs and services (create each with `uv init --lib shared/dc-core`, etc.)

# --- Web + core (services) ---

# --- dc-auth ---

# --- dc-odcs ---

# --- dc-rules ---

# --- events / audit ---

# --- dev (workspace root) ---

# docker-compose.yml (infra services only; pin images)

#   postgres:   postgres:18

#   nats:       nats:2.15   (command: ["-js", "-sd", "/data"] + volume)

#   gateway:    traefik:v3.7

#   dev IdP:    ghcr.io/navikt/mock-oauth2-server:6.1.5

## Stack Patterns by Variant

- Swap NATS for it behind `dc_core.events.Publisher/Consumer`.
- The outbox, idempotent consumer and event envelope do not change.
- Use outbox -> HTTP push to `audit`, or a Postgres queue (PGQueuer/Procrastinate).
- Ship `dc-rules` with `cel-python` (pure Python) as the portable engine and `cel-expr-python` as an optional speedup.
- Introduce OpenFGA behind the existing `Authorizer` interface. Keep folder RBAC as the baseline model.
- Add an issuer-registry entry plus a claim mapper. No service code changes.

## Version Compatibility

| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| fastapi 0.165.0 | starlette >=0.46 (resolves to 1.10.0), pydantic >=2.9 | Pin FastAPI exactly. FastAPI now depends on `opentelemetry-api`. |
| sqlalchemy 2.1.x | Python >=3.11, alembic 1.20.1 (needs SA >=2.0), psycopg 3.3.x | `greenlet` is only installed by `sqlalchemy[asyncio]`. |
| sqlmodel 0.0.48 | `sqlalchemy<2.2` | Not used; listed because it constrains SA if ever added. |
| open-data-contract-standard 3.2.0 | pydantic >=2.8, pyyaml >=6 | Maps to ODCS 3.2.0 only. `extra=forbid`. |
| datacontract-cli 1.2.9 (dev-only) | `pydantic<2.15`, `open-data-contract-standard<3.3` | Reason it stays out of the lock. |
| jsonschema-rs 0.61.0 | Python >=3.10, abi3 wheels | 2019-09 validator selected via `validator_for(schema)` (uses `$schema`). |
| cel-expr-python 0.1.4 | Python 3.10-3.14, manylinux_2_27+ / macOS / Windows wheels | No musl wheels. Extensions in `cel_expr_python.ext.*`. |
| pydantic-settings 2.16.2 | Python >=3.11 | Sets the floor with SA 2.1 (Python >=3.11). |
| respx 0.23.1 / pytest-httpx 0.36.2 | `httpx` 0.28.x | Do not move to `httpx2` until these support it. |
| PostgreSQL 18 | `ltree`, `uuidv7()` | `ltree` is in the official image contrib. 19 is RC only. |

## Open Items to Resolve Early (feed into phase research)

## Sources

- PyPI JSON API (`pypi.org/pypi/<pkg>/json`), queried 2026-11-09 for all package versions, release dates, `requires_python` and `requires_dist`. HIGH.
- GitHub releases API: `fastapi/fastapi` (0.143-0.162 breaking changes), `bitol-io/open-data-contract-standard` (v3.2.0 on 2026-09-08, v3.1.0 on 2025-12-08), `traefik/traefik`, `nats-io/nats-server`, `navikt/mock-oauth2-server`, `openfga/openfga`, `authzed/spicedb`, `cerbos/cerbos`, `Kong/kong`, `keycloak/keycloak`. HIGH.
- ODCS official JSON Schema: `github.com/bitol-io/open-data-contract-standard/schema/odcs-json-schema-v3.2.0.json` (draft 2019-09; identical to `-latest.json` at the time of fetch). HIGH.
- Local tests (venv, Python 3.12): ODCS schema validation with `jsonschema` vs `jsonschema-rs`, ODCS pydantic model strictness, CEL runtime comparison (cel-expr-python, common-expression-language, cel-python), JSONPath benchmarks (python-jsonpath, jsonpath-ng). HIGH for the observed behaviour.
- `github.com/cel-expr/cel-python` README (C++ wrapper, extensions, official CEL org `cel.dev`). HIGH on provenance, MEDIUM on maturity.
- [SQLAlchemy 2.1 migration notes](https://docs.sqlalchemy.org/21/changelog/migration_21.html) (Python >=3.11, psycopg 3 default, greenlet extra, autoflush and dataclass changes). The page was truncated at 100k characters, so the PostgreSQL driver section was not read. MEDIUM-HIGH.
- [Microsoft Learn: group claims and app roles in tokens](https://learn.microsoft.com/en-us/security/zero-trust/develop/configure-tokens-group-claims-app-roles): 200-group JWT limit, overage detection via `_claim_names`/`hasgroups`, Graph `transitiveMemberOf`, "Groups assigned to the application", app roles. HIGH.
- [uv Docker integration guide](https://docs.astral.sh/uv/guides/integration/docker/): multi-stage pattern (`--no-install-workspace`, `--no-editable`, cache mounts). `--package` per-member sync is from uv workspace docs, not re-fetched. HIGH/MEDIUM.
- [datacontract CLI: compare contract versions / breaking](https://docs.datacontract.com/compare-contract-versions) and [pyodcs compatibility analysis](https://odcs.readthedocs.io/en/latest/user/compatibility/): used as rule-semantics references. The pyodcs page does not state maintainer, licence or maturity. MEDIUM/LOW.
- [Kong OSS 3.10 image discussion](https://github.com/Kong/kong/discussions/14405) and [Kong forum thread](https://discuss.konghq.com/t/is-opensource-support-for-kong-ceased/13529): community reports that OSS images stopped at 3.9.x. No official statement found. LOW-MEDIUM.
- Zanzibar-style engine comparisons (authzed.com, startwithidentity.com, others): vendor-authored, no neutral list-objects benchmark found. LOW; used only directionally.

<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->

## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->

## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->

## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->
