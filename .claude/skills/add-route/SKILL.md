---
name: add-route
description: Add an HTTP route to a service the platform way, with a PolicyRouter, require() or public(), a target resolver, the walker allow-list, one audited write, problem slugs and docs.
---

# add-route

Use for any new public or internal route in identity, contracts, validation or audit.

1. **Declare it on a `PolicyRouter`** (`from dc_core.policy import PolicyRouter, public`) whose
   prefix is the real public path, for example `PolicyRouter(prefix="/api/v1/contracts")`. A route
   without a policy fails at import time, so the app will not even start.
2. **Choose the policy.** `dependencies=[require("<resource>:<action>", target=<resolver>)]` for an
   authenticated route (`from dc_auth.deps import require`), or `dependencies=[public()]` for the
   rare route that needs no token. A `target` is a FastAPI dependency returning the folder id the
   permission is checked against (`uuid.UUID | None`), for example `_path_folder_id` or
   `contract_folder` in `contracts/src/dc_contracts/api/contracts.py`. The Authorizer decides
   404 (target not readable, D-08, D-35) versus 403 (readable but not permitted). Every permission
   string must be a key of `dc_auth.roles.PERMISSIONS` (the walker's `known_permissions` enforces
   it), so a new permission is added there first, with its `visible_via`, and mapped to roles in
   `ROLE_PERMISSIONS`. A list or search route declares a principal-level `<resource>:list`
   permission with no target: it is answered ALLOW for any authenticated caller and trims its rows
   with `accessible_folders` (a compact `FolderScope`; `scope_clause` in
   `contracts/src/dc_contracts/search.py`), returning an empty 200 page when the caller can see
   nothing, never a 403 and never a count. Authz-mutating writes (grants, groups, block flags) take
   `dc_identity.guards.lock_authz_writes` first. Never read authority from a token claim.
3. **Reads** take `session: Annotated[AsyncSession, Depends(read_session)]`. **Writes** take
   `uow: Annotated[UnitOfWork, Depends(unit_of_work)]`, mutate through `uow.session`, call
   exactly one `uow.audit(action, aggregate_type, aggregate_id, before=, after=, scope_path=)`
   with snapshots built from the model's allow-listed `__audit_fields__`, and
   `await uow.commit()` before answering. Follow the `audit-events` skill for action names.
4. **Describe it for OpenAPI**: `response_model` with a module-unique DTO name
   (`ContractMetadataOut`, not `Out`; the OpenAPI merge fails loudly on schema-name collisions)
   and `status_code`. `create_app` installs `dc_core.openapi.build_openapi`, which already adds
   the tag (the service name unless the router sets `tags=`), the security requirement
   (`[]` for `public()`, `bearerAuth` for `require()`, read from the route's policy) and the
   statuses every route can produce (400, 429; 401, 403, 503 when guarded; 404 with a path
   parameter; 422 with parameters or a body; 413 with a body), and documents every 4xx/5xx as
   `application/problem+json` with the shared `Problem` schema. The route still declares its
   own route-specific statuses in `responses={...}` with a description (409, 412, 428, 406 and
   so on); never a `default` response and never a `content` for an error (it is rewritten to
   problem+json). `tests/repo/test_openapi_completeness.py` fails CI for an undocumented,
   untagged or wrongly secured operation.
5. **Errors** are `raise ProblemError(status, "<slug>", "<Title>", "<detail>")`. Reuse a slug
   from `docs/user/api-guide.md` ("Errors: RFC 9457 problem details"); add a new
   `urn:dc:problem:<slug>` row there when you need one
   (`tests/repo/test_problem_slugs_documented.py` fails CI for an undocumented slug).
   Never echo submitted input in a problem body.
6. **Register it**: add the router to `routers=[...]` (public) or `internal_routers=[...]`
   (service-to-service under `/internal/v1/...`, never routed by the gateway) in the service's
   `main.py`. The gateway already forwards `/api/v1/<module>/`, so no gateway change is needed
   for a new route in an existing module. The route list is generated, never hand-written: after
   `uv run python scripts/export_openapi.py` and `pnpm --dir ui gen:api`, run
   `uv run python scripts/gen_http_reference.py` to refresh `docs/user/reference/http-api.md`. Its
   Permission column comes from `tests/repo/authz_matrix.golden.json`; refresh that file with
   `uv run python tests/repo/test_authz_route_matrix.py --update` (the command documented in that
   test) and review its diff. Route rules worth recording (status codes, limits, headers) go
   under "Route rules" in `docs/dev/api-design-decisions.md`.
7. **Update the walker test** `<service>/tests/unit/test_route_policies.py`: raise `min_routes`,
   add a deliberately public route to `ALLOWED_PUBLIC`, and keep `ALLOWED_NON_API` for
   non-API paths only. `assert_all_routes_have_policy` fails when a route has no policy, is
   public without being listed, or the walk finds fewer than `min_routes` routes.
8. **Prove it**: unit and integration tests beside the route (`<service>/tests/`), and an e2e
   case through the gateway when it is part of a user journey (`tests/e2e/`). Then
   `bash scripts/ci.sh`. A route with no test of its deny path (401 without a token, 404 or 403
   for an unauthorised caller) is not done.
