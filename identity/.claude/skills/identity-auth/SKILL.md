---
name: identity-auth
description: Work on identity's login, token and authorization code (bootstrap admin, locked token claims, PDP rules) without ever reading authority from a token.
---

# identity-auth

Use when changing `identity/src/dc_identity/{bootstrap,sessions,tokens,authz,access,guards,users,grants,groups,throttle,gate,principal_ids,entra,credentials,service_accounts,deactivation,denials}.py`,
`identity/src/dc_identity/directory/`, or the auth, user, grant, group, Entra, PAT, service-account
and deactivation routes.

1. **Bootstrap flow.** On first start `bootstrap_hook` (lifespan) creates ROOT, the `admin` user
   and the `global_admin` role assignment at ROOT under an advisory lock, emits one
   `system.bootstrapped` event, and reads `/run/secrets/bootstrap_admin_secret` (at least 32
   characters; the value is never logged or audited). Logging in with that secret yields a
   restricted session (scope `password:rotate`, no refresh token). The only call it may make is
   `POST /api/v1/identity/auth/password`; every `require(...)` route refuses a restricted principal
   with `password-rotation-required` unless it passes `allow_restricted=True`.
1a. **Contracts notifier** (`ensure_notifier`, D-105). After `ensure_bootstrap` the same hook
   provisions the contracts delivery worker's identity from `/run/secrets/notifier_credential`
   (`notifier_credential_file`), one line `<16 hex public id>:<64 hex SHA-256 of the secret>`
   that dev-init generates; identity never sees the plaintext. It creates a ROOT-owned service
   account `contracts-notifier`, one key (name `bootstrap`) with that public id and hash, and a
   ROOT grant of role `notifier` (permissions `mail:send` and `settings:seed-webhook-hosts`, the one-time webhook
   allow-list seed, bound to ROOT, grantable only by a Global admin) under the bootstrap advisory lock, with one `system.notifier_provisioned` event
   (ids only, never the public id, hash or secret) when anything changed and none on a restart
   that changes nothing. A missing, unreadable or malformed file never stops identity: it logs
   `notifier_credential_unavailable` with the reason only. Rotation is a new file plus a restart:
   the new key is inserted and the previous `bootstrap` key revoked in the same unit. A public id
   owned by another credential logs `notifier_credential_conflict` and changes nothing; an
   inactive account logs `notifier_account_inactive` and is never reactivated. Renewal: every
   start, and then every `notifier_renewal_interval_s` (default 3600, `notifier-key-renewal` task in
   `bootstrap_hook`) while identity runs, renews the key in place (same row and public id, `expires_at = now() +
   notifier_key_lifetime_days`) when it has expired or expires within
   `credential_expiry_warning_days`, logs `notifier_credential_renewed` with reason `expired` or
   `expiring`, and lists `renewed_credential_id` in the event. A key that lapses despite
   this (a failing renewal logs `notifier_renewal_failed`) shows as `mail-http-401` on email
   deliveries and as an expiring key in the service-account key listing. A revoked key is never revived: identity logs
   `notifier_credential_revoked` and changes nothing, so write a new pair (for dev-init, delete the
   canonical `notifier_public_id` and `notifier_secret` so they regenerate), then restart identity
   and contracts.
1b. **Browser session cookie** (Phase 8, UI-01, D-UI-08-12/13; `api/auth.py`). Cookie delivery is
   opt-in: `X-Token-Delivery: cookie` on login or refresh sets `__Secure-dc_refresh` (HttpOnly,
   Secure, `SameSite=Strict`, `Path=/api/v1/identity/auth`, `Max-Age` = `refresh_token_ttl_s`,
   re-set on every rotation) and drops `refresh_token` from the body. Clients without the header
   keep the body form on login, refresh and logout and never get a `Set-Cookie`. Refresh and
   logout take the token from the body or, when the body has none, the cookie; both together is 422
   `validation-error`; no token on refresh is 401 `invalid-credentials`. Every refresh 401 on the
   cookie path (and the no-token 401) carries a `Set-Cookie` that expires the cookie; logout is
   always 204 and clears it on the cookie path. CSRF lives here, not in the gateway: a cookie-read
   token needs the header, and any request with the header (login included) needs `Sec-Fetch-Site`
   absent, `same-origin` or `none`; otherwise 403 `csrf-check-failed`. Rotation, reuse detection
   and family revocation are the same `sessions.py` code for both sources. A restricted bootstrap
   login never gets a refresh token or a cookie. Never log or echo the cookie value; the contract
   is declared in OpenAPI (`identity/tests/unit/test_auth_openapi.py`) and tested branch by branch in
   `identity/tests/integration/test_login_cookie.py`.
2. **Password rotation** is one audited write (`user.password_rotated`) that revokes all sessions.
   Policy lives in `password_policy.py` (length, deny-list `data/common_passwords.txt`); hashing is
   Argon2id in `passwords.py`.
3. **Token claims are locked** to `iss, sub, aud, iat, nbf, exp, jti, ptype, scope, name`
   (`tokens.issue_access_token`). Do not add `roles`, `groups` or permissions to a token. Verifiers
   in `dc_auth.verify` never read them either; the mock IdP in compose deliberately mints
   `roles: [global_admin]` to prove they grant nothing.
4. **Never read authority from claims.** Decisions come from `LocalAuthorizer` (`authz.py`) over
   the `role_assignments` and `group_members` tables, mapped through `dc_auth.roles`. The D-31
   evaluator (`access.py`, `effective_roles`) collects the roles a principal holds at a folder from
   grants on the folder and its `ltree` ancestors, held directly or through local groups; roles are
   also held through the Entra groups the gate resolved from Graph (`ResolvedPrincipal.entra_groups`
   fed into `Grantees.entra_groups`). A block-inheritance flag cuts grants held strictly
   above the deepest blocked folder on the path (D-29); grants made at the blocked folder or below
   keep applying and `global_admin` is exempt. `dc_auth.oracle` is the literal reference the SQL
   is proven equal to. A denial is `NOT_FOUND` (404) unless the permission's `visible_via` is held
   at the target, in which case it is `FORBIDDEN` (403); for contract and template reads the read
   permission is its own `visible_via`, so a denied read is always 404 (D-35, D-36). Principal-level
   list permissions (`folder:list`, `contract:list`, `template:list`, `group:list`, `entra:list`,
   `service-account:list`, `pat:list`) are ALLOW for any non-restricted principal and the handler
   trims by scope, so a caller with no access gets an empty page, not a 403. The principal-level
   `me:update` is a write (a read-only PAT is refused) and interactive-only like `pat:manage`. Root permissions
   (`user:create`, `user:read`, `user:update`, `user:deactivate`, `audit:read`, `mail:send`) are evaluated at ROOT;
   `folder:set-block` is evaluated on the parent folder. `password:rotate` is ALLOW only for a
   local principal authenticated with an interactive JWT (`via == "jwt"`), never for a PAT, key or
   Entra principal, and `authz:check` is ALLOW for any non-restricted principal. Every check is live (no
   decision cache), and an unknown permission string is `FORBIDDEN` even for a Global admin.
4b. **PrincipalGate** (`gate.py`, D-57) `PrincipalGate.resolve` runs first in every `LocalAuthorizer`
   decision: `check` for every permission (`authz:check` and `password:rotate` included) and
   `accessible_folders`, so a refused principal never gets ALLOW or a scope from the internal PDP
   API, and `/internal/v1/authz/principal` resolves a forwarded `dcp_` token through it too.
   Liveness is proven for every kind of principal, on every request:
   - A local user must exist and be `is_active` (401 otherwise, so a JWT minted before a
     deactivation stops working).
   - An Entra user or service principal is looked up in the directory port first, outside any
     database session (not found or disabled: 401; directory failure: 503), then admitted only
     through an active, `directly_registered` `entra_registrations` row of its kind or transitive
     membership of an active registered group of its tenant, else 403 `principal-not-registered`
     (the same answer on every public route of every service and through `RemoteAuthorizer`).
     Registration grants no role.
   - A PAT (`via == "pat"`) is loaded live and its owner is re-proven exactly as for a sign-in; a
     key (`via == "api_key"`) is loaded live and its `sa:<uuid>` account must be `is_active`.
   The directory is `GraphDirectoryClient` (`directory/graph.py`: Graph `transitiveMemberOf` paged,
   nextLink only on the configured Graph host, one client-credentials token per tenant) behind
   `DirectoryCache` (60 s, tenant-aware keys, single flight, failures never cached; set
   `DC_DIRECTORY_CACHE_TTL_S=0` to disable). Graph trouble after the window fails closed: 503, no
   stale grace, no direct-grants fallback. Startup raises when an Entra issuer has no
   `DC_GRAPH_CLIENT_ID`. After admission `entra.py` writes the just-in-time records in their own
   `UnitOfWork`: the `entra_users` row (`entra_user.created`/`updated`) and, for a service
   principal admitted only through a group, a `directly_registered=false` row owned by its oldest
   admitting group's owner (`entra_registration.created`). A JIT row never admits; an inactive row
   answers 401. `resolution_for(principal)` hands the gate's answer (`ResolvedPrincipal`: grantee
   type and id, Entra groups) to `access.grantees_for`, which raises `RuntimeError` for Entra,
   `sa:` and PAT or API-key principals the gate has not resolved in the request, so a principal
   without a gate answer can never silently get a group-less identity. Generic OIDC principals
   (`fake:`, `mock:`) pass unchanged.
4c. **Principal ids.** `principal_ids.py` is the only parser of `local:<uuid>`,
   `entra:<tid>:<oid>` and `sa:<uuid>` (canonical lowercase only); never split an id elsewhere and
   never key anything on an email or UPN (`username_hint` is display only).
4d. **Credentials** (`credentials.py`, `service_accounts.py`). A PAT or key is
   `dcp_<public_id>_<secret>`; only the SHA-256 of the secret is stored, looked up by public id and
   compared with `hmac.compare_digest`, and no event, log line or response after creation carries
   the token, secret, hash or public id (`dc_core.logging` also redacts `dcp_` values). Creating
   a key locks the service-account row `FOR UPDATE` before counting live keys, so two concurrent
   requests cannot exceed two active keys (409 `credential-limit-reached`). `last_used_at` is
   batched in memory and flushed, never written per request. Nothing about a credential is cached
   (D-72), so a revocation applies on the next request. Quick 261120-lbz (D-03): `create_pat` locks
   the owner row `FOR UPDATE` (not `FOR SHARE`) and counts the owner's live PATs against the
   effective `max_active_tokens` (`SecurityPolicy`, default 10, stored `security` override 1 to
   1000): at the limit 409 `token-limit-reached` (`limit`, `active_count`) before anything is
   minted; a service account's keys use the same limit under the account lock (below two it wins
   over `credential-limit-reached`). Existing tokens above a lowered limit are never touched.
4e. **`interactive_only`.** A permission with `PermissionSpec.interactive_only` (D-79) is refused
   for a principal that is not interactive (`authz.is_interactive`: `via == "jwt"` and
   `type == "user"`, so a PAT, a key and an Entra service principal's app token are refused;
   WR-01), but only after the normal decision would have
   allowed it, so an invisible target is still 404 and a denial is never turned into
   `interactive-session-required`. `password:rotate` additionally needs `via == "jwt"`.
4g. **Denial recording** (`denials.py`, AUD-04, D-172, D-173). Every `LocalAuthorizer.check` whose
   outcome is not ALLOW writes exactly one identity outbox row, `aggregate_type` `authz`, action
   `check.denied` (subject `audit.identity.authz.check.denied`, stored by audit as category
   `denial`), in its own short transaction that has committed before the decision is returned or
   the refusal raised. The four `detail.outcome` values (`OUTCOMES`) are `not_found`, `forbidden`,
   `gate_refused` (the gate's 401 inactive or stale session and 403 `principal-not-registered`)
   and `interactive_required` (the D-79 refusal, recorded before the 403). A gate 503 (directory or
   Graph outage) is unavailability, not a denial: nothing is recorded and the 503 propagates.
   ALLOW records nothing, and `accessible_folders` never records (trimming is not a denial). If the
   row cannot be written the check answers 503 `service-unavailable` (fail closed, logged as
   `denial_not_recorded` with the error class only); never swallow that error or return an
   unrecorded denial. Over the remote path the 503 becomes `AuthorizerUnavailable` and
   `require()` answers 503. The recorder opens its session only after `_decide`'s session has
   closed, so a check never holds two pooled connections; do not move the write into `_decide`.
   Content: `aggregate_id` is `root` for a root permission, else the target folder id, else
   `none`; `scope_path` is the target folder's ancestors when it exists, `[ROOT]` for a root
   permission, otherwise empty (only ROOT auditors see probes of unknown ids); `detail` has
   exactly `DETAIL_KEYS` = `permission` (at most 64 characters), `outcome`, `service` and `route`
   (the route template, never a raw URL or query string); `before`/`after` are null; the actor is
   the principal; `request_id` is the correlation id of the request identity is serving, which
   is the caller's own `X-Request-ID` forwarded by `RemoteAuthorizer` (a UUID4, accepted by
   identity's `CorrelationIdMiddleware`). Identity's own routes record service `identity` and their
   route template; the internal check route takes `service` and `route` from the check body
   (`unknown` when missing or malformed). A gate refusal over the remote path is recorded once,
   as permission `authz:check`, service `identity`, route `/internal/v1/authz/check`, because
   identity's own `require("authz:check")` runs the gate before the body's permission is read; the
   forwarded request id links it to the caller's request. In-handler probe checks (validation
   batch folder facts, contracts source checks, subscription read checks, the card `folder:read`
   check of a non-member) are recorded like any other denied check. Contracts answers a linked
   card member without a folder role from membership and `accessible_folders` (never recorded)
   without the `folder:read` check, so a member's successful read records no false denial
   (WR-02). `require()`'s own `password-rotation-required` refusal
   happens before the PDP and is not recorded.
4h. **Self-description** (`api/me.py`, D-UI-08-19, D-UI-08-20, UI-07). `GET /api/v1/identity/me`
   and `GET /api/v1/identity/me/capabilities` are gated by the principal-level `me:read` (every
   non-restricted principal; a restricted bootstrap token is 403 `password-rotation-required`) and
   are not audited. They never keep their own copy of the narrowing: `authz.narrow_read_only`
   (D-66, used by `LocalAuthorizer` itself), `authz.capabilities_for`,
   `authz.global_permissions_for` and `authz.folder_capabilities` produce the listed permissions,
   and the interactive-only permissions (D-79) are dropped for a non-interactive principal (the PDP still
   refuses them with its own 403). A change to `LocalAuthorizer._decide` therefore also changes
   what `/me` says; the parity matrix in `tests/integration/test_me.py` (five principals, four
   folder levels) fails when the two drift. `/me/capabilities?folder_id=` runs the PDP `folder:read`
   check itself: every non-ALLOW outcome is the uniform recorded 404, never a 403. `grantable_roles`
   is `guards.grantable_roles(roles, kind)` built on the `can_grant` and `is_bound_to` predicates
   the grant write's `check_ceiling` and `check_level_binding` use, and is empty unless `grant:manage`
   survives narrowing. `assignments` are trimmed with `accessible_folders("folder:read")` (D-35).
   The one write in the module, `PATCH /api/v1/identity/me` (`me:update`, quick 261119-16f), is
   audited: `users.change_display_name` locks the row and writes one `user.display_name_changed`;
   an Entra or service-account caller is 409 `display-name-managed-externally`. The token's `name`
   claim is display-only and refreshes with the access token; nothing reads authority from it.
   `principal_type` is derived from the resolved grantee and is never `pat` or `system`.
4f. **Deactivation** (`deactivation.py`). One transaction per deactivation: the row turns inactive,
   every refresh token, PAT and key it owns is revoked, and one event carries the complete
   `revoked` listing (row ids only). Grants and memberships stay in place and become inert because
   the gate refuses the principal. Reactivation flips `is_active` and sets `sessions_valid_after`
   on `users`, `entra_users` and service-principal `entra_registrations` rows; the gate compares it
   with the verified `iat` of a JWT and refresh rotation compares it with the token's `created_at`.
   Revoked secrets are never restored, and the cutoff only narrows, never grants.
4a. **Escalation guards** (`guards.py`, called in this order by `grants.py`, `groups.py` and
   `folders.py`): take `lock_authz_writes` (one transaction-scoped advisory lock, so a block-flag
   write cannot interleave with a grant write); `reject_archived` (409 `folder-archived`); level binding (`role-level-mismatch`, D-30); the
   grant ceiling checked against the actor's effective roles at the target folder
   (`grant-ceiling-exceeded`, D-32); no self-grant, directly or through a group
   (`self-grant-forbidden`, D-33); adding a group member counts as granting every role the group
   holds, so the ceiling is checked per (role, folder) pair (`check_group_change`); revoking
   follows the same ceiling; grantees are users (`local:` or `entra:<tid>:<oid>`), local groups, Entra
   groups (the route confirms the GUID with Graph in its registration's tenant first; an unregistered GUID
   is 404 with no Graph call) and service principals, the owned ones confined to their owner's Organization
   (`group-scope-mismatch` 422 for a caller who can read the registration, else 404); the last direct Global admin cannot be removed
   (`last-global-admin`, D-34). New users (`users.py`) hold no role until granted (D-26). The
   service files are `access.py`, `guards.py`, `users.py`, `grants.py`, `groups.py`, `folders.py`.
5. **Refresh tokens** are opaque (`secrets.token_urlsafe(32)`), stored as SHA-256 digests, rotated
   on use; presenting a rotated token revokes the whole family and answers 401. Routine
   issuance writes no event; every revocation writes exactly one.
5a. **Sessions list, sign-out and cap** (quick 261120-s0p). A session is a refresh-token family. Rows also store `client_summary`
   (fixed vocabulary from `summarize_user_agent`, never the raw User-Agent) and `access_jti` (login and refresh pre-generate the access
   token's jti and store it, so `Principal.credential_id` finds the caller's family; the locked claim set does not change).
   `GET /me/sessions` (`me:read`) lists live families; `DELETE /me/sessions/{id}` and `POST /me/sessions/revoke-others` (`me:update`) end
   them; a Global admin lists (`user:read`) and ends (`user:deactivate`) a local user's sessions under `/users/{id}/sessions`. Every route
   is interactive-only for writes, local users only for the admin routes, and every effective revocation is exactly one event
   (`session.revoked`, `user.sessions_revoked`, `user.sessions_evicted`) built from ids, counts and a reason only. A revocation takes effect at
   the next refresh; the access token already issued lives until it expires. `security.max_sessions_per_user` (default 5): at password
   sign-in `enforce_session_limit` locks the user row, counts live families by the list's rule and ends the oldest beyond the limit in the
   same unit of work; sign-in is never refused and the restricted bootstrap login never evicts.
6. **Throttle** (`throttle.py`) is enforced inside identity (per account and per client IP,
   exponential lock, `Retry-After`). The gateway's rate limit is an extra layer only, and through
   the gateway a throttled login surfaces as the generic `too-many-requests` 429.
7. **Principal resolve** (`principal_resolve.py`, `POST /internal/v1/authz/principals/resolve`, D-153,
   VAL-05): validation asks whether the owners, team members and teams a contract names exist. The
   answer per submitted name is exactly `found`, `not_found` or `unverifiable`, keyed by the name as
   submitted, and never a display name, id, e-mail, membership or activity (the route is a bounded
   existence check, not a directory read; do not add an attribute to `PrincipalResolveOut`). A user
   name matches, lower-cased and trimmed, a local user's username or e-mail or a known Entra user's UPN
   hint; an unmatched e-mail-shaped name is `unverifiable` while an Entra issuer is configured
   (`app.state.entra_tenant_ids`), otherwise `not_found`. A group name matches a local group or a
   registered Entra group's display name, case-insensitively. Guards: `authz:check`, the principal
   gate first (401/403/503), at most 100 users plus 100 groups of at most 256 characters (wire-model
   422, no submitted value echoed), and a per-principal rolling-minute throttle in process memory
   (`principal_resolve_names_per_minute`, default 1000 names) answering 429 `too-many-requests` with
   `Retry-After`. Accepted residual risk: any authenticated principal can still probe names inside
   that budget, and the budget is per identity replica. The client, `RemoteAuthorizer.resolve_principals`,
   turns a timeout, connection error, 429, 5xx or unreadable answer into `unverifiable` for every name
   (only `not_found` may fail a rule); `dc_auth.testing.FakePrincipalDirectory` is the in-process fake.
8. After any change run `uv run pytest -m unit identity`, then `uv run pytest -m integration
   identity`, then `bash scripts/e2e.sh tests/e2e/test_bypass.py tests/e2e/test_phase_goal_journey.py
   tests/e2e/test_phase4_entra_machine_journey.py` (the last one takes about two minutes: it waits
   out the directory cache). A change to `authz.py`, `denials.py` or the gate also runs
   `identity/tests/integration/test_denial_recording.py` and `bash scripts/e2e.sh
   tests/e2e/test_phase7_denial_guarantee.py`; a change to `DETAIL_KEYS` or `OUTCOMES` updates
   `tests/repo/test_audit_no_secrets.py` and `docs/dev/api-design-decisions.md` ("Audit trail and
   the API surface").
