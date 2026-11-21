# identity (package `dc_identity`, workspace member `dc-identity`)

Local IdP, folder hierarchy and the policy decision point (PDP) for the whole platform.

## What this module owns

- Local accounts: bootstrap admin, password login, password rotation, opaque rotating refresh
  tokens with reuse detection, per-account and per-IP login backoff.
- Browser session cookie (Phase 8, UI-01): opt-in `X-Token-Delivery: cookie` on login and refresh
  delivers the refresh token as the HttpOnly `__Secure-dc_refresh` cookie (Secure, SameSite=Strict,
  path `/api/v1/identity/auth`), refresh and logout accept it when the body has no token, and
  cross-site use is refused inside identity (`csrf-check-failed`: header plus `Sec-Fetch-Site`).
  API clients that do not send the header keep the body form unchanged (`api/auth.py`; the
  identity-auth skill, item 1b).
- The RS256 access tokens it issues and `/.well-known/jwks.json` (locked claim set:
  `iss, sub, aud, iat, nbf, exp, jti, ptype, scope, name`; authority never travels in a token).
- The folder tree (ROOT -> Organization -> Project -> Team; `ltree` path) and its audit trail.
  Folders are never moved or re-slugged, but a Global admin (`folder:delete`) deletes an empty one
  (quick 261119-0v1; ROOT never): `DELETE /api/v1/identity/folders/{id}` and
  `GET .../deletion-check`. Only live things block (subfolders, owned groups, service accounts and
  Entra registrations here; live contracts, templates, live cards and mandatory rule sets from the
  other services); soft-deleted contracts, archived cards and unused rule sets are purged with the
  folder. Fail closed: any doubt from contracts or validation is 503 and nothing is deleted.
- Users (`POST|GET /users`, created with no access; `PATCH /users/{id}` changes a local user's display name,
  `user:update`, Global admin only; quick 261119-16f), role grants on folders (inherited down the
  tree, assignable to users and local groups, bounded by the grant ceiling), local groups with
  guarded membership, the block-inheritance flag on Organization and Team folders, and the
  effective-access view of a folder.
- The authorization decision API other services call: `/internal/v1/authz/check`,
  `/internal/v1/authz/accessible-folders`, `/internal/v1/authz/grantees` (the caller's own, local-group
  and Entra-group identities) and `/internal/v1/authz/members/check` (may these groups and service
  accounts be linked at a folder, the grant rules as one boolean each; Phase 04.1). Authority comes only from the `role_assignments` and
  `group_members` tables, mapped through `dc_auth.roles`; list routes answer an empty page, never
  403, and an unreadable target is 404 (D-35).
- Entra and machine identities (Phase 4): the `PrincipalGate` that proves liveness and (for Entra)
  registration before every decision, the Microsoft Graph directory port with its tenant-aware
  cache, registrations of Entra groups, users and service principals with just-in-time records,
  personal access tokens (optionally read-only), service accounts with at most two keys, grants to
  all of them, and deactivation and reactivation with the `sessions_valid_after` cutoff.
- The notifier (Phase 04.1, D-105): at bootstrap identity provisions the `contracts-notifier` service
  account, its API key and a ROOT grant of the `notifier` role from the `notifier_credential` file
  (public id and SHA-256 of the secret only; the plaintext never reaches identity). A restart
  renews the key in place when it has lapsed or is inside the warning window, and never revives a
  revoked key. The `notifier` role holds only `mail:send` and `settings:seed-webhook-hosts` (the one-time webhook allow-list seed); contracts' delivery worker presents the
  key to the mail service, which authorizes it through this module's PDP. Provisioning writes one
  `system.notifier_provisioned` event on a change, none on a no-op restart (`bootstrap.py`).
- Database `identity` (role `identity`), version table `alembic_version_identity`.

## What it must never import

Services import only the `dc_*` libraries (`dc_core`, `dc_auth`, `dc_odcs`). This module must not
import `dc_contracts`, `dc_validation` or `dc_audit`, and the `dc_*` libraries never import any
service. `uv run lint-imports` (part of `scripts/ci.sh`) enforces both directions.

## Key files and entry points

- `src/dc_identity/main.py`: `create_identity_app` (`uvicorn --factory dc_identity.main:create_identity_app`).
- `src/dc_identity/api/auth.py` (login, refresh, logout, password), `api/folders.py` (including
  block-inheritance and effective-access), `api/users.py`, `api/grants.py`, `api/groups.py`,
  `api/entra.py` (Entra group, user and service-principal registration, deactivation, and the directory
  search `GET /entra/directory/search`: gate = registration's gate, `search` on the directory port, quick 261119-ofa;
  and the live member list `GET /entra/groups/{registration_id}/members`: `entra:read` at the owner, `group_members` on the
  directory port over Graph `transitiveMembers`, 30 s cache, never stored, quick 261119-wp6),
  `api/credentials.py` (PATs), `api/service_accounts.py` (accounts and keys), `api/wellknown.py`,
  `internal_api.py` (PDP wire API, including `/internal/v1/authz/principal`).
- `src/dc_identity/authz.py`: `LocalAuthorizer`, the Phase 2 decision point over `dc_auth.roles`
  (denied target -> 404 unless `visible_via` is held, then 403; principal-level lists -> ALLOW).
  It also holds the narrowing shared with `/me`: `narrow_read_only` (D-66), `capabilities_for`,
  `global_permissions_for` and `folder_capabilities`, so the capability answer equals the decision.
- `src/dc_identity/api/me.py` (Phase 8, UI-07): `GET /api/v1/identity/me` (who the caller is, groups,
  readable assignments, global permissions) and `GET /api/v1/identity/me/capabilities?folder_id=`
  (roles, PDP-equal permissions, `inheritance_blocked`, `grantable_roles` from
  `guards.grantable_roles`); gated by the principal-level `me:read`, an unreadable folder is the
  uniform recorded 404. The UI never decides access; every write re-checks (D-UI-08-21). `PATCH
  /api/v1/identity/me` (`me:update`, interactive only, quick 261119-16f) is local self-service rename of the
  caller's display name (one audited `user.display_name_changed`; an Entra principal gets 409
  `display-name-managed-externally`); `text.py` holds the shared `DisplayName` (1-200 characters under the
  `dc_core.names` rule; also used by user creation and folders).
- `src/dc_identity/api/principals.py`: `POST /api/v1/identity/principals/names` turns at most 100
  principal ids (`local:`, `sa:`, `entra:`) into display names for any non-restricted principal
  (`principal:read-names`); only the display name leaves identity and unknown ids are absent.
  The UI's `PrincipalName` batches every id on a screen into one call.
- `src/dc_identity/denials.py`: `DenialRecorder` (AUD-04, D-172, D-173). `LocalAuthorizer.check`
  writes one `authz` / `check.denied` outbox row (subject `audit.identity.authz.check.denied`,
  audit category `denial`) for every outcome that is not ALLOW, in its own short transaction that
  commits before the answer leaves identity; a row that cannot be written turns the check into
  503. The flat `detail` (`permission`, `outcome`, `service`, `route`) lives in the `outbox.detail`
  jsonb column added by migration `0009_outbox_detail` (identity's outbox only). `OUTCOMES` are
  `not_found`, `forbidden`, `gate_refused` (the gate's 401 and 403 `principal-not-registered`) and
  `interactive_required` (D-79, recorded before the 403); a gate 503 and ALLOW record nothing, and
  `accessible_folders` never records (trimming is not a denial). `request_id` is the caller's own
  `X-Request-ID`, forwarded by `RemoteAuthorizer` together with the asking `service` and route
  template (`dc_auth.authz_wire.CheckContext`); identity's own routes record service `identity`.
  In-handler probe checks of other services record like any check. A remote gate refusal is
  recorded once as `authz:check` on `/internal/v1/authz/check`, because the gate runs before the
  checked permission is read. Full rules: the identity-auth skill, item 4g.
- `src/dc_identity/gate.py` (`PrincipalGate`: liveness and Entra admission before every decision),
  `directory/` (the directory port, the Graph client `graph.py`, the TTL cache `cache.py` and the
  test fake), `entra.py` (registration services plus just-in-time `entra_users` and service-principal rows), `principal_ids.py` (the one parser of
  `local:`, `entra:` and `sa:` ids), `credentials.py` (PAT and key issue, hash, resolve and
  batched `last_used_at`), `service_accounts.py` (owned `sa:` principals), `deactivation.py` (the
  revoking cascades and reactivation cutoff).
- `src/dc_identity/access.py` (effective roles over the ltree chain with block cuts, visible
  folders), `guards.py` (`lock_authz_writes`, level binding, ceiling, `grantable_roles`, self-grant, last
  Global admin), `users.py`, `grants.py`, `groups.py`.
- `src/dc_identity/bootstrap.py` (idempotent ROOT + first admin), `sessions.py`, `tokens.py`,
  `keys.py` (`SigningKeys`: new keys are RSA-3072 from dev-init and create-secrets.sh, the loader floor stays
  2048 so a deployed key keeps working and identity logs `signing_key_below_recommended_size` at start;
  `DC_JWT_PREVIOUS_PUBLIC_KEY_FILE` keeps the old public key in the JWKS during a rotation),
  `passwords.py`, `password_policy.py`, `throttle.py`, `folders.py`, `models.py`.
- `src/dc_identity/folder_usage.py` (quick 261119-0v1): `FolderUsagePort`, `HttpFolderUsage` (the caller's own
  bearer to contracts' and validation's internal `.../folders/{id}/usage` and `.../purge` routes, any doubt a
  503; never imports their packages) and `folder_usage_hook` (`app.state.folder_usage`; tests inject a fake
  through `create_identity_app(folder_usage=...)`). Settings `DC_CONTRACTS_URL`, `DC_VALIDATION_URL`,
  `DC_FOLDER_USAGE_TIMEOUT_S` (5). `folders.collect_blockers` is the one blocker source for the check and
  the DELETE; `folders.delete_folder` takes `lock_authz_writes`, locks the folder, re-checks `folder:delete`,
  removes the grants and writes one `folder.deleted` event. Migration `0010_folder_delete_guard` replaces the
  delete-refusing trigger with a ROOT-only one.
- Platform settings (quick 261120-0yc, D-01 to D-17): `platform_settings.py` (the store over table
  `platform_setting`, migration `0011`; one row per section holding only stored overrides, a missing field
  means the owning service's Helm/env value; `SettingsStore` 5 s cache cleared on local writes; `validate_section`,
  `diff`, `update_section`, `plan_import`, `apply_import`; `default_entra_tenant` for E1) and `api/settings.py`
  (`/api/v1/identity/settings`: `GET ""`, `PUT /{section}` with `If-Match` `"<section>-r<rev>"`, `/public`, `/export`,
  `POST /import?dry_run=`, `/entra/overview`, `POST /entra/test`, `/about`; plus the internal `GET
  /internal/v1/settings/runtime?sections=` for `RUNTIME_SECTIONS` only). Permissions `settings:read` and
  `settings:write` are ROOT permissions held by the Global admin only (write is interactive only; a refused
  caller is 403, never 404); `settings:read-public` and `settings:read-runtime` are principal-level reads. One
  `settings.updated` or `settings.imported` outbox event per effective write (`before`/`after` are
  `{"<section>": {<stored overrides>}}`, ROOT scope, a no-op writes none). The Entra overview and the secret
  expiry are view only: the Graph port gained `check_connection` and `application_secret_expiries` (only
  `endDateTime` leaves `graph.py`; never a hint). Identity now depends on workspace member `dc-odcs` for the
  depth-capped `load_json` of the import. Settings `DC_AUDIT_URL` and `DC_MAIL_URL` (defaults match the compose
  and Helm Service names) feed About. Quick 261120-0yd adds the `notifications` section (read at runtime by contracts and mail), `POST /api/v1/identity/settings/notifications/preview` (`settings:read`, shared renderer over a fixed sample, not audited) and defaults merged from contracts' and mail's about (`defaults_source` `service`, built-in fallback). A new section: register the model in `dc_core.platform_settings.SECTIONS`
  (and `RUNTIME_SECTIONS` if another service reads it); the routes, export and tabs follow.
- Governance, sign-in and security settings (quick 261120-0ye, D-31 to D-45): `security_policy.py` merges the stored `security`
  section over the `IdentitySettings` env values into one frozen `SecurityPolicy` per request (`request_policy`): effective sign-in
  methods (local is on unless stored off, forced on by `DC_SIGNIN_LOCAL_FORCE_ENABLED`; Microsoft needs a configured Entra issuer and
  is never turned on by a setting), credential ceiling, idle timeout (`DC_REFRESH_TOKEN_TTL_S`), session length (`DC_SESSION_MAX_S`,
  default 7 days), lockout thresholds, `PasswordRules`. `check_signin_change` is the one lockout function (PUT `security` and import,
  dry run included): both off 422, the actor's own method 409, local off without an Entra Global admin 409, Microsoft off without a
  local one 409, all `signin-lockout-risk`, no event. `api/auth.py`: login and refresh answer 403 `sign-in-method-disabled` first
  (before the throttle, counting no failure); `GET /auth/policy` is public (`methods`, `password_rules`, `no-store`);
  `gate.py` refuses an Entra user JWT with the same 403 when Microsoft is off (recorded as `gate_refused`; service principals, PATs
  and keys are unaffected). `sessions.py`: migration `0012` adds `refresh_tokens.session_started_at` (set at login, copied on
  rotation); a rotation after `session_max_s` is the plain expired-token 401; the cookie Max-Age is the idle timeout or the rest of
  the session. `throttle.py` takes the policy (window at least 2 x the longest lock); `credentials.py` `check_expiry(max_days=)`;
  `password_policy.py` `check_password(rules=)` applies to new passwords only (never at login). `/settings/public` gains
  `default_odcs_api_version`, `require_lifecycle_reason`, `credential_max_ttl_days`, `password_rules`; the section defaults of
  governance and platform come from validation's, contracts' and audit's about (`_service_defaults`, four services).
- Webhook allow-list seed (quick 261120-s0p, item 6): the `notifications` section's `webhook_allowed_hosts` is mandatory (an empty list allows
  no host; `dc_core.platform_settings.webhook_host_allowed`). `platform_settings.seed_webhook_hosts` backs the internal
  `POST /internal/v1/settings/webhook-hosts/seed` (permission `settings:seed-webhook-hosts`, ROOT, held by the `notifier` role and the Global
  admin): one transaction under the settings lock inserts the marker row of table `platform_setting_seed` (migration `0014_settings_seed`,
  name `webhook_hosts`; a conflict means a no-op `{seeded: false}`), keeps a non-empty stored list, otherwise stores the valid hosts
  (`check_host_pattern`; invalid ones and the overflow past 100 are counted as `skipped`; deduplicated and sorted) and writes one
  `settings.webhook_hosts_seeded` event, so a host an admin removes is never put back. Hosts are never echoed. Contracts calls it at start-up.
- Active token limit (quick 261120-lbz, D-03): `SecurityPolicy.max_active_tokens` (env `DC_MAX_ACTIVE_TOKENS`, default 10, stored
  `security.max_active_tokens` 1 to 1000) bounds the live PATs of each owner and each service account's keys; `credentials.create_pat` takes the
  owner row `FOR UPDATE`, counts and answers 409 `token-limit-reached` (`limit`, `active_count`); `GET /pats` adds `active_count` and
  `max_active`. Names follow `dc_core.names` (`DisplayName` and the group, service-account, PAT and key names).
- Active sessions (quick 261120-s0p, ASVS 7.4.5 and 7.5.2): a session is a refresh-token family. Migration `0013_refresh_session_client` adds
  `refresh_tokens.client_summary` (a fixed-vocabulary summary of the sign-in User-Agent from `sessions.summarize_user_agent`, at most 64
  characters, the raw User-Agent is never stored) and `access_jti` (the jti of the access token issued with the row; login and refresh
  pre-generate it, so the caller's own session is found without a new claim). `api/sessions.py`: `GET /api/v1/identity/me/sessions` (`me:read`),
  `DELETE /me/sessions/{id}` and `POST /me/sessions/revoke-others` (`me:update`, interactive only), and for a Global admin
  `GET /users/{id}/sessions` (`user:read`) plus `DELETE /users/{id}/sessions/{session_id}` and `POST /users/{id}/sessions/revoke-all`
  (`user:deactivate`, interactive only; local users only). One `session.revoked` or `user.sessions_revoked` event per effective change
  (never a token, hash, jti or User-Agent), none for a no-op; 409 `current-session-unknown` when the caller's session cannot be identified.
  Effective at the next refresh: an issued access token lives until it expires. `security.max_sessions_per_user` (env
  `DC_MAX_SESSIONS_PER_USER`, default 5, 1 to 100): `sessions.enforce_session_limit` locks the user row at password sign-in and ends the oldest
  live sessions beyond the limit with one `user.sessions_evicted` event; sign-in is never refused and the restricted bootstrap login never evicts.
  The compose test stack sets 100 because API seeding and parallel Playwright workers sign the same users in many times.
- `src/dc_identity/migrate.py` and `migrations/`: `python -m dc_identity.migrate upgrade head`.

## Tests

- Unit (no Docker): `uv run pytest -m unit identity`
- Integration (needs Docker, real Postgres 18): `uv run pytest -m integration identity`
- Route policy walker: `identity/tests/unit/test_route_policies.py`.
- Platform settings: `identity/tests/integration/test_platform_settings.py` and `identity/tests/unit/test_graph_settings_probes.py` (respx).
- Folder delete: `identity/tests/integration/test_folder_delete.py` (fake usage port),
  `identity/tests/unit/test_folder_usage_client.py` (respx), and the whole chain in
  `contracts/tests/integration/test_folder_usage.py`.
- Denial recording: `identity/tests/integration/test_denial_recording.py`; on the running stack
  `bash scripts/e2e.sh tests/e2e/test_phase7_denial_guarantee.py` (exactly once per denied check,
  across a NATS outage and an identity restart) and `tests/e2e/test_phase7_authz_load.py` (the
  D-187 load profile at concurrency 25, sized to one identity worker; a known gap, recorded as
  deferred work). The load profile is marked
  `performance` and only runs with `DC_PERFORMANCE=1`: Docker on development hardware cannot hold it.
- Whole repo gate: `bash scripts/ci.sh`.

## Platform rules that apply here

- Every route is declared with `require(...)` or `public()` on a `PolicyRouter` and listed in the
  walker test (`ALLOWED_PUBLIC` for public ones). A route with neither fails at import time.
- Every authz-mutating write (grants, revokes, group changes, block flags) takes
  `guards.lock_authz_writes` first.
- One `uow.audit(...)` per business write; snapshots come only from the model's
  `__audit_fields__` allow-list, so secrets (hashes, tokens) never reach an event.
- Errors are RFC 9457 `application/problem+json` with `urn:dc:problem:<slug>` types listed in
  `docs/user/api-guide.md` ("Errors: RFC 9457 problem details").
- This module has its own migrations and version table; never touch another service's database.
- No security control lives only in the gateway: identity throttles logins itself.

## Skills

- `.claude/skills/identity-auth/SKILL.md`: bootstrap flow, token claims, PDP rules, escalation guards.
- Repo-level skills (run-stack, add-route, add-migration) live in the root `.claude/skills/`.
