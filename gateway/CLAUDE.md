# gateway (Traefik v3.7 configuration plus `problem-responder` and `api-docs`)

The public entry point: TLS, CORS, rate limits, body cap and routing. It is deliberately thin and
replaceable; it performs no authentication, and no security control may live only here.

## What this module owns

- `traefik.yml` (static config: entry point `websecure` on 443 with `sanitizePath`, file
  provider, JSON logs that drop `Authorization` and `Cookie`) and `dynamic/*.yml`
  (`routers.yml`, `middlewares.yml`, `tls.yml`).
- `problem-responder/responder.py`: a stdlib-only ASGI app that renders RFC 9457 bodies for the
  errors the gateway itself generates (D-18).
- `api-docs/` (image `infra/docker/api-docs.Dockerfile`, compose service `api-docs` on the edge
  and backend networks, no published port): `merge.py` merges the four services' OpenAPI
  documents (stdlib `merge_specs`, collision policy D-177), `server.py` (stdlib
  `ThreadingHTTPServer`, port 8080) fetches them at start and serves the merged document at
  `/api/v1/openapi.json` plus the vendored Swagger UI (swagger-ui-dist 5.35.0, checksum-pinned at
  build, unpacked with tar) at `/api/v1/docs/`, with `swagger-initializer.js` from this
  directory. Until a merge succeeds, and after a collision, `/healthz` and the document answer 503:
  a partial document is never served (D-176, D-178). `/api/v1/docs/version` answers the container's build
  identity `{service: "api-docs", version, commit, built_at}` from `DC_VERSION` (given at startup, the image holds no
  version), `DC_GIT_SHA` and `DC_BUILD_TIME` (the image's `GIT_SHA`/`BUILD_TIME` build args), "dev" otherwise, the
  same shape as the services' `/version`; it is routed by the existing docs PathPrefix rule.
- Compose publishes exactly one host port, `8443 -> 443`, from this module's container.

## What it must never import

The gateway has no Python dependency on the platform: `responder.py` and `api-docs/*.py` import
the standard library only, never `dc_*` or any service, so a replacement gateway can drop them in.
`api-docs` reads the services' documents over HTTP, never their code. Services never import
anything from here.

## Key files and entry points

- `dynamic/routers.yml`: one trailing-slash `PathPrefix` router per module, the priority-1
  `catchall-problem` router that only targets `problem-responder`, and `api-docs`, the one
  non-module router (``Path(`/api/v1/openapi.json`) || Path(`/api/v1/docs`) ||
  PathPrefix(`/api/v1/docs/`)``, standard middleware list), pinned exactly by
  `tests/repo/test_gateway_config.py`.
- `ui` router (``(Method(`GET`) || Method(`HEAD`)) && PathPrefix(`/`)`` minus `/api/`, `/api`,
  `/internal/`, `/.well-known/`, `/healthz`, `/readyz`; priority 2; service `http://ui:8080`, the
  `ui` compose container built from `infra/docker/ui.Dockerfile`): the single-page app. Any other
  method, and every excluded path, still reaches the catch-all. Middlewares `security-headers`,
  `gateway-problems`, `rl-ui` (no CORS, no body limit). Pinned exactly by
  `tests/repo/test_gateway_config.py`.
- `dynamic/dev-mock-oidc.yml`: **DEV ONLY** router `mock-oidc-dev` (``PathPrefix(`/entra/`)``, no
  prefix stripping, `security-headers` only) to the compose `mock-oidc` Entra stand-in, so a
  browser can run PKCE against the issuer `https://localhost:8443/entra`. It lives in its own file
  so a non-compose deployment omits it; `tests/repo/test_compose_topology.py` pins that `mock-oidc`
  appears in the gateway only there and that the router has no stripPrefix or replacePath.
- `dynamic/middlewares.yml`: `security-headers` (HSTS, `stsSeconds: 31536000`, include
  subdomains), `cors`, `gateway-problems` (errors middleware for 413, 429, 5xx),
  `strip-identity-headers`, `body-limit` (2097152 bytes), `rl-default` (50/s, burst 100; every
  API, docs and catch-all router), `rl-auth` (5/s, burst 10), `rl-ui` (300/s, burst 600; the `ui`
  router only, sized for a cold load of dozens of static chunks per client IP), `replace-path-404`.
- `dynamic/tls.yml`: the certificate files under `/run/secrets` and the TLS 1.2 minimum.

## Tests

- Config rules (no Docker): `uv run pytest -m unit tests/repo/test_gateway_config.py
  tests/repo/test_problem_responder.py tests/repo/test_openapi_merge.py`
- Behaviour through the real gateway: `bash scripts/e2e.sh tests/e2e/test_gateway.py`
- The merged document against the running stack: `bash scripts/e2e.sh
  tests/e2e/test_phase7_schemathesis.py` (Schemathesis through `https://traefik`, about 3 minutes).
  Operation coverage of the whole e2e suite against the merged document is
  `tests/e2e/test_zz_operation_coverage.py` (only a 2xx or 3xx answer counts as coverage, WR-05);
  it enforces only when every `test_phase*.py` module
  runs (`bash scripts/e2e.sh tests/e2e`) and writes `.dev/phase7-operation-coverage.json`.
- Whole repo gate: `bash scripts/ci.sh`.

## Platform rules that apply here

- Every error the gateway generates for a well-formed request is RFC 9457 problem+json with a
  `urn:dc:problem:<slug>` type (D-18). The one exception is syntactically invalid HTTP or URI
  input (for example `%zz`), which Traefik refuses before routing with an empty 400 (D-24).
- `/internal/*`, `/healthz`, `/readyz` and `/.well-known/*` are never routed: the catch-all
  answers 404 for them.
- HSTS is a transport property, not an authorization control: `security-headers` is the outermost
  middleware of every router, so the gateway's own 404, 413 and 429 problems carry it as well. A
  replacement gateway (APIM, ingress, load balancer) must also set
  `Strict-Transport-Security: max-age=31536000; includeSubDomains`. The UI's own security headers
  (CSP, nosniff, Referrer-Policy, Permissions-Policy, COOP) come from nginx in the `ui` container,
  so they survive a gateway swap.
- Thresholds (body bytes, rates, bursts) are plain integers in `dynamic/middlewares.yml`.
- Services keep their real `/api/v1/<module>` prefix; there is no strip-prefix or `root_path`.
- Nothing in `gateway/` may route to `fake-graph`, and `mock-oidc` may appear only in
  `dynamic/dev-mock-oidc.yml` (dev only, never in production).

## Skills

- `.claude/skills/gateway-routing/SKILL.md`: adding a module router, middleware order, the
  api-docs router and merge collisions, swapping the certificate.
- Repo-level skills (run-stack, add-route, add-migration) live in the root `.claude/skills/`.
