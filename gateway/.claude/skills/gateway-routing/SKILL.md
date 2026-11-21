---
name: gateway-routing
description: Change the Traefik gateway safely by adding a module router, keeping the middleware order that preserves problem+json errors, protecting the catch-all, keeping the api-docs router and merged OpenAPI document collision-free, and swapping the dev certificate for an enterprise CA.
---

# gateway-routing

Use when editing `gateway/traefik.yml` or `gateway/dynamic/*.yml`.

1. **Add a module router** in `dynamic/routers.yml`: a router whose rule is a trailing-slash
   prefix, for example ``PathPrefix(`/api/v1/<module>/`)``, with `entryPoints: [websecure]`,
   `tls: {}`, the standard middleware list below, and a `services` entry pointing at
   `http://<module>:8000`. The trailing slash matters: `/api/v1/identity-evil/` must not match.
   If a path needs stricter limits (like `/api/v1/identity/auth/`), add a longer-prefix router
   with the stricter limiter; Traefik prefers the longer rule. A new module also goes into
   `DC_API_DOCS_SERVICES` and `depends_on` of the `api-docs` compose service and the `urls` list
   of `api-docs/swagger-initializer.js`, so its document joins the merge.
2. **Middleware order is fixed**: `[security-headers, cors, gateway-problems,
   strip-identity-headers, body-limit, <rate limiter>]`. `security-headers` (HSTS) is outermost on
   every router, including `ui` and the catch-all, so HSTS also lands on gateway-generated 404,
   413 and 429 problems; never move it inward. `cors` comes next so CORS headers also land on
   re-rendered errors.
   `gateway-problems` must come second so it wraps the body limit and the rate limiter and sees
   their 413 and 429; that is what keeps gateway errors problem+json (D-18). Never move it inward
   or drop it from a router.
3. **The catch-all stays dumb.** `catchall-problem` (`PathPrefix(`/`)`, priority 1) targets only
   `problem-responder`, through `replace-path-404`. Never point it at an application service and
   never add an application router with a lower priority than it. Paths the catch-all answers
   with 404 on purpose: `/internal/...`, `/healthz`, `/readyz`, `/.well-known/...`, unknown
   modules, sanitized dot-segment paths.
4. **Thresholds** live only in `dynamic/middlewares.yml` as integers (`body-limit`
   `maxRequestBodyBytes`, `rateLimit` average/period/burst). Update the constants at the top of
   `tests/e2e/test_gateway.py` when you change one.
5. **No authentication here.** Services verify tokens themselves; do not add a forward-auth or
   basic-auth middleware as the only control. `strip-identity-headers` only removes spoofable
   identity headers.
6. **Swap the dev certificate for an enterprise CA.** The `traefik` container mounts the
   `dev-secrets` volume subpath `traefik` at `/run/secrets`, and `dynamic/tls.yml` reads
   `/run/secrets/tls.crt` and `/run/secrets/tls.key`. In a deployment, replace those two files in
   that mounted subpath (or mount your own directory with the same file names) with the
   enterprise certificate chain and key; no configuration change is needed. Keep
   `options.default.minVersion: VersionTLS12`. Clients then trust your CA instead of
   `.dev/ca.crt`.
7. **The api-docs router is a non-module router (the `ui` router below is the other).** `api-docs` (rule
   ``Path(`/api/v1/openapi.json`) || Path(`/api/v1/docs`) || PathPrefix(`/api/v1/docs/`)``, the
   standard middleware list, service `http://api-docs:8080`) is skipped by the module-prefix test
   and pinned exactly by its own test in `tests/repo/test_gateway_config.py`; change both
   together. Exact `Path`s plus the trailing-slash prefix keep `/api/v1/docsevil` on the
   catch-all. Do not add any non-module router beyond `api-docs` and `ui`. Under `/api/v1/docs/` the server serves only
   its allow-list of vendored Swagger files; never load a script, style, validator or spec from
   another origin (CSP `default-src 'self'`, `validatorUrl: "none"`).
   **The `ui` router is the one application router that is not a module or api-docs.** Rule
   ``(Method(`GET`) || Method(`HEAD`)) && PathPrefix(`/`) && !PathPrefix(`/api/`) && !Path(`/api`)
   && !PathPrefix(`/internal/`) && !PathPrefix(`/.well-known/`) && !Path(`/healthz`) &&
   !Path(`/readyz`)``, priority 2, chain `[security-headers, gateway-problems, rl-default]` (no
   cors: same origin), service `http://ui:8080`. Adding a new reserved path means adding an
   exclusion to this rule and to its pin in `tests/repo/test_gateway_config.py`, then proving the
   path still answers the catch-all 404 in `tests/e2e/test_gateway.py`. HSTS is a transport
   property: a replacement gateway must set it too, and the UI's own CSP and other headers come
   from nginx in the `ui` container, not from here.
8. **Merge collisions are fixed in the service, never in the merge.** `api-docs/merge.py`
   keeps a component with the same name and canonically identical JSON once (the shared
   `Problem` and `bearerAuth`), and refuses the same name with different content, a duplicate
   `operationId`, a duplicate path and a path outside the owner's `/api/v1/<svc>/` prefix, naming
   both owners. `tests/repo/test_openapi_merge.py` runs the same merge over the four in-process
   apps, so a collision fails CI; on the stack `api-docs` stays unhealthy and serves no document.
   Rename the model (module-unique names such as `IdentityUserOut`) or the operation in the
   service. Never add renaming, prefixing or "last one wins" to the merge.
9. **Upgrade Swagger UI** by changing the version and the `ADD --checksum=sha256:` value in
   `infra/docker/api-docs.Dockerfile` (sha256 of the npm tarball; check its sha512 against npm's
   `dist.integrity`) after a package-legitimacy check. Keep the `tar` unpack: never
   `npm install`, which would run the dependency's install script.
10. **The dev-only mock IdP route** lives alone in `dynamic/dev-mock-oidc.yml` (header comment
   `DEV ONLY`, router `mock-oidc-dev`, ``PathPrefix(`/entra/`)``, `security-headers` only, no
   stripPrefix: the mock derives `iss` from `Host` and `X-Forwarded-Proto` plus the path, so
   stripping would change the issuer). Do not add `mock-oidc` or `fake-graph` to any other gateway
   file; `tests/repo/test_compose_topology.py` pins that and the shape of the file. A deployment
   without the compose mock removes the file and the compose `mock-oidc` edge network.
11. **Verify** with `uv run pytest -m unit tests/repo/test_gateway_config.py
   tests/repo/test_problem_responder.py tests/repo/test_openapi_merge.py`, then
   `bash scripts/e2e.sh tests/e2e/test_gateway.py`.
   Traefik watches the dynamic directory, but restart the container when you change
   `traefik.yml`.
