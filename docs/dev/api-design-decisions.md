# API design decisions

This page records how the Contractum HTTP API is designed and why. It is for developers who
change the services. API users should read the [API guide](../user/api-guide.md), which covers
sign-in, roles, versions, formats, paging, caching, every problem type and the validation report.
The route list is generated into the [HTTP API reference](../user/reference/http-api.md).

These conventions are consumed by pipelines, the Python SDK and the merged OpenAPI document. They
are one-way: changing them after routes exist breaks a published contract.

## Public URL scheme

Public routes are module-prefixed: `/api/v1/<module>/...`, where `<module>` is one of
`identity`, `contracts`, `validation`, `audit`. Each service mounts its real routes under
this prefix itself (no gateway strip-prefix, no `FastAPI(root_path=...)`), so the services stay
gateway-independent and the gateway stays replaceable. The one public path outside a module
prefix is the merged API documentation: `/api/v1/openapi.json` and `/api/v1/docs/`, served by the
gateway's `api-docs` container, not by a service.

## Errors: problem details

Every client-visible error is `application/problem+json` (RFC 9457) with a type
`urn:dc:problem:<slug>`, including errors the gateway itself generates (unknown route, body too
large, rate limited, upstream failure). The mechanism is described under "Gateway-generated
errors" below. A slug is one-way once shipped: a slug no route returns any more stays documented as
retired and is never reused.

The full list of slugs, with status and meaning, is in the
[API guide](../user/api-guide.md#errors-rfc-9457-problem-details). `tests/repo/test_problem_slugs_documented.py`
fails when code raises a slug that the guide does not list, so add the row there in the same
change.

## Names

One rule, `dc_core.names` (`NameRule`, `check_name`), decides what a user-entered name may contain, so a
name cannot spoof a "by whom" cell, a title or an audit row. The UI applies the same rule before submit
(`ui/src/components/form/nameRule.ts`, message "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @
only."); the server stays authoritative.

- **Allowed:** letters, marks and numbers of any script (Unicode L*, M*, N*), the regular space U+0020 and
  the punctuation `- _ . , ( ) & ' / : # + @`.
- **Refused:** emoji and pictographs; symbols (So, Sk, Sc and Sm other than `+`); control characters (Cc,
  tabs and newlines included); format characters (Cf: bidirectional embedding, override and isolate points
  U+202A to U+202E and U+2066 to U+2069, zero-width characters, ZWNJ and ZWJ, BOM); private-use, unassigned
  and surrogate code points; line and paragraph separators; every space other than U+0020 (a no-break space
  is refused); the variation selectors (U+FE00 to U+FE0F, U+E0100 to U+E01EF) and combining enclosing marks
  (U+20DD to U+20E4), which are marks but turn allowed characters into emoji. A typographic apostrophe
  (U+2019) is refused: use `'`.
- **Order:** every character of the value as sent is checked first (so a tab or newline at an end is
  refused), then regular spaces are stripped at both ends, then a blank result is refused. The stored value
  is the trimmed one.
- **Refusal:** 422 `validation-error` with `errors[{pointer, detail}]`; the pointer names the field (for
  example `/body/display_name`, `/query/name`, or `/name` for the name inside an uploaded contract) and the
  detail is "Contains U+202E, which is not allowed. Use letters, numbers, spaces and - _ . , ( ) & ' / : # +
  @ only." (the first offending character as `U+XXXX`, at least four upper-case hex digits, never the
  character itself) or "Must not be blank.".
- **Stored names are not migrated and never block anything:** reads return names as stored, and a write
  that keeps a legacy name (a draft save with the contract's stored name, a card PATCH without a name) is
  not re-checked. Only a new or changed name is.
- **Fields covered:** folder `display_name` (create, rename); user `display_name` (create, `PATCH /users`,
  `PATCH /me`); local group, service account and access-token (`POST /pats`) and service-account key names;
  the notification sender name (`PUT /settings/notifications`, settings import); contract starter `name` and
  the top-level `name` of an uploaded, from-template or from-version document (a draft save only when the
  name changes); contract template `name` (create and draft save); subscriber card `name` (create, PATCH);
  rule-set `name`. Not covered: card contact names and Entra display names (directory data), email template
  subject and body (message text), rule keys and slugs (own patterns).

## Browser session cookie

Identity can keep a browser signed in without page JavaScript ever reading the refresh token. The mode is opt-in; clients that do not send the header get
exactly the body behaviour of `login`, `refresh` and `logout` and never a `Set-Cookie`.

- **Opt-in header.** `X-Token-Delivery: cookie` on `POST /auth/login` or `POST /auth/refresh`
  answers with the usual token body minus `refresh_token`, and sets
  `__Secure-dc_refresh=<token>; HttpOnly; Secure; SameSite=Strict; Path=/api/v1/identity/auth;
  Max-Age=<refresh token lifetime>`. Each rotation sets the cookie again. A restricted bootstrap
  login (`password:rotate`) never gets a refresh token or a cookie, with or without the header. Any
  other value of the header is 422 `validation-error`.
- **One token source.** `POST /auth/refresh` and `POST /auth/logout` read the refresh token from
  the body or, when the body carries none, from the cookie. A body token and a cookie together are
  422 `validation-error` (pointer `/refresh_token`). A refresh with no token anywhere is 401
  `invalid-credentials`.
- **Clearing.** Every refresh 401 on the cookie path, and the no-token 401, carries a `Set-Cookie`
  that expires the cookie (`Max-Age=0`, same attributes), so a stale cookie cannot loop. Logout
  always answers 204 (also for an unknown token) and, on the cookie path, expires the cookie.
- **CSRF, three layers, all in identity** (the gateway is replaceable and does not authenticate):
  (1) a refresh or logout that reads the cookie must carry `X-Token-Delivery: cookie`, a custom
  header a cross-origin script cannot send without a CORS preflight the gateway does not allow;
  (2) every request that carries that header, login included, is refused when `Sec-Fetch-Site` is
  present and is neither `same-origin` nor `none`; (3) the cookie is `SameSite=Strict`. A refused
  request is 403 `csrf-check-failed`.
- **Reuse detection is unchanged.** Presenting an already rotated cookie value revokes the whole
  token family exactly as the body form does (401, one `session.reuse_detected` event).

## Caching

Every JSON and problem answer a service sends carries `Cache-Control: no-store`, set by the
service itself (dc-core's `SecurityHeadersMiddleware`, next to `X-Content-Type-Options: nosniff`),
so a browser or proxy keeps no copy of a token, a list or an error. A route that documents its
own `Cache-Control` keeps it, because the middleware only fills the header when it is absent:

| Route | Value | Why |
|-------|-------|-----|
| `GET /.well-known/jwks.json` (identity) | `public, max-age=300` | verifiers cache the signing keys |
| `GET /api/v1/<service>/version` | `public, max-age=60` | the UI shows the build |
| `GET /api/v1/identity/settings/public` | `private, max-age=30` | public settings, per browser |
| contract and draft document fetches and validation reports (ETag) | `private, no-cache` | consumers use conditional GET; the answer is revalidated every time and never shared |
| `GET /api/v1/<service>/openapi.json` | none | a public document, left cacheable |

The UI's nginx and the api-docs server serve static files, not JSON API answers, and keep their
own headers.

## Gateway-generated errors

Every error the gateway generates for a well-formed request is RFC 9457 `application/problem+json`
(no waiver). The gateway stays thin and replaceable, so this is done with configuration
plus one tiny stdlib-only responder (`gateway/problem-responder/responder.py`), not with
gateway-side logic:

- **Unrouted paths.** Each module has one trailing-slash `PathPrefix` router
  (`/api/v1/identity/`, `/api/v1/contracts/`, ...), so `/api/v1/identity-evil/` matches nothing. A
  priority-1 catch-all router answers everything else with `404 not-found`: `/internal/...`,
  `/healthz`, `/readyz`, `/.well-known/...`, unknown modules and sanitized dot-segment paths. The
  catch-all's only target is the problem responder; it can never reach an application service.
- **413, 429 and 5xx.** The `gateway-problems` errors middleware sits outside the body limit and
  the rate limiters and re-renders statuses `413`, `429` and `500-599` through the responder, with
  the slugs `payload-too-large`, `too-many-requests`, `internal-error`, `bad-gateway`,
  `service-unavailable`, `gateway-timeout` and `gateway-error` (any other status in range).
- **Everything else passes through unchanged.** Problems a service renders with its own detail
  (400, 401, 403, 404, 405, 406, 409, 415, 422) are not intercepted.

Consequences clients should know about (proven by `tests/e2e/test_gateway.py`):

- The middleware cannot tell whether a 413, 429 or 5xx came from the gateway or from a service, so
  both are re-rendered with a generic `detail`. Upstream headers are dropped, including
  `Retry-After` on a throttled login and the service's `X-Request-ID` response header. Callers
  that talk to a service directly (inside the compose network) still see the service's own
  response.
- Send your own `X-Request-ID`: the responder echoes a well-formed one as `request_id` in the
  problem body. It reflects nothing else from the request.
- For a body above the 2 MiB gateway cap Traefik answers 413 after the first few hundred KiB and
  resets the connection while the client is still sending. A client that finishes writing before
  it reads may see a connection error instead of the problem; a client that reads while it sends
  gets the 413.
- Gateway-generated `application/problem+json` carries CORS headers, so a browser client allowed
  by the CORS allow-list can read it.

**Accepted exception.** The one class no configuration can cover: requests
that are not valid HTTP/URI syntax (malformed %-encoding such as `/api/v1/identity/%zz`, oversized
headers, CONNECT or opaque targets). Traefik refuses them before routing with its own empty
400/431/501 (plain text, `Connection: close`), so they are not problem+json. They never reach a
service. Every well-formed request gets problem+json.

## OpenAPI documents

Each service serves its own OpenAPI 3.1 document at `/api/v1/<module>/openapi.json`
(`identity`, `contracts`, `validation`, `audit`, `mail`). One post-processor in `dc_core`
(`dc_core.openapi.build_openapi`) generates all of them the same way:

- **Coverage.** Every public route appears. Internal routes (`/internal/v1/...`) and the health
  routes (`/healthz`, `/readyz`) never do. A service with no public route (mail) serves a valid
  document with an empty `paths` object and the shared components.
- **Tags.** Each operation carries at least one tag: the module name unless the route sets a finer
  one. Every `operationId` is unique across all services.
- **Security.** `components.securitySchemes.bearerAuth` is `{"type": "http", "scheme": "bearer"}`.
  The bearer is a JWT, a PAT or a service-account key (`dcp_...`). Routes declared `public()`
  (login, refresh, logout) carry `security: []`; every other operation carries
  `security: [{"bearerAuth": []}]`. The declaration is derived from the route's own policy, never
  written by hand.
- **Errors.** Every 4xx and 5xx response is documented as `application/problem+json` with
  `$ref: "#/components/schemas/Problem"` (`type`, `title`, `status`, `detail`, `instance`,
  `request_id`, `errors[{pointer, detail}]`; additional properties such as `report`, `run_id` and
  `reasons` allowed). FastAPI's `HTTPValidationError` schema is not used: a 422 is a problem body.
  The `Problem` component is byte-identical in every service document, so the merged document
  holds one copy.
- **Statuses.** Every operation declares 400 and 429; every authenticated one 401, 403 and 503;
  every operation with a path parameter 404; with parameters or a body 422; with a body 413.
  Route-specific statuses (409, 412, 428, 406, ...) are declared by the route with their own
  description. No operation has a `default` response.
- **Merged document.** The gateway's `api-docs` container fetches the four
  service documents (identity, contracts, validation, audit) at start, merges them and serves the
  result at `GET /api/v1/openapi.json`, with Swagger UI at `/api/v1/docs/`. Identical components
  are kept once; the same schema name with different content, a path or `operationId` declared
  by two services, or a path outside its service's `/api/v1/<module>/` prefix is a collision: the
  container stays unhealthy, its log names both owners, and the document answers 503
  problem+json, never a partial document. Model names are module-unique
  (`AuditEventOut`, `ContractVersionOut`, ...) so collisions do not happen in practice.
- **Conformance.** The merged document is checked against the running stack by
  Schemathesis (`tests/e2e/test_phase7_schemathesis.py`: no server error, documented status codes,
  content types and response schemas for every operation), and every public `operationId` must be
  exercised successfully (a 2xx or 3xx answer, never only refusals) by an e2e test or listed with
  a reason in `tests/e2e/operation_exemptions.json` (the operation-coverage gate,
  `tests/e2e/test_zz_operation_coverage.py`).

## Document representations

`GET /api/v1/contracts/contracts/{contract_id}/document` returns the submitted bytes with the
submitted media type by default. That covers an absent `Accept`, `*/*`, and any type the
document is already stored as (every YAML alias, `application/yaml`, `application/x-yaml` and
`text/yaml`, matches a YAML document). `Accept: application/json` on a YAML document, or
`Accept: application/yaml` on a JSON document, returns a converted rendering instead: it is
produced by re-parsing the stored bytes (original key order, YAML 1.1 look-alikes such as `NO`
stay strings), so it is equal as data but not byte-faithful, and comments and formatting are not
carried over. Quality values are honoured and a tie goes to the original. An `Accept` that names
no representation the service can produce is `406 not-acceptable`, answered only after
authorization. Every response carries `Vary: Accept`.

A contract created from a template (`POST .../contracts/from-template`) is the one place a stored
document is not the submitted bytes: it is stored as JSON, re-serialised from the template's
parsed document with the new `id` and any permitted overrides applied. YAML comments and
formatting are not carried over. Later template edits, publishes or deprecations never
change a contract already created from it. Template drafts follow last-write-wins: a
`PUT .../draft` replaces the draft with no version check, and unchanged bytes record nothing.
Published template versions are immutable raw bytes.

## Deny semantics

- 401 `unauthenticated`: no or invalid credentials.
- 404 `not-found`: the caller cannot read the target object. The body is identical whether
  the object exists or not (no existence leak). "Read" is the permission named in the route's
  `visible_via`: usually `folder:read` at the target folder, and for contract and template reads
  the read permission itself (`contract:read`, `contract:read-metadata`, `contract:view`,
  `contract:read-published`, `template:read-published`, `template:read`), so a denied read is always 404.
- 403 `forbidden`: the caller can read the target but is not permitted to perform the
  action.
- List and search endpoints (`folder:list`, `contract:list`, `template:list`, `group:list`,
  `audit:list`) have no
  target object and are answered for every authenticated caller. They return only the items the
  caller can see: an empty 200 page when nothing is visible, never a 403 and never a count or
  total that leaks existence.
- Metadata-only callers: a caller who holds `contract:read-metadata` but not `contract:read`
  (a Policy Manager without a contract role) gets the metadata with the content-derived fields
  (`api_version`, `domain`, `tags`, `media_type`, `content_sha256`, `size_bytes`) null and
  `content_redacted` true, and `404` on the document. Template reads follow the same pattern with
  `draft_redacted` for callers holding `template:read-published` without `template:read`; such a
  caller also sees `name`, `updated_by` and `updated_at` of the latest published version rather
  than those of the unpublished draft.
- Published-only callers: a caller who holds `contract:read-published` without
  `contract:read-metadata` or `contract:read` (a Consumer) is admitted to `GET /contracts/{id}`
  by `contract:view` and gets the Latest only. A draft-only contract answers the same 404 as an
  unknown contract on that route and on the drafts routes, and the list omits it.
- Accepted exception: `GET .../grantable-groups` lists the ROOT-owned and the Organization-owned
  groups of the folder to any caller who holds `grant:manage` there, including a Team admin who
  has no `group:read` at the owning Organization (their `GET /groups/{id}` is 404 and `GET /groups`
  omits them). This is deliberate: the grant route accepts any group owned by an ancestor of the
  folder, so the list is how a grant manager discovers the groups they may grant. It exposes the
  group id, owner folder id and name, never members.
- Capabilities only inform the UI. `GET /me` and `GET /me/capabilities` tell a client
  what the Policy Decision Point would answer, with the same narrowing, so a UI never keeps a copy
  of the role matrix. The answer is never an authorization: every write is checked again by the
  server and can still answer 403 or 404, for example after a grant is revoked.
- Routes whose permission is evaluated on the parent (`folder:set-block`) or at ROOT
  (`user:create`, `user:read`) are noted under "Route rules". The parent rule can answer ALLOW
  to a caller who cannot read the target (an Organization admin whose grants a Team's own block
  flag cuts), so `PUT .../block-inheritance` returns only `{id, block_inheritance}` to such a
  caller, and `effective-access` names the cutting folder (`inheritance_blocked_at`) only when
  the caller can read it; `inheritance_blocked` reports the cut either way. Neither response may
  expose more than the `GET` of the same folder would.

## Roles and inheritance

The role names, the folder kinds each role can be granted at, the permission registry and the
role-to-permission map are data in `shared/dc-auth/src/dc_auth/roles.py`. That file is the source
of truth; this document does not copy the matrix. Role names appear in the grant API and are
one-way once shipped. A grant at a folder applies to that folder and everything below it, and
grants are additive: access is the union of every grant the caller holds directly or through a
group, with no per-principal deny rules.

**Block inheritance.** An Organization or Team folder can carry a block flag. A block on
folder F cuts the grants held on F's strict ancestors from applying at F or below; grants made at F
or below keep applying. A Global admin is exempt. The flag affects access grants only: template
visibility and inherited mandatory rules flow through a block. The flag is
set with `PUT .../block-inheritance`, whose permission is evaluated on the parent, so the admin who
can clear it is never locked out by it.

**Folder deletion.** `folder:delete` is held by exactly the roles that hold
`folder:archive`: the Global admin. Organization, Project and Team folders can be deleted; ROOT never
(422 at the API, a trigger in the database). Only **live** things block (`folder-not-empty`
`blockers`): subfolders (archived ones too), contracts that are not soft-deleted in any lifecycle
state (drafts included), templates, subscriber cards that are not archived, rule sets that are
**in use**, and the local groups, service accounts and Entra registrations the folder owns. A rule
set is in use when it is mandatory; a rule set can only be attached to or pinned by contracts in its
own folder or below, and live contracts and subfolders already block, so once they are gone no live
contract can reference the folder's rule sets (pins on soft-deleted contracts go with those
contracts). **Leftovers are purged, not blockers**: contracts that were already soft-deleted (with
their versions, references, attachments and ended pins), archived subscriber cards (with members,
notices, webhook secret and ended pins) and rule sets that are not mandatory (with every published
version) are permanently removed with the folder. A copy made from a purged contract keeps living and
loses only its origin link. The check (`deletion-check`) lists them as `purges` so the UI can say so
before the click. The order is: usage counts from contracts and validation with the caller's own
bearer, identity's own blockers, the purge in contracts and validation (idempotent, one counts-only
`folder.purged` audit event each), then identity's own transaction (grants removed, folder deleted,
`folder.deleted`). Anything unclear from contracts or validation (a timeout, an error status, a
malformed body) is 503 and the folder stays; a purge that already ran is accepted, because it only
removed things that were already deleted or unused. A contract, card or rule set created in the
folder during the few milliseconds of the round trip could be orphaned: unreachable, because the
folder no longer resolves for any permission check, and recorded in the audit trail. The immutability
guarantee of published versions is unchanged for live contracts: the delete bypass of migrations
contracts 0017 and validation 0008 is transaction-local and honoured only for rows of a
soft-deleted contract, an ended pin of an archived card or deleted contract, and a non-mandatory
folder rule set.

**The `notifier` role.** A platform role that holds only `mail:send` and nothing else. It is granted at ROOT only and only by a Global admin (no admin below can grant it), and it is provisioned by identity's bootstrap for the service account `contracts-notifier`, whose `dcp_` key reaches the contracts delivery worker as the Docker secret `mail_api_key`. The role cannot read or write a contract, a card or a folder.

**Rule sets and validation.** Eight permissions are registered as data in `dc_auth.roles`: `ruleset:read` (drafts), `ruleset:read-published`, `ruleset:create`, `ruleset:update`, `ruleset:publish`, `ruleset:deprecate`, `ruleset:mandate` and `contract:validate`. A Policy Manager holds all of them (and still holds none of `contract:read`, `contract:update` or `contract:publish`), and a Global admin holds them automatically. `ruleset:read-published` always goes with `folder:read`: every role that can read a folder can read the published rules that apply there, so an editor can read the rule that blocks them. `contract:validate` goes with `contract:read` (readers, editors and admins at every level) and the Policy Manager; a Consumer and the notifier do not hold it. `contract:validate` is read-class, so a read-only personal access token may validate; `ruleset:mandate` is a write that is not interactive-only, so a Policy Manager's pipeline may switch enforcement. Grants inherit downward only, so a Policy Manager at an Organization manages and mandates rule sets there and below and is refused at its parent and at a sibling Organization: a lower level can never edit or weaken a higher level's rule set. Mandatory rule sets flow through block flags; only access grants are cut by one. Two read-class rule-impact permissions complete the set: `ruleset:dry-run` (Policy Manager and Global admin; visible via `ruleset:read`) and `governance:drift-read` (Policy Manager, Org admin and Global admin; visible via `folder:read`). Neither gives any contract content: impact results carry ids, counts, rule keys and fixed texts, and JSON Pointers only where the caller also holds `contract:read` (a Policy Manager never does).

**Audit query.** `audit:list` is a principal-level read permission that gates `GET /api/v1/audit/events` and `GET /api/v1/audit/events/{event_id}` for any authenticated caller. `audit:query` is a folder-evaluated read permission that is never a route gate: the audit service asks `accessible_folders` for it and shows the events of those folders. It is held by Global admin, Global reader, Org admin, Project admin and Team admin, and by no other role (Org reader, Team reader, editors, Policy Manager, Consumer and the notifier do not hold it; a Policy Manager stays out of the trail, consistent with the metadata-only rule in "Deny semantics"). It inherits down and is cut by block flags like every other permission. The root `audit:read` (Global admin only) stays on the internal read `GET /internal/v1/events`. The section on audit visibility below has the full rule.

**Grant ceiling.** An admin may grant and revoke roles only inside their own
subtree, at their own level and below, and never their own admin role; only a Global admin grants
Global roles and Org admin. The check uses the granter's effective roles at the target folder
(block flags included). Granting to yourself is refused (`self-grant-forbidden`), also via a group
you belong to, and adding any member to a group counts as granting every role the group holds, so
the actor must be within the ceiling for each. At least one local user must hold Global admin by a
direct grant (`last-global-admin`). Authorization writes serialise on one advisory lock.

## Rule sets and the publish gate

Policy Managers author versioned, declarative rule sets, and those rule sets gate publishing.
No contract is published while it fails its ODCS schema, an **enforced mandatory** rule or an
attached **error-severity** rule; a warning-mode mandatory version and warning or info findings are
reported but never block. How the pieces fit, with the section that specifies each:

| Piece | What it is | Where |
|-------|------------|-------|
| Rule sets | A draft plus immutable numbered versions of CEL rules over JSONPath targets, authored at any folder level | "Rule sets" |
| Mandatory and enforcement | A flag on the set applies it to the folder and everything below; each version warns for `DC_MANDATORY_WARN_DAYS` (default 14) and then blocks, or at once through `enforce-now` (any holder of `ruleset:mandate` may also move the date either way with `enforce-at`); there is no override below, and only a Global admin or Global Policy Manager can pause a platform set | "Mandatory rule sets and enforcement" |
| Runs and reports | One report shape for save, publish and on-demand validation; every finding names its rule, rule-set version, folder and JSON Pointer and never quotes the document | "Validation runs and reports" |
| The platform rule set | `odcs-structural`, platform scope, enforced from version 1 | "The platform rule set `odcs-structural`" |
| On-demand validation | Upload a document to a folder, optionally trying extra rule sets, nothing stored | "Validate on demand" |
| The publish gate and pins | Publish runs validation with the caller's own bearer; a version records the run and the rule-set versions it was validated against | "Versions and lifecycle": "The publish gate" |
| Attachments | A contract owner attaches extra published sets to one contract; a change during a publish is 409 `rules-changed` | "Versions and lifecycle": "Attached rule sets" |
| Validate on save | Six draft-producing routes embed the advisory report, and a stored draft is validated on demand | "Versions and lifecycle": "Validation on save" |

**The platform rule set (`odcs-structural`).** Besides the official ODCS JSON Schema, every run
applies the platform's own structural rules as the rule set `odcs-structural`: it has `platform`
scope (no folder), is mandatory and is enforced from version 1 for every folder, so every report
lists it and every published version pins it. It is not editable through the API: with no folder to
hold a permission, every by-id authoring route answers the uniform 404, and a change ships as a new
platform version in a migration (see "The platform rule set `odcs-structural`").

**Fail-closed.** A rule that errors, times out, exceeds its memory or budget, or cannot be resolved
is its own outcome and blocks wherever a failure would block. A validation service that cannot
answer makes publishing 503 `validation-unavailable`; a draft save still succeeds and reports
`validation` as `{"status": "unavailable"}`. Publishing never passes without a verdict.

## Semantic checks and rule impact

Publishing also catches breaking changes and semantic inconsistencies, not only structural
errors, and lets Policy Managers see a rule's impact before and after it enforces. Nothing here adds
an override: no field, header, setting or role skips the compatibility gate or lowers a mandatory
set. How the pieces fit, with the section that specifies each:

| Piece | What it is | Where |
|-------|------------|-------|
| Breaking-change classifier | `dc_odcs.diff` (`classifier_version` `odcs-compat-1`) compares two ODCS documents by element name, so reordering is no change, and classifies each change as breaking, warning or info; descriptions and other free text never enter a change | "Validation runs and reports": "Compatibility and the native findings" |
| The compatibility gate | Every run against the previous published version carries a `compatibility` section; a breaking change without a declared major above the stable major floor is the native blocking finding `semver-breaking-change`, so publish answers 422 `contract-validation-failed`; an unusable baseline fails closed in publish mode (`compatibility-unavailable`) | "Validation runs and reports": "Compatibility and the native findings" |
| Baseline under the lock | Contracts sends the baseline, the floor and caller-scoped link facts with every run and re-checks the baseline under the publish lock: 409 `baseline-changed` | "Versions and lifecycle": "Publish compatibility" |
| Reference facts and cel-2 | Before rules run, elements, servers, contract links, people and teams are resolved into the tri-state `refs` binding; `cel-2` rules also read `previous` and `changes`; published `cel-1` versions evaluate as before | "Validation runs and reports": "Reference facts" |
| Seeded semantic sets | `odcs-consistency` and `odcs-references`, platform scope, mandatory, in warn grace for `DC_MANDATORY_WARN_DAYS` after migration 0006 (set the variable on `validation-migrate` too); a Global Policy Manager can enforce them early | "Mandatory rule sets and enforcement" |
| Publication warning | `odcs-publication` (platform scope, mandatory, warn grace like the semantic sets, migration 0009) holds the warning `published-status-active`: a version published with an ODCS `status` other than `active` carries one non-blocking finding in its stored report; it never blocks, before or after `enforce_at`; draft saves and validations never show it | "Mandatory rule sets and enforcement"; "The `ctx.operation` binding" |
| Stored report and changelog | `GET .../versions/{ref}/validation-report` returns the report a version was published with; `GET .../contracts/{contract_id}/diff?from=&to=` classifies the changes between any two readable versions or drafts on the fly | "Versions and lifecycle": "Stored validation reports", "Changelog between two versions" |
| Rule impact | `POST /api/v1/contracts/governance/rule-set-dry-runs` (would a rule-set version, published or draft, block the published contracts in scope) and `GET /api/v1/contracts/governance/drift?folder_id=` (which published contracts fail the rules now in force); paged, content-free, no state change | "Versions and lifecycle": "Rule impact: dry-runs and compliance drift" |
| Breaking-change notices | A card pinned to a version the new publish breaks also gets `contract.version.breaking_published`, listing the breaking changes per pinned version | "Subscriber cards": "Breaking-change notices" |

The journeys `tests/e2e/test_phase6_semantic_journey.py` and
`tests/e2e/test_phase6_rule_impact_journey.py` walk each piece through the gateway.

## Audit trail and the API surface

The platform answers "who changed, read or was refused what, and when", and makes the
whole API discoverable at the gateway. Where each part is specified:

- **Reading the trail:** the `GET /api/v1/audit/events` and `GET /api/v1/audit/events/{event_id}`
  notes under "Route rules", the `audit:list` / `audit:query` paragraph in "Roles and inheritance",
  and "Audit visibility" below (visibility, ordering, fail closed).
- **Change events:** one per business write, as before. A publish the gate refuses is recorded as
  `contract.version.publish_refused` ("The publish gate" in "Versions and lifecycle").
- **API documentation:** `GET /api/v1/openapi.json` and `/api/v1/docs/` ("Route rules") and "OpenAPI
  documents" (how every service document is generated, merged and checked).
- **Every principal type:** a local or Entra user's JWT, an Entra service principal, a
  personal access token (read-only ones on reads) and a service-account key can call every
  operation their grants allow; interactive-only permissions refuse a PAT, a key or a service
  principal with 403
  `interactive-session-required`. `tests/e2e/test_phase7_principal_matrix.py` proves it
  area by area.

**What a client sees as a denial in the trail.** Identity's PDP records denials (category `denial`,
`aggregate_type` `authz`, action `check.denied`, `before`/`after` null):

- Every permission check whose outcome is not ALLOW is exactly one denial event: a 404 because
  the target is not visible (`detail.outcome` `not_found`), a 403 (`forbidden`), a gate refusal of
  an inactive, stale or unregistered principal (`gate_refused`) and the interactive-only refusal
  (`interactive_required`). In-handler probe checks count too (validation's batch folder facts,
  contracts' source checks, subscription reads, and the card `folder:read` check of a caller who
  is not a linked member), so one request can record more than one denial. A linked card member
  without a folder role is answered by membership without the `folder:read` check, so a member's
  successful card, notice or subscription read records no denial; a member's refused
  card write still makes the check and records one `folder:read` `not_found` denial.
- A refusal that a route derives from the permissions its decision returned is recorded too: the
  service asks the PDP for the decisive permission at the same folder before it answers the
  uniform 404 (`dc_auth.deps.recorded_refusal`), so the row names that permission. This covers a
  published-only reader asking for a contract without a Latest, its versions list or its metadata
  (`contract:read`, `contract:read-metadata`), an unpublished version by fetch, in a diff or as a
  create-from-version source (`contract:publish`), a draft id in a diff (`contract:read`), a
  never-published template (`template:read`) and a draft dry-run without `ruleset:read`
  (validation). The answer stays the uniform 404, and an unrecordable denial is 503. One
  exception: refusing to pin an unpublished version (subscribe and move) is decided under the
  contract lock, where no identity call is made, so that 404 has no denial row.
- `detail` holds exactly `permission`, `outcome`, `service` (the service that asked) and `route`
  (the route template, for example `/api/v1/contracts/contracts/{contract_id}/drafts`, never a
  raw URL or query string). `aggregate_id` is the target folder id (`root` for a root permission,
  `none` without a target) and `scope_path` is that folder's chain, so the denial is visible to
  the auditors of the folder that was protected, not to the refused caller's own admins. A denial
  with no known folder has an empty `scope_path` and is visible only at ROOT.
- `request_id` is the caller's own `X-Request-ID` (a UUID4) on identity's routes and on every
  other service, which forwards it to identity with the check. Send one to find your request's
  denial.
- A gate refusal on the remote path appears once, as permission `authz:check`, service `identity`,
  route `/internal/v1/authz/check`, because identity refuses the forwarded principal before it
  reads the checked permission; the forwarded request id still links it to the caller's request.
- A denial that cannot be written turns the request into 503 `service-unavailable`: a refused
  request is never answered without its record. A directory outage (gate 503) is unavailability,
  not a denial, and records nothing.
- Not denials: trimming a list or search to what the caller can see (an empty page is not a
  refusal), a 401 for a missing, invalid, expired or revoked token (authentication fails before any check;
  a deactivated principal's token is a gate refusal and is recorded) and `require()`'s
  `password-rotation-required` refusal of a bootstrap session. That refusal is by design: it is
  decided from the token's `restricted` session before any permission is checked, it only means
  "rotate your password first" for the one bootstrap account (whose rotation is in the trail as
  `user.password_rotated`), and a restricted session holds no authority it could probe with.

**Access events.** Every 200 that returns contract content (`.../versions/{ref}` including
converted renderings, `.../document`, `.../drafts/{draft_id}/document`) records one access event
(category `access`, action `read`, the contract as aggregate, `before`/`after` null) with `detail`
`contract_id`, `version_id` or `draft_id`, `semver`, `ref` (`latest`, the resolved SemVer, `head`
or `draft`) and `media_type`, never content. A successful create-from-version (`201` from
`.../contracts/from-version`) records one access event on the **source** contract (its id, the
copied `version_id` and SemVer with `ref` the SemVer, or the `draft_id` with `ref` `draft`), scoped
to the source folder, so the source's auditors see who copied it, including across
organizations. A 304 revalidation, any error and metadata, list, search, diff and report
reads are not access events. Access events are best effort and batched
(about a second): a broker outage can lose some, and it never fails or slows a read. "What did this
pipeline read" is `?actor_id=sa:<uuid>&category=access`.

## Audit visibility

`GET /api/v1/audit/events` answers "who changed, read or was refused what, and when".
Its parameters and errors are under "Route rules"; this section states what a caller sees.

- **One store, three categories.** `change` (business writes through each service's outbox),
  `access` (reads of contract content, best effort) and `denial` (denied permission checks,
  recorded by identity's PDP). The `category` filter selects one; without it every category is
  returned. Access and denial rows have `before` and `after` null and a small flat `detail`.
- **Visibility rule.** The audit service asks identity for
  `accessible_folders(caller, "audit:query")`. An event is visible when its own folder (the last
  element of `scope_path`) is in that scope. A scope that covers ROOT (`all`) covers every folder
  except the blocked subtrees in `excluded_subtrees`, plus any `folder_ids` added back inside
  them. The rule is one SQL predicate over the stored `leaf_folder_id` and `scope_path`, with no
  post-filtering, and it is proven equal to the access oracle by
  `audit/tests/integration/test_visibility_property.py`.
- **ROOT-only events.** Events with an empty `scope_path` (for example a personal access token or a
  probe of an unknown folder) or with `scope_path` `[root]` (users, credentials, sessions, Entra
  registrations, bootstrap) are visible only to callers who hold `audit:query` at ROOT: a Global
  admin and a Global reader. An Org admin never sees them.
- **Block flags cut audit visibility too.** A Global reader does not see the events of a blocked
  Organization or Team unless it also holds a grant inside it. A Global admin is never cut.
- **Empty scope, empty page.** A caller with no `audit:query` anywhere (a Team editor, a Policy
  Manager) gets `{"items": [], "next_cursor": null}`, never a 403. One event by id outside the
  scope is the same 404 as an unknown id.
- **Fail closed.** If identity cannot answer the scope the route answers 503
  `service-unavailable`; it never returns an unfiltered page.
- **Snapshots as stored.** `before`, `after`, `changes` and `detail` are returned exactly
  as the store holds them. They are metadata-only by construction, so the query adds no
  redaction.
- **Ordering and late arrivals.** Pages are newest first by `(occurred_at, id)` with an opaque
  keyset cursor. Ordering is by occurrence time on purpose: an event held in a producer's outbox
  during a broker outage arrives later with its original `occurred_at` and can land behind a page
  a client has already read. Poll a window again (for example `from` a little before the last
  seen `occurred_at`) when completeness matters.
- **Who may call.** Every principal type: local and Entra users, Entra service principals, personal
  access tokens (read-only ones included) and service-account keys. Typical calls:
  `?aggregate_type=contract&aggregate_id=<id>&from=<t-7d>` (who touched this contract last
  week) and `?actor_id=sa:<uuid>&category=access` (what did this pipeline read).

## Rule sets

A rule set is one editable **draft** plus immutable numbered **versions**. A Policy Manager creates it
in a folder they manage (any level), saves the draft under `If-Match` and publishes it; the draft's
`ETag` is `"r<draft_revision>"` and every save that changes it bumps the revision. Rules have no ids
of their own: each has a `key` that is stable inside the set, and the set is versioned as a whole.

**The check report.** `PUT .../draft` (advisory), `POST .../draft/check` and a refused publish all
carry the same shape:

```json
{
  "ok": false,
  "rule_set_problems": [{"code": "...", "detail": "..."}],
  "rules": [
    {"rule_key": "unique-property-names", "ok": false,
     "problems": [{"rule_key": "unique-property-names", "code": "fixture_misbehaved",
                   "detail": "...", "fixture_kind": "fail", "fixture_index": 0,
                   "error_code": null}]}
  ]
}
```

`code` names the problem (`compile_error`, `fixture_misbehaved`, `missing_fail_fixture`, and so on);
`detail` is a short message; `fixture_kind` (`pass` or `fail`) and `fixture_index` locate a fixture
problem; `error_code` is the engine's error code when a fixture raised an evaluation error. Absent
keys are omitted. The report never contains a rule's condition or a fixture document.

**Which refusal slug.** Publishing is refused with 422 when `ok` is false. The slug is
`ruleset-fixtures-failed` only when **every** problem found is a fixture problem (the codes in
`dc_rules.checks.FIXTURE_CODES`: a missing pass or fail fixture, too many or too large fixtures, or
a fixture that does not evaluate as declared); one compile error, size or scale limit, empty set or
duplicate key among them makes the whole refusal `rule-invalid`. The report is the response's
`check` member in both cases, and no version row is written.

**Draft saves never fail on rule problems.** A draft with a syntax error saves (200) and returns
`check.ok` false; only a body of the wrong shape (unknown fields, a missing `key`) is 422. When the
sandbox is saturated the save still lands and `check` is `{"status": "unavailable"}`.

**Idempotent re-publish.** A draft whose canonical content hash equals the newest version's is not
published again: the same version is returned with 200, no row and no event. Publishing twice in a
row, or retrying after a lost response, is therefore safe. A draft changed between the check and the
lock makes the publish 412 (never a publish of unchecked rules).

**Who sees which rules.** Drafts are for authors only: every route under `/rule-sets/{id}` and
`/folders/{id}/rule-sets` needs `ruleset:read` (Policy Manager and admins at the holder folder or
above). Published versions are readable by anyone who holds `ruleset:read-published` at the rule set's
folder **or any folder below it**, but only through their own folder: `GET
/folders/{folder_id}/available-rule-sets...` checks the permission at the folder in the path and
returns the sets that folder, its ancestors or the platform hold, so nothing has to compute
descendants and a sibling branch never matches. This is deliberate: rule content is governance data,
not contract content, and a contract editor must be able to read the rule that blocks them.

**Overload.** The sandbox is a bounded pool. When no slot frees up within the queue wait, publish and
`draft/check` answer 503 `validation-unavailable` with `Retry-After: 1`; a check is never a pass.

## Mandatory rule sets and enforcement

**The flag is on the rule set.** A Policy Manager (or Global admin) at the rule set's folder or above
sets it with `PUT /rule-sets/{id}/mandatory`. A mandatory set applies to its folder and to every
folder below it. The set is found through the structural `ancestor_ids` identity reports for the
folder, so a block-inheritance flag, which cuts access grants only, never stops a mandatory set. There is no override, exemption, severity lowering or per-contract bypass: no route
or setting exists for a lower level, or a contract owner, to remove, pause or downgrade the set. The one exception is deliberate and audited: a Global admin or Global Policy Manager may
pause a platform set and any holder of `ruleset:mandate` at the set's level or above may reschedule
a version's date (see below). A lower level can only add rule sets.

**Mode is computed, never scheduled.** Each published version of a mandatory set carries an
`enforce_at`. A version is `enforce` when `enforce_at` is at or before the database's now (one
`SELECT now()` per read; nothing the client sends is used) and `warn` before that; a version with no
`enforce_at` does not apply. The grace is `DC_MANDATORY_WARN_DAYS` (default 14, `0` enforces at
once, a negative value is refused at start-up). Setting the flag stamps the newest published version
`now + grace`; publishing a new version on a mandatory set stamps that version the same way in the
publish transaction; `POST .../versions/{n}/enforce-now` moves a warning version's date to now and
is a no-op on one that is already enforced. **Reschedule:** `PUT
.../versions/{n}/enforce-at` (folder-scoped sets) and `PUT /folders/{root_folder_id}/platform-rule-sets/{id}/versions/{n}/enforce-at`
(platform sets) move a version's date to any timestamp, either way: any holder of `ruleset:mandate`
at the set's level or above may do it. A future date warns until then, now or a past date enforces
at once (a past date is stored as the database's now), and each change is audited as
`ruleset.enforcement_rescheduled`. Dates move either way: an earlier rule that let them move only earlier was dropped.

**Pausing a platform rule set.** `PUT /folders/{root_folder_id}/platform-rule-sets/{id}/paused`
with `{"paused": true}` (a Global admin or Global Policy Manager; the folder in the path must be the
ROOT folder) makes a platform set **warn only**. It still runs on every save, validation and
publish and its findings still appear, but every one is `mode: "warn"`, `paused: true`,
`blocking: false` with `enforce_at: null` (a paused set has no effective date), so it never blocks
publishing. `paused: true` also appears on the matching `rule_sets` entry of the report, on the
run's pins and on `available-rule-sets` (`paused`, `paused_at`, `resolved[].paused`). A published
contract version's `pinned_rule_sets` entry carries `"paused": true` for a pin taken while the set
was paused (the key is absent otherwise, so stored history is unchanged), and a drift report shows a
paused set's versions as `warn`. Resume (`{"paused": false}`) restores the computed mode exactly:
the pause never touches `enforce_at`, so an enforced version blocks again at once and a version
still ahead keeps warning until its date. Pause and resume are each one audited write
(`ruleset.paused`, `ruleset.resumed`, with before and after and the actor); repeating the current
state is a 200 no-op without an event. Only platform sets can be paused (a database CHECK enforces
it); folder-owned sets keep the clear/set break-glass below. This is the one change to a platform set the API allows; everything else about a platform set
ships as a migration.

**Seeded semantic platform sets start in warn grace.** Migration
`0006_seed_semantic_rule_sets` seeds `odcs-consistency` and `odcs-references` (cel-2, mandatory,
platform scope) with `enforce_at = migration time + DC_MANDATORY_WARN_DAYS`, so a contract that
breaks one of their rules gets a non-blocking `warn` finding until the grace ends. Platform sets
hold no folder, so a Global Policy Manager (or Global admin) enforces one early with
`POST /api/v1/validation/folders/{root_folder_id}/platform-rule-sets/{rule_set_id}/versions/{n}/enforce-now`;
the folder in the path must be the ROOT folder. If `DC_MANDATORY_WARN_DAYS` is ever set for the
validation service it must be set on `validation-migrate` too, because the date is stamped by the
migration.

**The publication warning never blocks.** Migration
`0009_seed_publication_rule_set` seeds `odcs-publication` (cel-2, mandatory, platform scope, same
warn grace and `DC_MANDATORY_WARN_DAYS` rule as above) with one rule, `published-status-active`,
severity `warning`: it fires only in a publish-mode run whose document's ODCS `status` is not the
string `active` (`draft`, `proposed`, `deprecated`, absent or any other type). The platform never
rewrites the author's `status`; this only tells consumers-to-be. It covers a first publish (Latest)
and a later one (Preview) alike, because promote never re-validates and a published document is
immutable, so the publish run is the only moment to warn. A finding is blocking only when its
severity is `error` and its set is enforced, so this finding stays `blocking: false` and the
report `publishable: true` in warn grace, after `enforce-now` or a past `enforce-at` (mode
`enforce`) and while paused. The ROOT pause and `enforce-at` routes work as for the other platform
sets. The finding's message is fixed text and never quotes the document.

**What applies.** Per mandatory set the enforced version is the newest one whose `enforce_at`
has passed; it can block. Every version newer than that one (every version when none is enforced)
whose `enforce_at` is still ahead is reported too, each as its own non-blocking `warn` entry, so a
rollout never skips an intermediate version and governance never drops while a new version warns.
Once a newer version is enforced, older ones drop out. A deprecated version stays in while it is the
enforced one or inside its warn window. `GET /folders/{id}/available-rule-sets` shows `mandatory` and
the resolved `[{version_no, mode, enforce_at}]` entries at that folder.

**Clear is the audited break-glass; set again never lowers governance.** Clearing the
flag takes effect at once: it records which version was enforced at that moment
(`cleared_enforced_version_no`) and nulls `enforce_at` on every version, in one audited write
(`ruleset.mandatory_cleared`, naming that version and the versions that were still warning). Setting
the flag again restores that version as enforced immediately and gives only the versions newer than
it a new grace period (`ruleset.mandatory_set` carries `restored_version_no`); if nothing was
enforced when it was cleared, the set behaves like a first-time set. Setting a flag that is already
set changes nothing, so clear then set is the only way to a new grace period, and it never weakens
what was enforced.

## Validation runs and reports

**The internal run route.** `POST /internal/v1/validation/folders/{folder_id}/runs` is how the
contracts service asks validation to check one document. It is on the compose network only: it is
never in OpenAPI and the gateway does not route it. The caller forwards the **end user's own
bearer token**, and validation checks `contract:validate` itself at `{folder_id}` through identity's
PDP. The method is POST, so that decision is fresh (never served from the cache), in save mode as
well as publish mode. The folder's ancestry and context come from the PDP's folder facts and never
from the body, so a caller cannot weaken governance by sending its own ancestor list. Validation is
a pure function of the request: it re-parses the document bytes itself and never calls the contracts
service. The body is read, capped at about 2.8 MB (two documents, base64 encoded, plus the
envelope), only after the policy has passed.

```json
{"mode": "publish", "contract_id": "...", "draft_id": "...", "media_type": "application/yaml",
 "document_b64": "...", "attachments": [{"rule_set_id": "...", "version": "latest"}],
 "attachments_revision": 3}
```

`mode` is `save` or `publish`; publish needs `contract_id` and `draft_id` (422 `validation-error`
with a JSON pointer otherwise). Two optional members request a compatibility check, see
"Compatibility and the native findings": `baseline` (`{"version_id": "...", "semver": "1.0.0",
"media_type": "application/yaml", "document_b64": "..."}`, the previously published version, with
`semver` optional, limited to plain version characters and at most `MAX_VERSION_CHARS` (2,048,
`dc_odcs`, shared with contracts) characters; contracts sends `null` for a longer one and validation
derives it from the baseline document, so a long published version never refuses a run) and `stable_major_floor` (an integer, the highest
stable major published so far, `null` while only prereleases exist). `version` is `"latest"` or an exact version number; at most
`DC_MAX_ATTACHMENTS` (default 50) attachments are accepted. The answer carries `run_id`, `mode`,
`content_sha256` (of the bytes validation parsed), `engine_version` (`none` when no rule ran),
`dsl_version`, `schema_sha` (the hash of the vendored ODCS schema manifest), the echoed
`attachments_revision`, `pins` (one entry per rule-set version applied: rule set, name, folder,
scope, version, `source`, `mode`, `dsl_version`, `deprecated_version`, `paused`) and the `report`. A publish
run is stored immutably and forever (`run_id` set, even when the report blocks); a save run is
never stored (`run_id` null).

**The `ctx.operation` binding.** A cel-2 rule reads why the run happens as
`ctx.operation`: `"publish"` for the publish gate, `"save"` for a draft save, a draft validate and
the public `POST /folders/{folder_id}/validate`. It is the run's server-side mode, never a caller
value. The batch dry-run and drift evaluations bind `"publish"` (they judge stored published
versions as the gate would); a cel-2 fixture binds its own `ctx.operation` or `"save"`; the
rule-set scale probe binds `"publish"`. A cel-1 rule never sees the key, so published cel-1
versions evaluate exactly as before. Guard it: `!has(ctx.operation) || ctx.operation != 'publish' || (...)`.

**The report.** One shape for every mode; the shape and the meaning of each field are in the
[API guide](../user/api-guide.md#validation-reports). The rules behind it:

`paused` (findings, `rule_sets` entries and pins) is true when the rule set is a paused platform set:
its entries are then `warn`, never blocking, and a finding's `enforce_at` is null.

Structural errors are the ODCS JSON Schema's: a JSON Pointer, the schema `keyword` and a **fixed
text per keyword** (for example `required`: "A required property is missing.", `type`: "The value
has the wrong type."); the schema library's own message is never forwarded because it embeds the
failing value. A finding's `message` is the rule author's static message (`outcome` `fail`) or a
fixed text for its `error_code` (`outcome` `error`):

| `error_code` | Fixed message |
|--------------|---------------|
| `no_such_key` | The condition referenced a field that does not exist. |
| `type_error` | The condition applied an operator or function to values of the wrong type. |
| `invalid_argument` | The condition raised an invalid argument error. |
| `budget_exceeded` | The condition exceeded the evaluation budget. |
| `memory_exceeded` | The condition exceeded the memory limit. |
| `timeout` | The rule did not finish within its time limit. |
| `not_boolean` | The condition did not produce true or false. |
| `too_many_matches` | The target matched more nodes than the limit allows. |
| `target_error` | The target could not be evaluated against the document. |
| `run_budget_exceeded` | The validation run exceeded its time budget before this rule ran. |
| `engine_error` | The rule engine reported an unexpected error. |
| `unresolvable_rule_set` | The rule set could not be resolved. |

**When a finding blocks.** `blocking` is true only for `severity: error` findings from an
**enforced mandatory** version, an **explicit** attachment, an **ad-hoc** set or a **native**
finding (see "Compatibility and the native findings"). Everything else
never blocks: warnings and info findings, and any finding of a `warn`-mode version. An evaluation
error follows the same rule as a failure, so an error in a rule that would block, blocks (fail
closed), while the same error in a warn version is reported and does not block. Every other rule
of the set is still evaluated and counted. `publishable` is
`structural.valid and blocking_count == 0`: ODCS schema violations make a run unpublishable in
every mode. `blocking_count` and `publishable` are computed before the findings cap.

**Compatibility and the native findings.** Every report carries a
`compatibility` section (it is absent only from a report stored before the compatibility check existed, which still loads).
With a `baseline` in the run request, validation diffs the document against it with the
`dc_odcs.diff` classifier, off the event loop, and the section lists the changes:

```json
{"compatibility": {
  "status": "compared", "baseline": {"version_id": "...", "semver": "1.0.0"},
  "classifier_version": "odcs-compat-1", "declared_version": "1.1.0",
  "required_bump": "major", "bump_ok": false,
  "counts": {"breaking": 1, "warning": 0, "info": 0},
  "changes": [{"code": "property-removed", "class": "breaking",
               "path": "schema.orders.properties.amount",
               "pointer_before": "/schema/0/properties/1", "pointer_after": null,
               "attribute": null, "before": null, "after": null}],
  "changes_total": 1, "changes_truncated": false}}
```

`status` is `compared`, `no-baseline` (no baseline sent: no changes, `bump_ok` true) or
`unavailable` (a baseline was sent but could not be used, below). `required_bump` is `major` for a
breaking change, `minor` for a warning-class change, `patch` for an info change and `none`;
`bump_ok` is whether `declared_version` satisfies the one enforced rule: a breaking change is
publishable only when the declared major is above `stable_major_floor`. With a `null` floor (no
stable release yet, prereleases only) any bump passes and the changes are still listed. `changes`
lists at most 200 entries; `changes_total` is the true number and `changes_truncated` is true only
when entries were cut (exactly 200 changes are all listed, 201 list 200). A change names its
element by name-keyed `path` (`schema.<table>.properties.<column>`) and JSON Pointers into the old
and new document; `before` and `after` are set only for a small allow-list of structural attributes
(`logicalType`, `physicalType`, `required`, `unique`, `primaryKey`, `primaryKeyPosition`, the
length, range and size bounds, `enum` and a server's `type`), strings cut at 200 characters, and
are `null` for everything else: descriptions, examples, quality SQL and other free text never enter
a report. `declared_version` and `baseline.semver` are shown only when they are plain version
characters, at most 2,048 (`MAX_VERSION_CHARS`).

Two **native findings** belong to the platform, not to a rule set: `source` is `native`, `mode`
`enforce`, `severity` `error`, `blocking` true, and `rule_set` has every member `null`. They are
always on: no request field, setting, header or role skips or downgrades them, and they count in
`blocking_count`, so a publish run with one is not publishable and contracts answers 422
`contract-validation-failed`. They are listed first, so the findings cap never hides them.

| `rule_key` | When | Fixed message |
|------------|------|---------------|
| `semver-breaking-change` | The changes include a breaking one and `declared_version` is not above `stable_major_floor`. Location `/version`. | A breaking change needs a new major version. The smallest passing version is `<floor + 1>.0.0`. |
| `compatibility-unavailable` | **Publish mode only**: a baseline was sent but is over `DC_MAX_DOCUMENT_BYTES`, does not parse or is not a supported ODCS document, so the gate fails closed. | The previous version could not be compared, so its compatibility cannot be checked. |

In save mode an unusable baseline is only `status: unavailable` with no finding, and a breaking
change under a minor bump does show the native finding (still `blocking` true), so an editor sees
what publish will do; save mode stores nothing. cel-2 rules in the run read the parsed baseline as
`previous` and the full classified change list (not the 200-entry cut) as `changes`; without a
usable baseline `previous` is null and `changes` is `[]`. The publish run stored in
`validation_run.report` carries the section like any report.

**Reference facts.** Before any rule runs, validation resolves what the document references into
the cel-2 `refs` binding: five string-keyed maps (`elements`, `servers`, `contracts`, `principals`,
`teams`) keyed by the reference as written (two links to the same contract that differ in text,
for example pinning two versions, are two keys). Values are `found`, `not_found` or `unverifiable`
(only `not_found` can fail a rule), except `contracts`, whose values are `{"state": "published" |
"unpublished_or_draft_only" | "not_found" | "unverifiable", "pinned_version", "pinned_published"}`.
Elements and servers are resolved against the document itself (an external file prefix is
`unverifiable`; servers are referenced by a `customProperties` entry whose `property` is `server`).
People and teams are asked of identity with the caller's token and remembered for
`DC_PRINCIPAL_CACHE_TTL_S` (45 s); `team.name` also matches the owning Team folder's slug without
asking. Contract links come only from the optional run-request member
`ref_facts` (`{"contracts": {"<link as written>": {"state": "published", "pinned_version": null,
"pinned_published": null}}}`, at most 200 entries, keys and `pinned_version` at most 2,048
characters (`MAX_VERSION_CHARS` for the pin, shared with contracts, which reports a longer pin as
`null`), unknown members 422 `validation-error`); a link without a fact is `unverifiable`, and validation never calls
contracts. Each map resolves at most `DC_MAX_REFS_PER_KIND` (200) references; the rest are
`unverifiable`. The report's optional `ref_facts` holds counts per kind and state only, never a
name.

**Attachment resolution.** An explicit attachment must name a rule set that the platform, the
folder or one of its ancestors holds and that has a published version. `"latest"` is the newest
non-deprecated version (the newest version when all are deprecated, flagged
`deprecated_version: true` in its pin); an exact version resolves even when deprecated. An
attached set that is already mandatory at the folder resolves once, as `mandatory`. An attachment
that cannot be resolved (unknown id, no published version, held in a sibling branch or below, or a
missing exact version) is **never skipped**: it becomes one blocking `error` finding with
`error_code` `unresolvable_rule_set` and `rule_key` `*`, and the run is not publishable. The
answer is the same for an unknown set and a set the caller may not see.

**Order and caps.** Rules are evaluated in a fixed order: holder depth (platform first, then ROOT
down to the folder), rule-set name (case-insensitive), then rule key, each rule on its own in the
sandbox. Findings are ordered by depth, name, rule-set id, rule key and location, with unresolvable
attachments first, so the same input gives the same bytes. A report lists at most
`DC_MAX_FINDINGS` findings (default 200; `findings_total` and `findings_truncated` say so). A rule
whose condition references `contract` is capped at `max_matches_with_contract` = 100 matches (more
is `too_many_matches`), any other rule at `max_matches` = 10,000. A saturated sandbox answers 503
`validation-unavailable` with `Retry-After: 1` (a run is never a pass); a document larger than
`DC_MAX_DOCUMENT_BYTES` (default 1048576, measured after base64 decoding) is 413
`payload-too-large`. The internal run body itself is capped at twice the base64 size of
`DC_MAX_DOCUMENT_BYTES` (document and baseline), 64 KiB of envelope and room for 200 maximal
`ref_facts` entries, so no request contracts builds is refused for its size.

**Validation's problems never quote the document.** A rejection from the ODCS ingest keeps its
status, slug, title and JSON Pointer, but validation replaces its detail with a fixed text per slug
(`unsupported-odcs-version`: "The document's apiVersion is not a supported ODCS version.";
`malformed-document`: "The document is not valid YAML or JSON."; `unsupported-media-type`: "Use
application/yaml or application/json."; `payload-too-large`: "The document exceeds the size
limit."; `validation-error`: "The document fails the ODCS acceptance check."), because the library's
own detail can echo the submitted value (for example the declared `apiVersion`). Reports and
stored runs hold locations, the authors' static messages and the texts above, never a document
value.

## The platform rule set `odcs-structural`

Every validation report covers the official ODCS JSON Schema plus the platform structural rules.
They ship as the rule set `odcs-structural`, created by validation's migration 0005 with the system
actor `system:validation-seed` and its `ruleset.version_published` outbox event. Its scope is
`platform` (no folder), it is mandatory, and version 1 is enforced from the moment it exists, so
every run reports its findings with `rule_set.scope: "platform"`, `folder_id: null`, `source:
"mandatory"` and `mode: "enforce"` and pins `odcs-structural` v1, with no setup by anyone. It is
listed first (depth 0) by `GET /folders/{folder_id}/available-rule-sets` for any folder the caller
can read. It has no folder to hold a permission, so every by-id authoring route (read, draft,
check, publish, versions, deprecate, mandatory, enforce-now, enforce-at, delete) answers the uniform
404; the three ROOT-scoped routes (`enforce-now`, `enforce-at` and `paused` under
`/folders/{root_folder_id}/platform-rule-sets/{id}`) are the only way to change its schedule. A later
platform version ships as a new migration.

Version 1 holds four rules, all guarded so that an unexpected JSON type is never an evaluation
error: `property-names-unique` (error), `schema-names-unique` (error),
`primary-key-positions-valid` (error; applies only when a primary key declares a position other than
the ODCS default -1, then every primary key needs one, unique and within 1..number of primary keys)
and `logical-type-option-bounds` (warning; `minLength <= maxLength`, `minimum <= maximum`,
`minItems <= maxItems`).

## Validate on demand

`POST /api/v1/validation/folders/{folder_id}/validate` validates an uploaded contract at a folder
without saving it. The body is the raw document (`Content-Type` `application/yaml` or
`application/json`; another type is 415 `unsupported-media-type`). The policy checks
`contract:validate` at `{folder_id}` from the path **before** the body is read, so a caller without
the permission (a Consumer, for example, gets 403 or 404) never makes the service buffer anything;
the body is then read with a cap of `DC_MAX_DOCUMENT_BYTES` (413 `payload-too-large` above it) and
parsed with the same ODCS gate as contracts (422 `malformed-document`, `unsupported-odcs-version`
or `validation-error`, with the fixed per-slug details of "Validation runs and reports").

The 200 answer is the run's answer without a run: `content_sha256`, `engine_version`,
`dsl_version`, `schema_sha`, `pins` and the same `report` as save and publish. The route uses the
run pipeline in save mode, so it writes no validation run and no event. A Policy Manager may
validate a document they upload themselves (they hold `contract:validate`), which reveals no stored
content: the report is about the uploaded bytes only. A saturated rule sandbox answers 503
`validation-unavailable` with `Retry-After: 1` instead of queueing without bound.

**Comparing with a previous document (`application/vnd.dc.validate-pair+json`).** The same route
also accepts a JSON envelope with both documents as text, so an author can see the compatibility
section before saving:

```json
{"document": "<yaml or json text>", "document_media_type": "application/yaml",
 "previous_document": "<yaml or json text>", "previous_media_type": "application/yaml"}
```

All four members are required and no others are allowed (422 `validation-error` with pointers and
fixed texts, never an echo). Each document is capped at `DC_MAX_DOCUMENT_BYTES` after decoding (413
`payload-too-large`); the envelope itself may be larger, to leave room for JSON escaping. The
answer is the usual one with `report.compatibility` against the previous document (`baseline.semver`
is the previous document's `version`, `version_id` null). With no publish history the floor is
derived from the previous document: its major version unless it is a prerelease, in which case there
is none. A previous document that does not parse is `unavailable` and advisory. A plain YAML or JSON
upload keeps working unchanged and reports `compatibility.status` `no-baseline`. The multipart form
is deliberately not offered: it needs `python-multipart`, which the platform does not ship.

**Ad-hoc rule sets (`rule_set`).** The query parameter `rule_set` adds published rule sets to try
on top of the folder's mandatory ones: `?rule_set=<id>` (its latest version) or
`?rule_set=<id>@<version number>` (exactly that version, even when deprecated), repeatable, at most
`DC_MAX_ADHOC_RULE_SETS` (default 10). A set resolves with the same visibility and version rules as
an explicit attachment (platform or the folder's own and ancestors', with a published version), its
findings carry `source` `adhoc` and its error-severity failures block as if it were attached
(`publishable` false), and the `pins` list it. A set that is already mandatory for the folder
resolves once, as `mandatory`. A value that is not `<uuid>` or `<uuid>@<n>` (n from 1 to
2147483647), or more values than the cap, is 422 `validation-error` with a pointer such as
`/query/rule_set/0` and no echo of the value. A set that cannot be resolved is **never skipped**:
the whole request is 422 `rule-set-not-attachable` before any rule runs, and the detail ("Rule set
`<value>` is not a published rule set visible at this folder.") is the same for an unknown id, a
set of another branch, a set without a published version and a missing version, so the answer
reveals nothing about other branches. This differs from a stored attachment on a contract, which
is reported as a blocking `unresolvable_rule_set` finding.

## Entra and machine identities

Four kinds of principal reach the API, each with a stable id that is never an email or a UPN:

| Kind | Principal id | How it authenticates |
|------|--------------|----------------------|
| Local user | `local:<uuid>` | Access JWT from identity's local login, or a PAT |
| Entra user | `entra:<tid>:<oid>` | Entra v2 access token for the API app registration, or a PAT |
| Entra service principal | `entra:<tid>:<oid>` | Entra v2 app token (client credentials) |
| Service account | `sa:<uuid>` | A service-account key (`Authorization: Bearer dcp_...`) |

**Entra issuer entry.** An Entra tenant is one `DC_AUTH_ISSUERS` entry with `kind: entra` and a
`tenant_id` (a lowercase GUID); a second tenant is a second entry. `namespace` is configurable
(`entra` is the convention), but the principal id always starts with `entra:`, and the namespaces
`entra` and `sa` are reserved: no generic OIDC issuer may mint them. Only v2 access tokens are
accepted. Required claims are `exp, iss, aud, iat, nbf, sub, oid, tid, ver` (Entra tokens carry
`uti`, not `jti`). A token that fails any check (an ID token without `scp`, a wrong audience, the
wrong tenant, an expired token) is the generic 401. The principal type (`user` or
`service_principal`) comes from `idtyp` and `scp`, never from a `roles` or `groups` claim, which
grants nothing.

**Admission.** Registration is admission, not access. An Entra principal is admitted when it
is registered directly (`POST /entra/users`, `POST /entra/service-principals`) or is a transitive
member of a registered Entra group (`POST /entra/groups`) of its tenant. An Entra principal that is
not admitted gets 403 `principal-not-registered` on every public route of every service, from both
internal PDP routes (`/internal/v1/authz/check`, `/internal/v1/authz/accessible-folders`) and
through `RemoteAuthorizer`, never a 401 and never a 404; only the generic 401 for a bad token and
the 503 for a directory outage differ. Identity writes no record for a refused principal.
Registration grants no role: access comes only from grants made to the user, the registered Entra
group (`principal_type: entra_group`, the group GUID), the service principal or a local group the
principal belongs to. After admission identity writes just-in-time records: an `entra_users` row for
every admitted Entra user (`entra_user.created`) and, for a service principal admitted only through
a group, a registration row owned by the oldest admitting group's owner. A just-in-time row never
admits by itself; it lets the principal be named in a grant or deactivated.

**Graph membership.** Group membership always comes from Microsoft Graph (`transitiveMemberOf`,
paged, so a user in more than 250 groups needs no token overage handling), with one
client-credentials token per tenant. Answers are cached for 60 seconds per tenant and principal
(`DC_DIRECTORY_CACHE_TTL_S`), failures are never cached, and once the cached answer has expired a
Graph outage is a 503, never an admission from stale data or from direct grants alone. A principal
that is disabled or deleted in the directory is deactivated: its token is 401 once the cache window
has passed. A registration may carry an optional `tenant_id`; it is required when several tenants
are configured.

**PATs.** `Authorization: Bearer dcp_...` on any service. A PAT is created by a signed-in human
(`POST /pats`) with a mandatory `expires_at` at most `DC_CREDENTIAL_MAX_TTL_DAYS` (365 by default)
away, shown once and stored as a hash. It acts as its owner with the owner's current access,
resolved by identity on every request with no cache, so a PAT can neither outlive nor exceed its
owner. A `read_only` PAT holds only the read permissions (`dc_auth.roles.READ_PERMISSIONS`): every
write is refused. `last_used_at` is recorded in memory and flushed in batches.

**Service-account keys.** A service account (`sa:<uuid>`, owned by ROOT or an Organization) holds at
most two active keys so a key can be rotated without downtime: create key 2, move the pipeline,
revoke key 1. A third key is 409 `credential-limit-reached`. The account holds only what is granted
to its `sa:` id, directly or through a local group; revoking a key stops it on its next request.

**Interactive-only permissions.** Only a person signed in with a JWT (a local or Entra user)
may manage credentials, memberships, registrations or deactivations. A PAT, a service-account key
and an Entra service principal's app token (a JWT, but a machine credential) are refused,
whatever roles they hold. In "Route rules" below, "interactive sign-in only: a PAT or
key gets 403" covers the service principal too. The permissions are
`pat:manage`, `pat:revoke-any`, `user:create`, `user:deactivate`, `grant:manage`, `group:manage`,
`entra:manage`, `service-account:create`, `service-account:manage`, `service-account:manage-keys`
and `service-account:deactivate`; `password:rotate` is allowed only for a local principal signed in
with an interactive JWT. The refusal is 403 `interactive-session-required` and is raised only where
the caller could otherwise have acted, so an invisible target is still the unknown-target 404.

**Deactivation.** `POST .../deactivate` exists for local users, Entra users, Entra service
principals and service accounts. It is one transaction: the account turns inactive and every
refresh token, PAT and key it owns is revoked, with one event whose `revoked` object lists every
revoked row id (complete lists). The identity gate refuses an inactive principal on every request,
so its access token stops on the next request without waiting for the directory. Grants and
memberships stay in place and become inert. Reactivation restores the account but never a secret:
revoked PATs, keys and refresh tokens stay revoked, and every access token and refresh token issued
at or before the reactivation stays refused through the `sessions_valid_after` cutoff, so a
reactivated Entra user or service principal must present a token issued after the reactivation. The
cutoff only ever narrows access.

## Versions and lifecycle

Every contract version is one `contract_version` row; the lifecycle state lives in the row, never in
the ODCS document (whose own `status` field is unrelated).

**States and transitions.** `draft -> published <-> unpublished`. Publishing turns a draft into a
version in place (the version id is the draft id) and the bytes are immutable from then on: the
database refuses any change to a published row except its lifecycle columns, and refuses every
delete. Only published versions are served to consumers; unpublished ones stay visible to
`contract:publish` holders.

**Derived status and the deprecated flag.** A published version is `latest` (exactly one per
contract, the contract's Latest), `preview` (SemVer precedence above the Latest) or `older`. The
status is computed on every read, never stored, and moves when another version is promoted.
"Deprecated" is a separate flag with a `sunset_date`, orthogonal to the status: any
published version, the Latest included, may be deprecated and it stays fetchable.

**SemVer rules.** The document's top-level `version` must be a quoted SemVer 2.0.0 string (422
`invalid-version`; `1.0`, `v1.0.0` and an unquoted number are refused) and, at publish, strictly
greater than every published or unpublished version of the contract (409 `semver-conflict`, the body
names `highest_semver`). Build metadata is ignored (`1.0.0+x` is `1.0.0`) and a number is never
reused, not even after an unpublish. A new draft started from a version bumps the minor part by
default (`bump`: `major`, `minor`, `patch` or `none`).

**The publish gate.** `POST .../drafts/{draft_id}/publish` runs the stored
bytes through the validation service before any lock is taken: contracts re-parses them locally
(the ODCS acceptance check, unchanged errors), then calls validation's internal run route in
publish mode with the caller's own bearer token and the raw bytes. The same `If-Match` and
content-hash checks still apply under the locks, and the hash validation answers with must equal
the hash contracts gated. A contract that fails an enforced mandatory rule, an attached
error-severity rule or its ODCS schema is **422 `contract-validation-failed`**: the problem body
carries `report` (validation's report; every finding names its rule, rule-set name and version,
folder and JSON Pointer) and `run_id` (the stored run); the draft is untouched and nothing is
published. The refusal is audited: after the refused unit is rolled back, contracts records
one `contract.version.publish_refused` event on the contract (scope: the contract's folder chain)
whose `after` holds only `draft_id`, `semver` (the declared version, at most 256 characters),
`run_id` and `blocking`, at most 50 `{rule_key, code}` pairs of the blocking findings (`code` is the
finding's `error_code`, else its `outcome`; a schema failure adds
`{rule_key: "odcs-json-schema", code: "structural"}` first), never a message, pointer or document
value. A failure to record it never changes the 422, and no other outcome (201, 409, 412, 428,
404, 503) records it. A validation service that is unreachable, slow, saturated,
failing, or that answers for different bytes is **503 `validation-unavailable`** (`Retry-After`
copied when sent); publish never passes without a verdict, and the real gate is installed even
when `DC_VALIDATION_URL` is wrong. Settings: `DC_VALIDATION_URL` (default
`http://validation:8000`, the compose network, never the gateway), `DC_VALIDATION_PUBLISH_TIMEOUT_S`
(12) and `DC_VALIDATION_SAVE_TIMEOUT_S` (3).

A published version records what it was validated against, in the same transaction as the
publish: `validation_run_id`, `engine_version`, `schema_sha` and `pinned_rule_sets`, a JSON array
of `{rule_set_id, name, folder_id, scope, version, source (mandatory|explicit), mode
(enforce|warn), dsl_version}` (plus `"paused": true` for a pin taken while its platform set was
paused; the key is absent otherwise) ordered like the report (holder depth, rule-set name, rule-set id,
enforce before warn, version number). Every version of a mandatory rule set that was evaluated is
its own entry, so a version still in its warn window is pinned with `mode: warn`. These four fields
are immutable after the publish (a database trigger), and `contract.version_published` carries
`validation_run_id` and `pinned_rule_sets` in its `after`.

Consumers see the same four fields in `GET .../versions` and `GET .../versions/{ref}/metadata`
(`contract:read-published`): `validation_run_id`, `pinned_rule_sets`, `engine_version` and
`schema_sha`, so a consumer can tell exactly which rule-set versions a version was validated
against. All four are `null` for a version published before the publish gate recorded them (no
backfill).

**Validation on save.** Six routes create or save a draft and embed the advisory
save-mode report as `validation` next to the local `structural_validation`: `PUT
.../drafts/{draft_id}`, `POST .../drafts`, `POST /folders/{folder_id}/contracts` (upload),
`.../contracts/from-template`, `.../contracts/starter` and `.../contracts/from-version`. `validation`
is validation's report for the stored bytes under the contract's current attachments (`status`
`completed`, `structural`, `findings`, `rule_sets`, `summary`; `summary.publishable` says whether a
publish would pass now), or exactly `{"status": "unavailable"}`. The embed is requested after the
save committed (an identical save writes nothing: its transaction is ended and the draft lock
released first), with the caller's own bearer, holding no lock or transaction, within
`DC_VALIDATION_SAVE_TIMEOUT_S` (3 s): when validation is unreachable, slow, busy or answers anything
but a clean 200 the save still succeeds with `{"status": "unavailable"}`. A save-mode run is never
stored and a report is never cached, so every save asks validation again.

`POST .../contracts/{contract_id}/drafts/{draft_id}/validate` (`contract:read`) validates a stored
draft on demand without changing it (200 `draft_id`, `content_sha256`, `engine_version`,
`schema_sha`, `pins`, `report`; 404 for a published or unknown draft; 503 `validation-unavailable`
on an outage, because the report was asked for explicitly). A Policy Manager holds `contract:validate`
but not `contract:read`, so they validate only their own uploads through
`POST /api/v1/validation/folders/{folder_id}/validate`, never a stored draft.

**Attached rule sets.** A contract owner raises the bar above the mandatory governance by
attaching extra published rule sets to one contract (`PUT .../contracts/{id}/rule-sets/{rule_set_id}`),
from the contract's folder, any ancestor, ROOT or the platform. Visibility is decided by validation
(the caller's own bearer is forwarded to `GET /api/v1/validation/folders/{folder}/available-rule-sets/{id}`
before any lock is taken), so a set from a sibling branch is exactly as unattachable as an unknown
one (422 `rule-set-not-attachable`). `version` is `latest` (resolved at every publish to the newest
non-deprecated version) or an exact, non-deprecated number. The rows belong to contracts
(`contract_ruleset_attachment`, with a name snapshot and no foreign key to validation); every attach,
re-pin and detach takes the contract lock, bumps `attachments_revision` and writes one audit event.
A rule set that already applies to the folder as mandatory is reported as `status: "mandatory"` and
never becomes a row: a contract owner cannot detach or pin away a mandatory set. An attached set has
no warn period: its error-severity failures block a publish exactly like an enforced mandatory rule
(findings carry `source: "explicit"`, `mode: "enforce"`), while its warning and info findings never
block. A publish sends the attachments and the `attachments_revision` it read to validation, and
under the contract lock compares that revision with the contract's own: a different value is
**409 `rules-changed`**, nothing is published, and the retry validates the new set. The published
version pins every attached set it was validated against with `source: "explicit"`. A change to the
mandatory rule sets between the validation run and the publish lock is not revision-checked; the
version's pins record exactly what it was validated against and the next publish sees the new state.

**Publish compatibility.** A publish compares the draft with the highest published
version of the contract by SemVer precedence (the baseline: Preview, Latest and deprecated versions
count, unpublished versions and drafts never do) and sends validation that version's bytes together
with the `stable_major_floor`, the highest major among published versions that are not prereleases.
A breaking change needs a declared major above the floor; there is no override, no request field,
header or role that publishes a breaking change without it (422 `contract-validation-failed`, report
finding `semver-breaking-change`). A first publish has no baseline and no floor. Save-mode runs (a
draft save and `POST .../drafts/{draft_id}/validate`) send the same baseline and floor, so a saved
draft's report already says which version the publish needs. Contracts reads the baseline in the same
unlocked block as the attachments, and under the contract lock reads it again: a different baseline or
floor is **409 `baseline-changed`**, nothing is published, and the retry validates against the new
baseline. Every run also carries `ref_facts` for the document's contract links, scoped to the caller:
a target the caller cannot read is `not_found` exactly like a missing one. When identity cannot
answer the caller's scopes, a save or draft validate sends no facts (every link `unverifiable`, the
report is advisory), but a publish is **503 `service-unavailable`** before validation is called:
`unverifiable` never fails a rule, so a link rule must not pass on an outage. Under the lock the
link facts are also re-read from contracts' own database with the scopes already fetched (no
identity call under a lock), after the linked contracts and their version rows are locked: a fact
that got worse since it was read (a linked version unpublished, a linked contract deleted) is the
same **409 `baseline-changed`**, nothing is published, and the retry validates the new facts.
A fact that got better (for example the target was published meanwhile) is accepted, because the
verdict is then stricter than the truth.

How validation's answer maps to the publish answer (nothing is published in any row but the first;
the draft stays a draft and no `contract.version_published` event is written):

| Validation | Publish |
| --- | --- |
| 200, `summary.publishable` true, `content_sha256` equal to the gated hash | 201, version published with its pins |
| 200, not publishable | 422 `contract-validation-failed` with `report` and `run_id` |
| 200 with a different `content_sha256`, or a body that is not a report | 503 `validation-unavailable` |
| unreachable, connection error, no answer within `DC_VALIDATION_PUBLISH_TIMEOUT_S` | 503 `validation-unavailable` |
| 500 or any 5xx, 408, 429 | 503 `validation-unavailable` |
| 503 busy with `Retry-After` | 503 `validation-unavailable`, `Retry-After` copied |
| 401 | 401 `unauthenticated` |
| 403 `principal-not-registered` or `interactive-session-required` (identity's problems) | the same problem, 403 |
| any other 403 | 403 `forbidden` |
| any other 4xx (400, 404, 413, 415, 422, ...): a limit mismatch or a bug between the services, never the caller's permission | 503 `validation-unavailable` (the status and validation's problem type are logged) |

The token and the body of a failed answer never appear in a problem detail or a log line. A
personal access token (`dcp_...`) publishes through the same gate: contracts forwards the
caller's own bearer, validation resolves it through identity like every service, and a read-only
token is refused by contracts' own policy before validation is called.

**Stored validation reports.** `GET .../versions/{ref}/validation-report` returns
the report a version was published with to anyone who can fetch that version: the route requires
`contract:read-published` at the contract's folder and resolves `ref` with the same visibility rules
as the fetch, so a Consumer reads the report of a published version, an owner (`contract:publish`)
also that of an unpublished one, and a Policy Manager without a contract role gets the
unknown-contract 404 (reports carry change paths and allow-listed values). Contracts then
reads the version's `validation_run_id` and calls validation's internal run read with the caller's
own token; validation checks the permission again at the run's stored folder, so a run id is never
an IDOR. A version published before the gate recorded runs has none: 404
`validation-report-not-recorded`. The report is immutable, so the strong `ETag` is `"run-<run_id>"`;
`Cache-Control: private, no-cache` keeps shared caches away and makes every reuse revalidate, and a
matching `If-None-Match` is answered 304 only after authorization and visibility (a caller who may
not read the version still gets the 404). An unreachable or failing validation service is 503
`validation-unavailable`; an empty report is never served.

**Changelog between two versions.** `GET /contracts/{contract_id}/diff?from=&to=`
classifies every change from `from` to `to` with the same classifier as publish (`dc_odcs.diff`,
`classifier_version` in the answer) and the same change shape as a stored report's `compatibility`
section, but computed on the fly, never stored, and uncapped up to the hard limit of 10,000 changes
(`changes_total` and `counts` are always the true totals; `truncated` says the list stops early).
JSON is the only format. A ref is a strict SemVer string or `latest`, resolved with the version
fetch's visibility rules, or a draft id, which only a `contract:read` holder may name; anything the
caller cannot see is the unknown 404. `from` newer than `to` is allowed: the diff is simply that
direction (a removed property in one direction is an added one in the other). `required_bump` is
the bump the changes call for (`major` for any breaking change, `minor` for warnings, `patch` for
info only, `none` for no change); it does not judge the declared version. Changes are ordered by
path, code and attribute, so the answer is deterministic: pages hold at most `limit` changes (1 to
1,000, default 500) and `next_cursor` (opaque; `null` on the last page) continues without skipping or
repeating a change. The strong `ETag` is derived from the two content hashes and the classifier
version, with `Cache-Control: private, no-cache`; `If-None-Match` is answered 304 only after
authorization and visibility.

**Rule impact: dry-runs and compliance drift.** Both reports are
paged, synchronous evaluations orchestrated by contracts: contracts lists the next keyset page of
in-scope contracts (non-deleted, with a Latest version, ordered by contract id), sends their stored
Latest documents to validation's internal batch route with the caller's own bearer, and answers
the page with `next_cursor` (opaque, `null` on the last page) and `total_in_scope` (the number of
candidates for the whole walk, computed with the same filter). Walking every `next_cursor` visits
each contract exactly once; there is no snapshot across pages, and nothing is stored. A page holds
at most `DC_IMPACT_PAGE_SIZE` contracts (default 25), `DC_IMPACT_PAGE_MAX_FOLDERS` distinct folders
(default 20) and `DC_IMPACT_PAGE_MAX_BYTES` of stored documents (default 4 MiB), always at least one
contract; each setting is refused at startup above validation's default batch caps
(`VALIDATION_BATCH_CAPS` in contracts' settings; a repo test pins them to validation's defaults).
Contracts cannot read validation's settings: lowering `DC_MAX_BATCH_ITEMS`, `DC_MAX_BATCH_FOLDERS`
or `DC_MAX_BATCH_BYTES` on validation needs the matching `DC_IMPACT_PAGE_*` lowered on contracts,
otherwise every impact page is a 422 there and a 503 here. Candidates are filtered
by the caller's scope for the route permission, so a blocked subtree never reaches validation. A
scope with no candidates answers an empty page without calling validation. Names are least
privilege: a contract's id, name, ODCS id, folder and Latest SemVer are shown only when the caller's
`contract:read-metadata` scope covers it; otherwise it is left out of `items` and counted in
`summary.hidden`, while its outcome still counts in the totals. Results never carry document
content: metadata, outcomes, rule keys and the rule author's messages (or the fixed text of an error
code). A failing rule also carries at most 20 JSON Pointers (`pointers`, `pointers_total`), but only
for a contract inside the caller's `contract:read` scope: a pointer says where an author-controlled
target matched, so it locates document values. For every other contract the failing rule keeps its
verdict and keys, `pointers` is empty, `pointers_total` is 0 and `pointers_withheld` is true; the
scope is asked of identity's PDP and an outage is 503 `service-unavailable`, never pointers.
The per-contract verdict of a rule the caller wrote is still one bit about a document they may not
read; that residual risk is accepted and every call is audited. A dry-run's `would_fail` counts contracts with a
failing error-severity rule, `would_error` those blocked only by rule errors (or an unparseable
document), and `pass` the rest. Drift lists `drifted` contracts (a blocking finding under an
enforced version) and `at_risk` ones (only warn-mode versions fail) with the version's
`pinned_rule_sets` next to `current_versions`, so the reader sees which rule set moved. Starting a
page changes no state and sends no notice, but every answered page is audited: one event
`governance.rule_set_dry_run` (aggregate `rule_set`) or `governance.drift_read` (aggregate
`folder`) with the actor, the scope folder in `scope_path` and the page's counts in `after`
(`continued`, `listed`, `total_in_scope` and the summary counts), never a contract id, name, rule
key or pointer. The audit row commits after the evaluation, so no transaction is open meanwhile. A saturated validation
batch pool is 503 `validation-unavailable` with validation's `Retry-After`; an unreachable or slow
validation service (`DC_VALIDATION_IMPACT_TIMEOUT_S`, default 30 s), an unreadable answer or one for
other contracts than were sent is 503 `validation-unavailable` too; an identity outage is 503
`service-unavailable`.

**Conditional requests.**

- Draft routes use a strong ETag `"r<revision>"`. `PUT`, `DELETE` and `publish` need `If-Match` with it
  (strong comparison, `*` matches): 428 `precondition-required` without the header (answered before
  the body is read), 412 `precondition-failed` when the tag is stale or weak.
- Version routes use the strong ETag `"<sha256 of the stored bytes>"`, with a `-json` or `-yaml`
  suffix for a converted rendering. `If-None-Match` uses weak comparison (a list of tags or `*`) and
  a match is 304 with no body.
- Every version fetch and every 304 carries `ETag`, `Vary: Accept` and `Cache-Control: private,
  no-cache`; a deprecated version's 200, 304 and metadata also carry the deprecation headers below.
  A 304 never reveals what authorization or visibility would hide: the conditional check runs after
  both.
- Through the gateway, 304, 412 and 428 pass unchanged (the gateway re-renders only 413, 429 and
  5xx). The CORS configuration lets a browser send `If-Match` and `If-None-Match` and read `ETag`,
  `Deprecation` and `Sunset`.

**Deprecation headers.** A deprecated version carries `Deprecation: @<unix seconds>` (RFC 9745, the
moment of the first deprecation) and `Sunset: <HTTP-date>` (RFC 8594, 23:59:59 GMT on the sunset
date). The sunset date is today or later in UTC (422 `invalid-sunset-date`). The file is never
edited and stays fetchable after the sunset date.

**Write routes address a version by SemVer only.** `latest` is a read-only alias on the fetch and
metadata routes; promote, unpublish, republish and deprecate answer 404 for it (and for any `ref`
that is not strict SemVer). The current Latest cannot be unpublished (409 `latest-version-protected`;
promote another version first), and republishing an unpublished version makes it a Preview or an
older version, never the Latest.

**Visibility.** Drafts need `contract:read`; unpublished versions need `contract:publish`; published
versions need `contract:read-published` (a Consumer). A caller who may only read published versions
gets the unknown-contract 404 for a contract that has no Latest, so a draft-only contract cannot be
discovered. Every denial of a contract the caller cannot see is the same 404 body (see "Deny
semantics").

**Soft delete and references.** See "Soft delete" and "Contract references (delete guard)" below;
a contract that another contract's published version links to cannot be deleted (409
`contract-referenced`).

## Where contracts live

A contract can be created in, and lives in, a folder of any kind: ROOT, Organization, Project or Team
(an archived subtree is still read-only, 409 `folder-archived`). `contract:create` is checked at the
target folder like any other permission, so it is held by whoever holds the role at that folder or
above it; nothing is added to the role matrix.

**ODCS id scope.** An ODCS id is unique inside its scope: contracts at an Organization, its Projects
and its Teams share that Organization's scope, and contracts directly at ROOT share one ROOT scope.
The same id may exist in the ROOT scope and in an Organization, or in two Organizations; a duplicate
inside one scope is 409 `duplicate-contract-id`. `organization_id` in the contract metadata (and in the
`contract.created` event) is the Organization id for any contract at or below an Organization and null
for a contract directly at ROOT.

**Visibility.** Visibility and permissions follow normal downward inheritance only: a grant at a folder
covers that folder and the folders below it. No rule makes a contract visible to the folders below it,
so a contract at an Organization is invisible (404, no list row) to a principal whose grants sit only at
a Project or Team below it.

## Soft delete

`DELETE /api/v1/contracts/contracts/{contract_id}` marks the contract deleted (`deleted_at` and
`deleted_by`); no contract or version row is removed or rewritten. A deleted contract is hidden
from everyone: every contract-scoped route (metadata, document, drafts, versions, version
metadata and every lifecycle write) answers the unknown-contract 404, byte-identical to an id
that never existed, and the contract is on no list or search result. Its ODCS id stays taken in
its Organization (or in the ROOT scope for a contract directly at ROOT), so a create with that id is 409 `duplicate-contract-id`, and a deleted contract
is never a create-from-version source (404). Deleting a deleted contract is 404.

An Organization admin or a Global admin restores it with `POST .../contracts/{contract_id}/restore`; everything is visible again as it was. Everyone else, editors and Team and Project admins
included, gets the unknown-contract 404 from restore, so a deleted contract's existence is never
revealed to them.

## Contract references (delete guard)

A contract cannot be deleted while another contract's published version links to it. ODCS has no native contract-to-contract link, so the platform recognises two forms, in the
`relationships[].to` (a string or a list of strings) and `authoritativeDefinitions[].url` fields,
at any depth of the document:

- the platform URL of the target: a value containing `/api/v1/contracts/contracts/<uuid>`
  (optionally followed by `/versions/...`);
- a file reference named after the target's ODCS id: `<odcs-id>.yaml#...` or `<odcs-id>.yml#...`
  (the last path segment before `#`, without the suffix; a URL prefix is allowed, and the id may
  use letters, digits, `.`, `_`, `-` and `:`, so URN-style ids work).

**Resolution.** A platform URL names its target by uuid, in every Organization. A file name is
resolved to the contract with that ODCS id in the referrer's own Organization (or the ROOT scope,
for a referrer directly at ROOT) when it has one
(ODCS ids are unique per scope and stay taken after a delete); otherwise it names every
contract with that ODCS id in any Organization, so an ambiguous link blocks rather than leaving a
dangling reference (fail-closed). Links are stored as text when a version is published and
resolved when a delete asks, so a contract created after its referrer was published is still
protected.

**What blocks.** Only a *published* version of another, non-deleted contract. Drafts, unpublished
versions, soft-deleted referrers and a contract linking to itself never block, so a contract with
drafts only can always be deleted. At most 1000 distinct links are indexed per version. Links are
indexed once, at publish: a version published before the guard existed is not indexed.

**The refusal** is 409 `contract-referenced`. Referrers whose folder the caller can see (through
`contract:read-metadata` or `contract:read-published`) are listed as `referrers`
(`contract_id`, `odcs_id`, ordered by contract id); the others are only counted in
`hidden_referrer_count`, and no folder, path or Organization is ever returned. Visibility limits
what is named, never whether the delete is refused. A refused delete changes nothing and records
no event.

**Bringing links back.** Unpublished versions and deleted referrers do not block, so their
links must not come back into force after a target was deleted. Republishing an unpublished version
whose stored links resolve (by the rule above) to a soft-deleted contract, and restoring a contract
whose published versions do, are refused with 409 `contract-referenced`, `deleted_target_count` and
a `reasons` entry with the same count; a deleted contract is never named. Restore the target first
(an Organization or Global admin), or publish a new version without the link. Both checks run under
the referrer's contract lock and take `FOR KEY SHARE` on every live target, so a concurrent delete
of a target is either seen or refused. A refusal changes nothing and records no event.

**The second blocker.** A card with an active pin on any version of the contract also
refuses the delete, with 409 `contract-pinned` and a `reasons` entry naming every pinning card (see
"Blocked lifecycle operations"). When the contract is both referenced and pinned the
type stays `contract-referenced`, the top-level `referrers` and `hidden_referrer_count` stay, and
`reasons` lists the reference reason first and the pin reason second. A blocked delete records no
event and notifies no card: the 409 already names them to the owner. Ended pins never block.

### Blocked lifecycle operations

An unpublish or a delete that cannot go ahead answers one 409 listing every reason. The body keeps
the usual problem members and adds `reasons`, an array in the fixed order `latest-version-protected`,
`contract-referenced`, then the pin reason (`version-pinned` for an unpublish). The problem `type`
is the first reason's slug, so a client written against the earlier slugs still recognises them,
and the existing top-level keys (`referrers`, `hidden_referrer_count`) stay whenever the reference
reason is present. A reason is `{"code": ...}` plus its own data: the reference reason carries
`referrers` and `hidden_referrer_count`, a pin reason carries `cards`.

Each entry of `cards` has `card_id`, `name`, `contacts` (every contact, `{name, kind, value}`) and
`pins` (`subscription_id`, `version_id`, `semver`, `pinned_at`, `pinned_by`, highest SemVer first).
Cards are ordered by `lower(name)` then id. Pinned cards are named whatever their organization:
the pin sits on the caller's own contract and the owner must be able to reach the consumer. A
card's members, channels and its pins on other contracts are never included.

A refused unpublish with pinning cards records one `contract.version_unpublish_blocked` audit
event, written after the refused request released its locks, and the notice consumer gives each
listed card one `contract.version.unpublish_blocked` notice. A card that already holds such a
notice for the same version recorded within `DC_BLOCKED_NOTICE_COOLDOWN_S` (default 86400) of the
event gets no further one; every attempt is still audited. A failure while recording never changes
the 409. Once a publisher removes the pins (with a reason), the same unpublish succeeds.

## Subscriber cards

A subscriber card records what a consuming team depends on. Every rule below is enforced by the
contracts service itself, whatever sits in front of it.

- **Ownership.** A card belongs to one folder of any kind: Root, Organization, Project or Team
  (an earlier team-or-project rule was dropped; the slug `card-folder-not-supported` is retired). A card at Root has no Organization: `organization_id` is null in the card index
  (migration 0018). `POST .../folders/{folder_id}/cards`
  needs `card:create` at that folder: Consumers and Team, Project, Organization and Global admins
  hold it, Team editors and readers do not (403). Names are unique per folder among non-archived
  cards, compared with `lower()` and no Unicode normalisation (409 `card-name-taken`); an archived
  card's name can be reused.
- **Visibility.** Anyone who can read the owning folder reads the card. Everyone else
  gets the same 404 as for an unknown id, in status, title, detail and type. The card's creator and
  the folder's admins (holders of `card:manage` there) are its managers; a creator who lost access
  to the folder is no longer one.
- **Contacts.** `contacts` is a typed list of at most 20 `{name?, kind, value}` with `kind`
  one of `email`, `url` or `text`. Several contacts of one kind are allowed, and values are stored
  and returned exactly as submitted (no case folding, no trimming). A rejected contact is 422
  `validation-error` naming the pointer, and the problem body never echoes the value.
- **Outage rule.** The route gate `card:read` is principal-level, so it asks identity before
  the card is loaded: with identity fully down every card route answers 503 `service-unavailable`
  for every card id, known or unknown, with identical bodies. When only the card's folder decision
  fails after the card has been loaded, the service fails closed to the uniform 404, never to 503
  and never to an allow, so an outage cannot reveal which ids exist. The same holds for the
  membership lookup of a caller the folder decision did not allow; only a caller who already sees
  the card (a folder reader, or a manager linking a member) gets 503 for a later identity failure.
- **Members.** A manager links local groups, Entra groups and service
  accounts with `POST .../cards/{card_id}/members`; identity decides who is linkable (the grant
  rules: Organization confinement, an active account, Graph confirmation of an Entra group). A
  principal in a linked local group, a transitive member of a linked Entra group, or a linked
  service account reads the card even with no role at its folder; the card's folder listing stays
  closed to them. Members are not managers: they cannot edit, archive or change the members of the
  card (403 `forbidden`, and a pipeline key therefore cannot rewire its card). The card lists its
  members ordered by `member_type` then `member_id`. Membership changes are audited
  (`card.member_added`, `card.member_removed`, never for a no-op), and members act through
  subscribe, move and unsubscribe only.
- **Subscriptions and the switched-off seams.** A card pins concrete
  published versions with `POST .../cards/{card_id}/subscriptions`; it may hold several active pins,
  including several versions of one contract, and pinning a version it already pins is a 200 no-op.
  A pin needs the acting principal's own `contract:read-published` at the contract's folder, so a
  member who cannot read a contract cannot pin it, and the answer is the unknown-contract 404.
  Subscribing takes the contract lock every lifecycle write takes first, so a subscribe never races
  an unpublish or delete. The seams for later releases exist and are switched off: a subscription's
  `mode` is `pinned` only and `contract.subscription_policy` is `open` only (both CHECK
  constraints; a policy other than `open` would answer 403 `forbidden`), and "a card grants access
  to what it pins" is the code constant `dc_contracts.subscriptions.CARD_GRANTS_ACCESS`, which is
  `False`, is no setting and is read by no code path as a grant. If it is ever turned on, the
  contract policy must first move off `open`.
- **History and access.** Ending a pin never deletes it: ended rows keep who
  and when it started, which version, and who, when and why it ended (`moved`, `unsubscribed`,
  `card_archived`), and the database refuses deletes and every change except ending a row once.
  Archiving a card ends all its active pins with `card_archived` in the same unit; the single
  `card.archived` event lists every ended subscription id. A pin records dependency, not access: if
  the principal who pinned a version later loses read access, the pin stays on the card, and that
  principal's new pins are refused. There is no card-scoped fetch: members fetch versions only
  through the version routes with their own access, and no route under `/cards` returns a version
  or a document.
- **Card notices.** Lifecycle events are recorded on the
  cards that depend on them. Six event types exist: `contract.version.published` and
  `contract.latest.changed` (audience: every card with an active pin on any version of the
  contract), `contract.version.deprecated` (audience: cards with an active pin on that version),
  `card.subscription.removed` (audience: the one card whose pin an owner removed, carried by the
  `card.subscription_removed` event itself, with the owner's `reason` and `removed_by` in the
  payload), `contract.version.sunset_approaching` and
  `contract.version.unpublish_blocked`. A card with several pins on one contract is notified once per
  event, an archived card never. The operation's own audit event carries the audience as
  `notify_card_ids` (sorted card ids, computed under the contract lock in the same transaction, so a
  rolled-back operation leaves neither event nor notice); same-date deprecations and promote no-ops
  write no event and notify nobody. The `contracts-notices` JetStream consumer hands each
  event to `dc_contracts.notices.handle_payload`, which materialises one `card_notice` per card and
  event, deduplicated by event id, so redelivery and replay create nothing. Each notice gets one
  pending `card_notice_delivery` per channel the card has enabled at that moment (`email`,
  `webhook`); a card with both channels off still records the notice. Payloads carry contract and
  version identifiers, SemVer and event fields only, never document content, contacts or members.
  `GET .../cards/{card_id}/notices` reads the record; it has no since filter, read positions or
  acknowledgements.
- **Breaking-change notices.** A publish also records
  `contract.version.breaking_published`, in addition to `contract.version.published`, on every card
  whose active pin is on a version that has at least one breaking change against the new one. The
  `contract.version_published` event carries `notify_pins` (`[{card_id, version_id}]`, identifiers
  only, read under the publish locks); the notice consumer diffs each distinct pinned version
  against the new one off the publish path (at most 25 distinct pinned versions per event, newest
  first; `truncated_pins` counts the rest), so a breaking publish is never delayed or failed by it.
  One notice per card (dedupe key `event:<event id>:breaking`) with one section per affected pinned
  version in SemVer precedence order: `semver`, `breaking_count`, `changes_total`, up to 50
  `changes` (`code`, name-keyed `path`, `attribute`, and `before`/`after` only for the classifier's
  value allow-list) and a `diff_url` (its `from` and `to` query-encoded, so build metadata `+` survives); the payload also carries `classifier_version`. Never
  descriptions, examples, quality, queries or custom properties. Email lists at most 50 sanitised
  lines in all, then `and N more, see <diff URL>`; webhooks carry the payload as it is.
- **Sunset-approaching notices.** A scheduled in-service job warns the cards pinned
  to a deprecated version as its sunset nears. Sunset dates are UTC calendar dates. With
  `d = sunset_date - today` (UTC), a scan picks the smallest configured lead `L` (days) with
  `d <= L` (the boundary is inclusive); when none qualifies, or the sunset has passed, nothing is
  recorded. Each active pin of a non-archived card gets one `contract.version.sunset_approaching`
  notice per lead (dedupe key `sunset:<subscription_id>:<lead>:<sunset_date>`), so a pin held
  through the whole window is warned once at 30 days and once at 7, a pin created three days before
  the sunset is warned once (lead 7), and a re-dated sunset warns again because the date is part of
  the key. The payload carries `sunset_date`, `lead_days` and `days_left` with the usual
  contract and version identifiers. Ended pins, archived cards, versions whose deprecation was
  cleared by promote and sunsets beyond the largest lead record nothing. Settings:
  `DC_SUNSET_LEAD_DAYS` (JSON list, default `[30, 7]`, non-empty and positive),
  `DC_SUNSET_SCAN_INTERVAL_S` (default 3600), `DC_SUNSET_SCHEDULER_ENABLED` (default true). One
  replica scans (session advisory lock `dc-card-sunset`); repeat scans and concurrent replicas add
  nothing, because a conflict on the notice dedupe constraint creates neither a notice nor a
  delivery row. There is no contract operation to protect, so the scheduler records notices
  directly through the same materialiser as the consumer and writes no outbox event.

## Webhook channel

A card can be told about lifecycle events by webhook. The first part of this section configures the
channel; "Webhook delivery" below is what a receiver sees.

- **URL rules.** `webhook_url` is accepted only if it is `https` (`DC_WEBHOOK_REQUIRE_HTTPS`, default
  true; a development stack may set it false), has a host, carries no userinfo, is at most 2048
  characters with no whitespace or control characters, and every address its host resolves to is
  globally routable (so loopback, private, link-local and cloud-metadata addresses, and IPv4-mapped
  IPv6 forms of them, are refused). A hostname listed in `DC_WEBHOOK_ALLOWED_PRIVATE_HOSTS` (a JSON
  list, empty by default, compared in lower case) may resolve to private addresses; it is meant for
  a local sink in development. Any failure is 422 `invalid-webhook-url` and the body never repeats
  the URL. Redirects are never followed by the sender.
- **The secret.** The first time a manager enables the channel the service generates a
  `whsec_` secret (base64 of 32 random bytes), stores it Fernet-encrypted with the key from the
  `webhook_secret_key` Docker secret and returns it once in that PATCH answer. No later read, log
  line or audit event carries it; audit events carry only `webhook_host` and the generation number.
  Without the key a webhook cannot be enabled (503 `service-unavailable`) and nothing is stored.
  Disabling keeps the secret; changing the URL re-validates it and keeps the secret.
- **Rotation and the overlap window.** `POST .../cards/{card_id}/webhook-secret` issues the
  next secret and returns it once with its `generation` and `previous_valid_until`. The previous
  secret stays stored, encrypted, until `now + DC_WEBHOOK_SECRET_OVERLAP_S` (default 86400 seconds)
  so a receiver can switch without dropping events; only one previous secret is kept. A card whose
  URL was set but never enabled gets generation 1 and no previous secret. Rotation is manager-only
  and needs an interactive sign-in like the first secret.
- **Interactive sign-in.** Issuing the first secret is credential management, so the PATCH
  that would create it asks the identity PDP for `card:manage-secret`, which only an interactive
  sign-in holds: a PAT or a service-account key gets 403 `interactive-session-required` before
  anything is written. Other edits keep working with a manager's read-write PAT.
- **Who sees the URL.** The full URL is shown to managers only; everyone else sees
  scheme and host.

### Webhook delivery

Each recorded notice of a card whose webhook was enabled at the time becomes one request, sent by
the in-service worker to the card's current `webhook_url` (read at send time, so a URL change
applies to deliveries still pending). The scheme is [Standard Webhooks](https://www.standardwebhooks.com/);
consumers build their verifiers against it.

- **Headers.** `webhook-id` is `msg_<delivery id>` and is identical on every retry (use it for
  idempotency); `webhook-timestamp` is the integer Unix time in seconds of this attempt;
  `webhook-signature` is one or more space-separated `v1,<base64>` entries. `content-type` is
  `application/json`.
- **Signing.** Each entry is the base64 of HMAC-SHA256 over `<webhook-id>.<webhook-timestamp>.<body>`,
  keyed with the base64-decoded part of the `whsec_` secret. The bytes signed are exactly the bytes
  sent. A receiver should reject a timestamp far from its clock and compare in constant time.
- **Overlap after a rotation.** While the previous secret's `previous_valid_until` is in the future
  the header carries two entries, one per secret (the current secret first); a receiver accepts the
  request if any entry verifies. After that window only the current secret signs.
- **Envelope.** The body is `{"type", "timestamp", "data"}` with sorted keys and no whitespace.
  `type` is the notice event type, `timestamp` the notice's event time (UTC, ISO 8601), and `data`
  holds `schema_version` (1; later changes are additive), `notice_id`, `card` (`id`, `name`) and the
  notice payload (contract and version identifiers, SemVer and event fields). It never carries
  contacts, members, document content or another card's data.
- **The worker.** One replica at a time delivers (session advisory lock `dc-card-notifier`). Due
  rows are claimed with `FOR UPDATE SKIP LOCKED` and leased for `DC_NOTIFY_LEASE_S` (default 120)
  in a short transaction, sent outside any transaction, and the result is recorded in another, so no
  row lock is held across the HTTP call and a crashed worker's rows come back after the lease.
  `DC_WEBHOOK_TIMEOUT_S` (default 10) bounds one request; redirects are not followed.
  `DC_NOTIFY_WORKER_ENABLED=false` keeps the loop off.
- **Outcome.** A 2xx answer marks the delivery `delivered`. Anything else is a failed attempt. The
  delivery row (and `GET /cards/{card_id}/notices`) records `attempts`, `last_attempt_at`,
  `last_status` (the HTTP status when there was one) and `last_error`, always a code from a fixed
  list: `http-<status>`, `timeout`, `connect-error`, `tls-error`, `invalid-url`, `ssrf-blocked`, `dns-error`,
  `no-key`, `internal-error`, `channel-disabled`, `card-archived`; never a response body, a URL or a secret. A channel that is disabled, or has
  no URL, when the delivery comes due ends the delivery `skipped`.
- **Send-time validation and pinning.** DNS can change after a URL was
  accepted, so every attempt resolves the host again. If any resolved address is not globally
  routable (private, loopback, link-local including 169.254.169.254, unique-local, unspecified,
  multicast, reserved, or an IPv4-mapped form of one) the attempt fails with `ssrf-blocked`; a host
  that no longer resolves fails with `dns-error`. Neither opens a connection, and both follow the
  normal retry schedule. The request is then **pinned**: it connects to the first validated address
  (`https://<ip>:<port><path>`) with the `Host` header set to the original host and the TLS SNI and
  certificate check set to the same hostname (the httpx `sni_hostname` request extension), so a DNS
  answer that changes between validation and connect cannot redirect the request, and the receiver's
  certificate is still verified against its own name. A hostname in
  `DC_WEBHOOK_ALLOWED_PRIVATE_HOSTS` is exempt from the address check and connects by name; leave the
  list empty in production. The pinning was verified by a spike
  (`contracts/tests/unit/test_webhook_pinning.py`: a certificate for another name, or an IP
  connection without `sni_hostname`, is refused). There is no remaining DNS-rebinding window for
  non-allow-listed hosts. Redirects are never followed.
- **Retry policy.** A failed attempt (any non-2xx answer including 410, a timeout, or a
  connection or TLS error) is retried after the wait from `DC_NOTIFY_RETRY_SCHEDULE_S` (a JSON list
  of seconds, default `[60,300,1800,7200,21600,43200]`; the wait after attempt N is entry N and the
  last entry repeats), each wait jittered by +-10%, until `DC_NOTIFY_MAX_ATTEMPTS` attempts (default
  7) were made; the delivery is then `failed`. `failed` is terminal for that one delivery only: the
  channel is never disabled, no failure count is kept on the card, and the next event is delivered
  again. 410 Gone is not special. A receiver that wants no more events turns the channel off on the
  card.
- **Delivery never blocks the owner.** A lifecycle operation only writes its outbox event; a receiver
  that is down or slow cannot delay or fail it.

### Email delivery

A card with the email channel on gets every recorded notice in its contacts' mailboxes. Contracts
never speaks SMTP: the path is **contracts delivery worker -> mail service -> SMTP server**. Which
SMTP server production uses is an infrastructure decision; the development stack uses a mail catcher.

- **One message per notice.** An `email` delivery is sent by the same worker, with the same lease,
  retry schedule (`DC_NOTIFY_RETRY_SCHEDULE_S`, `DC_NOTIFY_MAX_ATTEMPTS`) and terminal `failed` state
  as a webhook delivery. The worker POSTs `{to, subject, text}` to `DC_MAIL_URL` +
  `/internal/v1/mail/send` with `Authorization: Bearer <mail_api_key>`, where `to` is every contact of
  kind `email` on the card at send time (the recipients see each other's addresses; they are the same
  card's contacts). `DC_MAIL_TIMEOUT_S` (default 10) bounds one call. The mail service never retries.
- **Configuration.** `DC_MAIL_URL` is the mail service's base URL on the backend network.
  `mail_api_key` is the `contracts-notifier` service account's key, read only from the Docker secret
  file of that name; it has no default and is never logged, stored on a notice or delivery, or put in
  an error code. Without either, a delivery fails with `mail-not-configured` and is retried, so
  fixing the configuration delivers the backlog.
- **Content.** The subject is a fixed template per notice type built from the contract's ODCS id
  (control characters removed, at most 80 characters), the SemVer and the event (for example
  `[Contractum] <odcs id> 2.3.0 deprecated (sunset 2027-01-12)`). The body is plain text: card
  name, contract id, ODCS id, version, the event's fields, the notice id and the event time. It carries
  no document content, no members and nothing about other cards. Every message is validated as the
  mail service's own request model before it is sent, so no CR or LF can reach a header.
- **Outcome.** A 200 answer with no refused recipient marks the delivery `delivered`. A 200 with some
  recipients refused permanently is also `delivered`, with `last_error` `mail-recipients-refused:<n>` (the accepted
  recipients are never sent a second copy). Everything else is a failed attempt, retried like a
  webhook: a 200 with any recipient temporarily refused (`mail-recipients-deferred`, greylisting or a
  full mailbox; the accepted recipients may get a second copy), a 200 with every recipient refused (`mail-recipients-refused`), any other status
  (`mail-http-<status>`, for example 503 when SMTP is down or 401 for a wrong key), a timeout
  (`mail-timeout`), a connection error (`mail-connect-error`), an unreadable answer
  (`mail-bad-response`), a message the mail service would reject (`mail-invalid-message`) and missing
  configuration (`mail-not-configured`). Contacts and the channel flag are read at send time: a card
  whose email channel was turned off after the notice ends `skipped` (`channel-disabled`), and a card
  with email on but no email contact records the notice and ends `skipped` (`no-recipients`).
- **Never blocks the owner.** A mail service that is down costs a retry and nothing else: the
  lifecycle operation that caused the notice has already answered.

## Known exception

A syntactically malformed JSON body on a Pydantic-bodied route returns 422 before authentication
is evaluated. Routes without a Pydantic body authenticate first.

## Route rules

The route list, each route's permission and whether it needs a token are generated from the
code into the [HTTP API reference](../user/reference/http-api.md). The notes below keep the
per-route rules the generated summaries do not carry: status codes, limits, headers, interactive
and read-only token rules, existence hiding and audit events. A route whose only rule is its
permission is not listed here. The permission named first in a note must match the generated
reference; the code is authoritative.

### Sign-in

- `POST /api/v1/identity/auth/login`: Local login (bootstrap secret or password); public; 403 `sign-in-method-disabled` (before the password is looked at, counting no failure) while a Global admin has turned local sign-in off; with `X-Token-Delivery: cookie` the refresh token arrives as an HttpOnly cookie, see "Browser session cookie"
- `POST /api/v1/identity/auth/refresh`: Rotates the opaque refresh token (from the body or, for a browser, the cookie); public; 401 `invalid-credentials` for an unknown, expired or reused token, one whose session is older than the session length (`session_max_s`) and when no token is sent; 403 `sign-in-method-disabled` while local sign-in is off
- `GET /api/v1/identity/auth/policy`: Public (`Cache-Control: no-store`), no secret: `{methods: {local, microsoft}, password_rules: {min_length, require_upper, require_lower, require_digit, require_symbol}}`. The sign-in methods currently on (a stored setting can switch a method off, never on without its capability; the break-glass `DC_SIGNIN_LOCAL_FORCE_ENABLED` forces local on) and the rules a *new* password must meet. The sign-in, rotate-password and change-password screens read it.
- `POST /api/v1/identity/auth/logout`: Revokes the refresh token (from the body or the cookie); public; always 204
- `POST /api/v1/identity/auth/password`: `password:rotate`; the only call a bootstrap session may make

### Folders

- `POST /api/v1/identity/folders`: `folder:create` on the parent (Organization, Project or Team by depth); `display_name` follows the name rule (see "Names")
- `GET /api/v1/identity/folders`: `folder:list` (principal-level); `?path=acme/payments/ingest` resolves a slug path
- `GET /api/v1/identity/folders/{folder_id}`: `folder:read`; folders are addressed by UUID
- `PATCH /api/v1/identity/folders/{folder_id}`: `folder:update`; only `display_name` changes (name rule, see "Names"), slugs are immutable
- `POST /api/v1/identity/folders/{folder_id}/archive`: `folder:archive`; subtree becomes read-only
- `POST /api/v1/identity/folders/{folder_id}/unarchive`: `folder:unarchive` (Global admin only)
- `DELETE /api/v1/identity/folders/{folder_id}`: `folder:delete` (Global admin only, write: a read-only PAT gets 403; a Team admin who can read the folder gets 403, a caller who cannot read it the uniform 404); deletes a folder when nothing live is inside it, 204 without a body (see "Folder deletion" below); 422 `validation-error` for ROOT (no remote call is made); 409 `folder-archived` when a folder above it is archived (the folder itself being archived is fine); 409 `folder-not-empty` with `blockers` `[{kind, count}]` while anything live is inside; 503 `service-unavailable` when contracts or validation cannot answer, nothing deleted; the folder's role grants are removed in the same transaction; audit `folder.deleted` (the allow-listed folder snapshot plus `slug_path`, `ancestors`, `removed_grants` and `purged`, `after` null)
- `GET /api/v1/identity/folders/{folder_id}/deletion-check`: `folder:delete`, the same permission, denials and ROOT 422 as DELETE; 200 `{folder_id, deletable, blockers, purges}`: `blockers` is exactly what DELETE would refuse with, `purges` lists the leftovers that would be permanently removed with the folder (`[{kind, count}]`); 503 `service-unavailable` like DELETE; writes nothing, no audit event
- `PUT /api/v1/identity/folders/{folder_id}/block-inheritance`: `folder:set-block`, evaluated on the parent folder; audit `folder.block_set` / `folder.block_cleared`; the body is the full folder, or only `{id, block_inheritance}` when the caller cannot read the folder
- `GET /api/v1/identity/folders/{folder_id}/effective-access`: `folder:read`; the caller's roles and permissions at the folder, `inheritance_blocked`, and `inheritance_blocked_at` (the cutting folder's id, null unless the caller can read that folder)

### Your account and sessions

- `GET /api/v1/identity/me`: `me:read` (principal-level, held by every authenticated non-restricted principal: JWT, PAT and key; a restricted bootstrap token is 403 `password-rotation-required`). Answers `IdentityMeOut`: `principal_id` in grant form (a PAT answers with its owner's id), `principal_type` (`user`, `entra_user`, `service_account`, `service_principal`; never `pat` or `system`), `idp` (`local` or `entra`), `via` (`jwt`, `pat`, `key`), `display_name`, `username` (local) or `preferred_username` (Entra, display only), `read_only`, `interactive` (`via` is `jwt` and the principal is a user, not a service principal), `groups`, `assignments` (trimmed to the folders the caller may read) and `global_permissions` (the principal-level and ROOT-evaluated permissions the caller holds after the read-only PAT and interactive-only narrowing). Reads are not audited
- `GET /api/v1/identity/me/capabilities`: `me:read`; optional `?folder_id=<uuid>` (one folder; repeats are not supported). With a folder the PDP first checks `folder:read` there: an unknown folder and a folder the caller cannot read are the same recorded 404 `not-found`, never a 403. Answers `IdentityMeCapabilitiesOut`: `roles` held at the folder, `permissions` (the folder-evaluated ones the PDP would answer ALLOW for, so a read-only PAT lists no write and a PAT, key or service principal lists no interactive-only permission), `inheritance_blocked` and `grantable_roles` (the roles the caller could grant there, by the same ceiling and level-binding predicates as the grant write; empty unless `grant:manage` survives narrowing). Without `folder_id` it answers `folder_id` null, the ROOT roles and the same permissions as `global_permissions` of `/me`
- `PATCH /api/v1/identity/me`: `me:update` (principal-level write, interactive sign-in only: a PAT or key gets 403 `interactive-session-required`, a read-only token is refused, a restricted bootstrap token is 403 `password-rotation-required`). Body `{display_name}` only (`extra` fields are 422): 1 to 200 characters under the name rule (see "Names": trimmed, no emoji, control, format or bidirectional characters; 422 `validation-error` at `/body/display_name` naming the code point, the value never echoed). Local accounts only: an Entra principal or a service account is 409 `display-name-managed-externally` with no write and no event. Answers `IdentityProfileOut` (`principal_id`, `display_name`, `username`, `updated_at`). One `user.display_name_changed` event per effective change (before and after from the allow-listed user fields, ROOT scope); the current name again is a 200 no-op with no event. The access token's `name` claim keeps the old name until the next refresh (at most the access-token lifetime, about 15 minutes); nothing on the server or in the UI reads it, the UI reads `/me`
- `GET /api/v1/identity/me/sessions`: `me:read`. The caller's live sessions (one per refresh-token family: not rotated away, not revoked, not idle-expired, started inside the session length), newest activity first, at most 100 shown. Answers `IdentitySessionListOut`: `items` (`id` the family id, `started_at`, `last_active_at` the last sign-in or refresh, `idle_expires_at`, `expires_at` the absolute end, `client` a fixed-vocabulary device summary such as "Chrome on macOS" or "API client (curl)" or null, `current`), `total`, `max_sessions` (the effective concurrent-session limit) and `access_token_lifetime_s`. The current session is the family whose row carries the jti of the caller's access token (login and refresh store it); a PAT or key lists its owner's sessions with none current; an Entra or service-account principal gets an empty list. The raw User-Agent is never stored. Not audited
- `DELETE /api/v1/identity/me/sessions/{session_id}`: `me:update` (principal-level write, interactive sign-in only: a PAT or key gets 403 `interactive-session-required`). Signs one of the caller's sessions (a family id from the list) out: 204 and one `session.revoked` event (aggregate `session`, before `{status: active, user_id}`, after `{status: revoked, user_id, reason: user_revoked}`, ROOT scope). A session that already ended is 204 with no event; an unknown id, another user's session and an Entra or service-account caller are the same 404 `not-found`. Takes effect at the next refresh: an access token already issued stays valid until it expires (`DC_ACCESS_TOKEN_TTL_S`, 15 minutes by default).
- `POST /api/v1/identity/me/sessions/revoke-others`: `me:update` (interactive sign-in only). Signs out every live session except the one making the request: 200 `IdentitySessionsRevokedOut` `{revoked}` and one `user.sessions_revoked` event (aggregate `user`, before `{active_sessions}`, after `{active_sessions, reason: others_revoked, revoked_session_ids}`); nothing to revoke is 200 `{revoked: 0}` with no event, and so is an Entra principal. A local sign-in whose session cannot be identified (its row predates session tracking) is 409 `current-session-unknown` and nothing changes. Takes effect at the next refresh: an access token already issued stays valid until it expires (`DC_ACCESS_TOKEN_TTL_S`, 15 minutes by default).
- `POST /api/v1/identity/principals/names`: `principal:read-names` (principal-level, held by every authenticated non-restricted principal: JWT, PAT and key; a restricted bootstrap token is 403 `password-rotation-required`). Body `IdentityPrincipalNamesIn` `{ids: [string]}`, 1..100 ids of at most 200 characters. Answers 200 `IdentityPrincipalNamesOut` `{items: [{id, display_name}]}` for the ids it knows (a local user, a service account, a just-in-time Entra user or a registered Entra user or service principal), sorted by id; an unknown, malformed or other-namespace id is absent, never an error. Only the display name is returned. 422 `validation-error` for an empty list, more than 100 ids or an over-long id. Reads are not audited

### Users

- `POST /api/v1/identity/users`: `user:create` (evaluated at ROOT); audit `user.created`; `display_name` follows the name rule (see "Names")
- `PATCH /api/v1/identity/users/{user_id}`: `user:update` (root-evaluated, Global admin only, interactive sign-in only: a PAT or key gets 403 `interactive-session-required`; an Org admin, a Team editor or a user with no grant gets 403). Body `{display_name}` only, validated like `PATCH /me` (the name rule, see "Names"; 422 `validation-error`). Answers `IdentityUserOut`. Only local users are addressable: an unknown id and an Entra user's id are 404 `not-found` (an Entra user's name comes from Microsoft). An inactive user may be renamed. One `user.display_name_changed` event per effective change (`updated_by` is the admin); the current name again is a 200 no-op with no event
- `POST /api/v1/identity/users/{user_id}/deactivate`: `user:deactivate` (root-evaluated, Global admin only, interactive sign-in only: a PAT or key gets 403 `interactive-session-required`); one transaction turns the local user inactive and revokes every refresh token and PAT it owns, so its access JWT, refresh token and PAT get 401 on the next request, on identity and through every other service. The one `user.deactivated` event carries a `revoked` object `{refresh_tokens, refresh_token_ids, credentials, credential_ids}` with complete id lists (row ids only, never a public id, secret or hash). Reactivation does not restore any revoked refresh token or PAT. The last active direct Global admin is 409 `last-global-admin`; an unknown user is 404; an already inactive user is 200 with no event
- `GET /api/v1/identity/users/{user_id}/sessions`: `user:read` (root-evaluated, Global admin only). The same `IdentitySessionListOut` as `/me/sessions` for a local user (none is ever current). An unknown id or an Entra user's id is 404 `not-found`. Not audited
- `DELETE /api/v1/identity/users/{user_id}/sessions/{session_id}`: `user:deactivate` (Global admin only, interactive sign-in only: a PAT or key gets 403 `interactive-session-required`; the session half of deactivation). 204 and one `session.revoked` event with reason `admin_revoked` (the admin is the actor); an ended session is 204 with no event; an unknown user or session is 404 `not-found`. Takes effect at the next refresh: an access token already issued stays valid until it expires (`DC_ACCESS_TOKEN_TTL_S`, 15 minutes by default).
- `POST /api/v1/identity/users/{user_id}/sessions/revoke-all`: `user:deactivate` (Global admin only, interactive sign-in only). Signs out every live session of a local user: 200 `{revoked}` and one `user.sessions_revoked` event (reason `admin_revoked_all`); none live is 200 `{revoked: 0}` with no event; an unknown user is 404. Takes effect at the next refresh: an access token already issued stays valid until it expires (`DC_ACCESS_TOKEN_TTL_S`, 15 minutes by default).
- `POST /api/v1/identity/users/{user_id}/reactivate`: `user:deactivate` (root-evaluated, Global admin only, interactive sign-in only); restores the account and its grants, one `user.reactivated` event. Every revoked refresh token and PAT stays revoked, and every access JWT and refresh token issued at or before the reactivation is refused with 401 (`users.sessions_valid_after`, whole-second `iat` precision, so a sign-in within the reactivation's own second must be repeated a second later); the user signs in again and creates new PATs. An unknown user is 404; an already active user is 200 with no event

### Grants and groups

- `POST /api/v1/identity/folders/{folder_id}/grants`: `grant:manage`; audit `grant.created`. Body `principal_type` and `principal_id`: `user` (`local:<uuid>` or `entra:<tid>:<oid>` of an active user record; or, instead of `principal_id`, the exact `username` of an active local user, resolved like a group member's and 422 `unknown-principal` when it names none, so an Org admin without global `user:read` can grant to a user they know; giving both or neither, or `username` with another type, is 422 `validation-error`), `local_group` (the group id), `entra_group` (the GUID of an active group registration; the directory confirms it in the registration's tenant first: unknown 422 `unknown-principal`, outage 503, an unregistered GUID 404 with no directory call), `service_principal` (`entra:<tid>:<oid>` of an active service-principal registration, direct or just-in-time; otherwise 404), `service_account` (`sa:<uuid>` of an active local service account; unknown or inactive 422 `unknown-principal`). An owned group, service principal or service account is grantable only inside its owner's Organization (ROOT-owned: anywhere; a Global admin may grant any of them on any folder); an out-of-scope service account is 422 `principal-scope-mismatch` for a caller who can read it, otherwise 404
- `POST /api/v1/identity/groups`: `group:create` on the owner (ROOT or an Organization); audit `group.created`; `name` follows the name rule (see "Names")
- `PATCH /api/v1/identity/groups/{group_id}`: `group:manage`; audit `group.renamed`; `name` follows the name rule (see "Names")
- `POST /api/v1/identity/groups/{group_id}/members`: `group:manage`, plus the grant ceiling for every role the group holds; audit `group.member_added`. Body: exactly one of `username` or `principal_id` (`local:<uuid>` of an active user, `entra:<tid>:<oid>` of an active Entra user record, or `sa:<uuid>` of an active service account; anything else 422 `unknown-principal`). `member_type` is `user` or `service_account`. An Organization-owned service account joins only groups owned by that Organization (422 `principal-scope-mismatch`, or 404 for a caller who cannot read the account); a ROOT-owned account may join any group
- `GET /api/v1/identity/folders/{folder_id}/grantable-groups`: `grant:manage`; groups that may be granted at the folder: the ROOT-owned groups and the folder's Organization's groups, names included (accepted exposure; see "Deny semantics")

### Entra registrations

- `POST /api/v1/identity/entra/groups`: `entra:manage` on the owner (ROOT or an Organization); body `owner_folder_id`, `object_id`, optional `tenant_id` (required when several Entra tenants are configured). The directory confirms the GUID in that tenant first (unknown: 422 `unknown-principal`, outage: 503); registration grants nothing; audit `entra_registration.created`
- `GET /api/v1/identity/entra/groups`: `entra:list` (principal-level); trimmed to owners where the caller holds `entra:read`; keyset-paginated in id order
- `GET /api/v1/identity/entra/groups/{registration_id}/members`: `entra:read` on the owner (the gate of the group detail, never weaker). The group's members LIVE from Microsoft Graph (`transitiveMembers`: nested groups are expanded and listed too); Contractum never stores Entra membership. 200 `{items, truncated, total}` with at most 200 items `{object_id, display_name, secondary, kind, account_enabled, registered, principal_id, record_id}`: `kind` is `user`, `group`, `service_principal` or `other`, `secondary` is the UPN or email, the mail of a group or the application ID, `account_enabled` is null for a group, `registered` says a Contractum Entra user or service-principal record exists and `principal_id` (`entra:<tid>:<oid>`) and `record_id` (the id of that record, for the link) are set only when the caller may open it (`user:read` at ROOT, or `entra:read` at the service principal's owner), otherwise they are null and `registered` is false; `truncated` says there were more than 200, `total` is Graph's count when known. 404 for an unknown or unreadable registration (also a service-principal or user id) and for a group Entra no longer knows; 503 when the directory is unreachable, throttled, refuses the call or Entra is not configured. Cached for 30 seconds, a failure never; reads are not audited, member names are never logged
- `DELETE /api/v1/identity/entra/groups/{registration_id}`: `entra:manage` on the owner, plus the grant ceiling for every role granted to the group; ends admission through the group and removes every grant made to its GUID in the same transaction; audit `entra_registration.deleted` (removed grant count, at most 20 ids)
- `POST /api/v1/identity/entra/users`: `user:create` (evaluated at ROOT, Global admin only); body `object_id`, optional `tenant_id`. The directory confirms the user first; the user's record is created or reused at once, so direct grants can precede the first sign-in; a second registration is 409; audit `entra_registration.created`
- `GET /api/v1/identity/entra/users`: `user:read` (evaluated at ROOT); keyset-paginated in id order; `registered_directly` shows whether an active registration exists
- `POST /api/v1/identity/entra/users/{entra_user_id}/deactivate`: `user:deactivate` (evaluated at ROOT, Global admin only, interactive sign-in only: a PAT gets 403 `interactive-session-required`); the Entra user gets 401 on the next request without waiting for the directory and every PAT it owns is revoked. One `entra_user.deactivated` event carries a `revoked` object `{refresh_tokens, refresh_token_ids, credentials, credential_ids}` with the complete PAT row-id list (row ids only; `refresh_tokens` is 0). Reactivation does not restore any PAT. An unknown user is 404; an already inactive user is 200 with no event
- `POST /api/v1/identity/entra/users/{entra_user_id}/reactivate`: `user:deactivate` (ROOT, Global admin, interactive only); one `entra_user.reactivated` event. A reactivated Entra user must present a token issued after the reactivation: a token issued at or before it stays 401 (`sessions_valid_after`). An already active user is 200 with no event
- `GET /api/v1/identity/entra/directory/search`: Searches the Entra directory for the register dialogs. Query `kind` (`user`, `group` or `service_principal`), `q` (2 to 100 characters; quotes, backslashes and control characters are stripped, a pasted object ID is an exact lookup, a service principal also matches its application ID), optional `tenant_id` and `owner_folder_id`. The gate is never weaker than registration: `user` needs `entra:manage` at ROOT plus `user:create` (Global admin only); `group` and `service_principal` need `entra:manage` on `owner_folder_id` (ROOT or an Organization, a Project or Team is 422 `owner-not-allowed`, a missing owner 422). Interactive sign-in only (a PAT gets 403 `interactive-session-required`). 200 `{items, truncated}` with at most 20 items `{object_id, tenant_id, display_name, secondary, kind, already_registered}` (`already_registered` is what the matching register call would answer 409 for); 422 for a `q` with fewer than 2 searchable characters; 503 when the directory is unreachable, throttled, refuses the call or Entra is not configured. Reads are not audited; the query is never logged or echoed
- `POST /api/v1/identity/entra/service-principals`: `entra:manage` on the owner (ROOT or an Organization); body `owner_folder_id`, `object_id`, optional `tenant_id`; Graph-confirmed. A just-in-time row of the same owner becomes a direct registration (audit `entra_registration.updated`), of another owner or already direct is 409; otherwise audit `entra_registration.created`. Registration grants nothing
- `GET /api/v1/identity/entra/service-principals`: `entra:list` (principal-level); trimmed to owners where the caller holds `entra:read`; keyset-paginated; each row shows `directly_registered`
- `POST /api/v1/identity/entra/service-principals/{registration_id}/deactivate`: `entra:manage` on the owner (interactive sign-in only: a PAT gets 403 `interactive-session-required`); acts on the registration row, directly registered or just-in-time, of kind `service_principal` (a group row or unknown id is 404). The principal gets 401 on the next request although a registered group still admits it. One `entra_registration.deactivated` event whose `revoked` object `{refresh_tokens, refresh_token_ids, credentials, credential_ids}` is empty (a service principal owns no credentials). An already inactive row is 200 with no event
- `POST /api/v1/identity/entra/service-principals/{registration_id}/reactivate`: `entra:manage` on the owner (interactive only); one `entra_registration.reactivated` event. A reactivated service principal must present an app token issued after the reactivation: a token issued at or before it stays 401 (`sessions_valid_after`). An already active row is 200 with no event

### Personal access tokens

- `POST /api/v1/identity/pats`: `pat:manage` (principal-level); body `name`, a required, timezone-aware `expires_at` and an optional `read_only` (default false) that must be in the future and at most `DC_CREDENTIAL_MAX_TTL_DAYS` (default 365) away. Only a signed-in human user (local or Entra) may create one; a PAT, a service principal or an API key gets 403. The 201 response carries the `dcp_` token once and never again; audit `credential.created` (no token, secret, hash or public id in the event). A PAT is accepted directly as `Authorization: Bearer dcp_...` and acts as its owner with the owner's current access. A `read_only` PAT holds only the read permissions its owner holds (the permissions whose `PermissionSpec.access` is `read` in `dc_auth.roles`, exposed as `READ_PERMISSIONS`): every write is refused at any folder, at ROOT and at principal level, and it has no writable folders. It still loses a read its owner loses on the next request. An owner holds at most `max_active_tokens` active (unrevoked, unexpired) PATs (default 10, `DC_MAX_ACTIVE_TOKENS`, stored override 1 to 1000 in the `security` settings section; each user counts separately, an Entra user by their own owner row): one more is 409 `token-limit-reached` with `limit` and `active_count`, also under concurrent requests (the owner row is locked `FOR UPDATE` before counting), and nothing is created or audited; tokens above a lowered limit are kept; `name` follows the name rule (see "Names")
- `GET /api/v1/identity/pats`: `pat:list` (principal-level); the caller's own PATs only, keyset-paginated in id order (an empty page when there are none). Each item shows `status` (`active`, `expired` or `revoked`) and `last_used_at`, never the token or its secret. `last_used_at` is recorded in memory and flushed in batches, so it can lag by up to the flush interval (`DC_CREDENTIAL_USAGE_FLUSH_S`, default 60 s). The page also carries `active_count` (the caller's live PATs) and `max_active` (the effective limit), so a screen knows the cap without loading every page
- `DELETE /api/v1/identity/pats/{credential_id}`: `pat:manage` (principal-level); revokes one of the caller's own PATs, effective on the next request; another owner's or an unknown id is 404, an already revoked one is 204 with no new event; audit `credential.revoked`
- `GET /api/v1/identity/admin/pats`: `pat:read-all` (root-evaluated, Global admin only; a denial is 403, also for an Org admin); every user's PATs, keyset-paginated in id order, optional `owner` (a `local:` or `entra:` principal id) and `status` (`active`, `expired`, `revoked`) filters. Each item shows `owner_principal_id`, `name`, `read_only`, `expires_at`, `created_at`, `revoked_at`, `last_used_at` and `status`, and never a token, secret, public id or hash. A read, so a Global admin's PAT may list too
- `DELETE /api/v1/identity/admin/pats/{credential_id}`: `pat:revoke-any` (root-evaluated, Global admin only, interactive sign-in only: a PAT or key gets 403 `interactive-session-required`); revokes any user's PAT, effective on the next request; an unknown id is 404, an already revoked one is 204 with no new event; audit `credential.revoked` with the admin as actor

### Platform settings

- `GET /api/v1/identity/settings`: `settings:read` (ROOT permission: only a Global admin; every other caller, a Global reader and an Org admin included, is 403). Answers `IdentitySettingsOut`: `sections` (in the fixed tab order, one `{section, values, defaults, defaults_source, revision, etag, updated_at, updated_by}` each: `values` are the stored overrides only, `defaults` the effective defaults, `etag` is `"<section>-r<revision>"`), the combined `etag` of all sections and `choices.entra_tenants`; see the [settings reference](../admin/settings.md)
- `PUT /api/v1/identity/settings/{section}`: `settings:write` (ROOT permission, Global admin only, interactive sign-in only: a PAT or key gets 403 `interactive-session-required`). Body is the section's full overrides object (an omitted field resets to its default; unknown fields are 422 with a JSON pointer). `If-Match` with the section ETag is required (428 without, 412 when stale). Unknown section 404 `settings-section-not-found`; 422 `validation-error` or `owner-not-allowed` for a tenant that is not configured or an owner that is not ROOT or a non-archived Organization. One audited `settings.updated` event per effective change (`before` and `after` are `{"<section>": {<stored overrides>}}`, ROOT scope); the same values again is a 200 with no event. Answers `IdentitySettingsSectionOut` and an `ETag` header. The sections include `governance` (`mandatory_warn_days` 0 to 365, `default_odcs_api_version` one of the vendored versions, `require_lifecycle_reason`), `security` (identity only, never served to other services: `local_signin_enabled`, `microsoft_signin_enabled`, `credential_max_ttl_days` 1 to 3650, `max_active_tokens` 1 to 1000 (default 10, active access tokens per owner), `max_sessions_per_user` 1 to 100 (strict integer; default 5, `DC_MAX_SESSIONS_PER_USER`: signing in with a password beyond it ends the user's oldest sessions, one `user.sessions_evicted` event, never refusing the sign-in; a lowered limit touches nothing until that user's next sign-in), `session_max_s` 3600 to 7776000, `idle_timeout_s` 900 to 604800 and at most the session length, `lockout_account_threshold` 3 to 20, `lockout_ip_threshold` 10 to 500, `lockout_max_lock_s` 60 to 86400, `password_min_length` 12 to 128 and four `password_require_*` booleans) and the platform field `audit_retention_days` (`0` keeps for ever, else 90 to 36500). The `notifications` section's `webhook_allowed_hosts` is a mandatory list (at most 100 lowercase hostnames or `*.` wildcards, no address, port or scheme): contracts sends a webhook only to a listed host and an empty list allows none, while HTTPS only, no userinfo, globally routable addresses and the Helm private-host exemption stay the outer bound that the list never widens. A deployment that already had webhook channels gets the list filled once at first start: contracts posts the distinct hosts of its non-archived webhook channels to identity's internal `POST /internal/v1/settings/webhook-hosts/seed` (permission `settings:seed-webhook-hosts` on the notifier role only), identity seeds only when no list is stored, writes a marker and one `settings.webhook_hosts_seeded` event, and never puts back a host an admin removed; a deployment with no notifier key cannot read the list and allows no webhook host. The seed route is internal and not in the public path table. Saving `security` runs the sign-in lockout rules: both methods off is 422 `signin-lockout-risk`; switching off the method the acting admin is signed in with, turning local off without a configured Entra issuer and an active Entra Global admin, or turning Microsoft off without an active local Global admin is 409 `signin-lockout-risk` (no event)
- `GET /api/v1/identity/settings/public`: `settings:read-public` (principal-level read: any signed-in, non-restricted principal). The active announcement banner (`null` outside its window), the Entra default tenant and owner that pre-fill the Register dialogs, and the effective `default_odcs_api_version`, `require_lifecycle_reason`, `credential_max_ttl_days` and `password_rules` (stored value, else the owning service's env default as last reported, else the built-in one). `Cache-Control: private, max-age=30`
- `GET /api/v1/identity/settings/export`: `settings:read`. A versioned download `contractum-settings-<UTC date>.json`: `{format: "contractum-settings", format_version: 1, exported_at, sections}` with the stored overrides of every section only (no secret, no view-only data)
- `POST /api/v1/identity/settings/import`: `settings:write`. Body is an export file (capped at 256 KiB, parsed with the depth-capped loader; 413 over the cap). `?dry_run=true` (the default) answers the field-level diff `{dry_run, changes[{section, field, before, after}], etag}` and writes nothing; `dry_run=false` needs `If-Match` with the combined ETag (428 without, 412 when stale), runs in one transaction and writes one `settings.imported` event (`aggregate_id` `import`). An unknown format, version, section or field is 422 `settings-import-invalid` with a JSON pointer. A section missing from the file stays unchanged. A `security` section runs the same lockout rules as the PUT, with the importing admin as the actor (422 `settings-import-invalid` for both methods off, 409 `signin-lockout-risk` otherwise), also on a dry run
- `GET /api/v1/identity/settings/entra/overview`: `settings:read`. View only: the configured Entra tenants (issuer, audiences), whether the Graph client and its secret file are configured (never the content) and the client secret expiry `{status: known|unknown|not-configured, expires_at, days_left, reason}` read from Graph (`endDateTime` only, cached 10 minutes, 60 seconds after a failure). Never returns a secret or a credential hint
- `POST /api/v1/identity/settings/entra/test`: `settings:write` (interactive). Requests a Graph token and one Graph call; answers `{result, tenant_id, checked_at}` with `result` one of `ok`, `not-configured`, `token-refused`, `graph-forbidden`, `graph-throttled`, `graph-unreachable`, `graph-error`. At most 6 per minute per principal (429 `too-many-attempts` with `Retry-After`). Not audited (it changes no state)
- `GET /api/v1/identity/settings/about`: `settings:read`. Version, commit, build time, database state and migration head of identity, contracts, validation, audit and mail (the others are asked with the caller's own bearer, 3 s each, in parallel; a failure reads `status: "unreachable"`)
- `POST /api/v1/identity/settings/notifications/preview`: `settings:read` (Global admin only). Body `{event_type, subject?, body?}` (`event_type` is one of the seven notice types or `generic`; an omitted `subject` or `body` previews the default). Renders the template with the shared safe renderer over a fixed sample (ODCS id `orders`, semver `1.2.0`, the card "Analytics team", a sunset 30 days ahead, two detail lines and, for the breaking type, a two-line breaking block) and answers `{subject, body}`; an invalid template is 422 `validation-error` with the pointer `/subject` or `/body`; 422 for an unknown `event_type` or extra field. Not audited (it changes no state).
- `POST /api/v1/contracts/settings/test-email`: `settings:write` (ROOT permission: Global admin only, interactive sign-in only: a PAT or key gets 403 `interactive-session-required`, any other caller 403). Body `{to}` (a plain address: at most 254 characters, no display name, no `<>()[],;:"\\`, ASCII; 422 `validation-error` otherwise, extra fields included; body capped at 1 KiB, 413). Sends the fixed message `[Contractum] Test email` through the mail service with the **caller's own** bearer (the sender is the stored sender of Admin > Settings > Notifications, else `DC_SMTP_FROM`) and answers `{outcome, error_code}` with `outcome` one of `sent`, `refused`, `unavailable`, `not-configured` and `error_code` a fixed delivery code (`mail-http-<status>`, `mail-recipients-refused`, ...) or null. At most 5 per 10 minutes per principal and 30 per hour per process (429 `too-many-attempts` with `Retry-After`). Every attempt that reaches the send step writes one `settings.test_email_sent` event (`aggregate_type` `platform_settings`, `aggregate_id` `notifications`, empty scope so it is visible at ROOT only) whose `after` is `{recipient_domain, outcome, error_code}`; the full address is never stored, logged or audited.

### Service accounts

- `POST /api/v1/identity/service-accounts`: `service-account:create` on the owner (ROOT or an Organization; Global admin or the Organization's admin; interactive sign-in only: a PAT or key gets 403 `interactive-session-required`); body `owner_folder_id`, `name` (1 to 100 characters). The principal id is `sa:<uuid>` (`principal_id` in the response). A Project or Team owner is 422 `owner-not-allowed`, another Organization is 404, the same name (case-insensitive) twice under one owner is 409 `conflict`; audit `service_account.created`; `name` follows the name rule (see "Names")
- `GET /api/v1/identity/service-accounts`: `service-account:list` (principal-level); trimmed to owners where the caller holds `service-account:read`; keyset-paginated in id order, optional `owner_folder_id` filter; an empty page when the caller can read none
- `GET /api/v1/identity/service-accounts/{service_account_id}`: `service-account:read` on the owner; an unknown id and an unreadable one are the same 404
- `PATCH /api/v1/identity/service-accounts/{service_account_id}`: `service-account:manage` on the owner (interactive sign-in only); body `name` only; audit `service_account.renamed` (no event when unchanged); `name` follows the name rule (see "Names")
- `POST /api/v1/identity/service-accounts/{service_account_id}/keys`: `service-account:manage-keys` on the owner (interactive sign-in only); body `name` and a required, timezone-aware `expires_at` in the future and at most `DC_CREDENTIAL_MAX_TTL_DAYS` away (422 `expiry-out-of-range`). An account holds at most two active (unrevoked, unexpired) keys, also under concurrent requests: a third is 409 `credential-limit-reached`; when the effective `max_active_tokens` is below two, an account at that limit gets 409 `token-limit-reached` (`limit`, `active_count`) instead; a deactivated account is 409 `conflict`. The 201 response carries the `dcp_` token once; audit `credential.created` (no token, secret, hash or public id). The key is accepted as `Authorization: Bearer dcp_...` on every service as principal `sa:<uuid>` and holds only what grants to that id (directly or through a local group) allow; `name` follows the name rule (see "Names")
- `GET /api/v1/identity/service-accounts/{service_account_id}/keys`: `service-account:read` on the owner; both keys with `status` (`active`, `expired`, `revoked`), `expiring_soon` (active and expiring within `DC_CREDENTIAL_EXPIRY_WARNING_DAYS`, default 14) and the batched `last_used_at` (it can lag by `DC_CREDENTIAL_USAGE_FLUSH_S`), so an owner sees which key a pipeline still uses before revoking the other; never the token, public id, secret or hash
- `DELETE /api/v1/identity/service-accounts/{service_account_id}/keys/{key_id}`: `service-account:manage-keys` on the owner (interactive sign-in only); revokes one key, effective on its next request; a key of another account or an unknown id is 404, an already revoked one is 204 with no new event; audit `credential.revoked`
- `POST /api/v1/identity/service-accounts/{service_account_id}/deactivate`: `service-account:deactivate` on the owner (Global admin, or the Org admin of an Organization-owned account; interactive sign-in only: a PAT or key gets 403 `interactive-session-required`; an unreadable or unknown account is 404). One transaction turns the account inactive and revokes every key it owns, so each key gets 401 on the next request. The one `service_account.deactivated` event carries a `revoked` object `{refresh_tokens, refresh_token_ids, credentials, credential_ids}` listing every revoked key row id in id order (`refresh_tokens` is 0; never a public id, secret or hash). An already inactive account is 200 with no event; creating a key on an inactive account is 409
- `POST /api/v1/identity/service-accounts/{service_account_id}/reactivate`: `service-account:deactivate` on the owner (interactive only); one `service_account.reactivated` event. Revoked keys stay revoked: new keys must be created after reactivation. An already active account is 200 with no event

### Contracts

- `POST /api/v1/contracts/folders/{folder_id}/contracts`: `contract:create` at the target folder (any kind: ROOT, Organization, Project or Team; not archived); the document's top-level `name` follows the name rule (see "Names"), 422 `validation-error` at `/name`
- `POST /api/v1/contracts/folders/{folder_id}/contracts/from-template`: `contract:create`; JSON body `template_id`, `template_version`, `odcs_id` (1 to 255 characters), `overrides`; the document's top-level `name` follows the name rule (see "Names")
- `POST /api/v1/contracts/folders/{folder_id}/contracts/starter`: `contract:create`; JSON body `odcs_id` (1 to 255 characters, `dc_odcs.MAX_ODCS_ID_LENGTH`), `name`, optional `notation` (`odcs`); a server-generated ODCS starter (`apiVersion` v3.2.0, version `0.1.0`, status `draft`); `name` follows the name rule (see "Names")
- `POST /api/v1/contracts/folders/{folder_id}/contracts/from-version`: `contract:create` at the target folder (any kind: ROOT, Organization, Project or Team; not archived); JSON body `source_contract_id`, exactly one of `source_version` (SemVer) or `source_draft_id`, and `odcs_id`; the source needs `contract:read-published` (published versions), `contract:publish` (unpublished versions) or `contract:read` (drafts) at its folder, else the unknown-contract 404. Content is copied with only the top-level `id` replaced, never linked; the origin (`origin.contract_id`, `origin.version_id`) is recorded; one access event on the source contract, scoped to the source folder; `odcs_id` is 1 to 255 characters; the copied document's top-level `name` follows the name rule (see "Names")
- `GET /api/v1/contracts/contracts`: `contract:list` (principal-level); list / search, keyset-paginated; three scopes decide the rows (`contract:read-metadata`, `contract:read`, `contract:read-published`): a caller who holds only `contract:read-published` (a Consumer) sees a contract only once it has a Latest, with every field and every `q`, `name`, `status`, `domain` and `tags` match taken from the Latest, never from a newer draft; 503 `service-unavailable` when identity cannot answer a scope (never an unfiltered page); the `odcs_id` filter is at most 255 characters
- `GET /api/v1/contracts/contracts/{contract_id}`: `contract:view` (held by every role that can see contracts, so a denied view is always 404); metadata as JSON, content fields redacted for a `contract:read-metadata` holder without `contract:read`; a caller who holds only `contract:read-published` gets the Latest with content visible, and the unknown-contract 404 for a contract that has no Latest; `latest_version` (`version_id`, `semver`) is null before the first publish; `version` and the content fields describe the head row, which is the Latest for every caller once one exists and otherwise the newest draft (newer drafts are read through the drafts routes)
- `DELETE /api/v1/contracts/contracts/{contract_id}`: `contract:delete` (visible via `contract:read`: a Team reader gets 403, a Consumer, a Policy Manager and anyone who cannot read the contract the unknown-contract 404; editors and admins hold it); soft delete, 204 without a body (see "Soft delete" below); an already deleted contract is the unknown-contract 404; a contract with drafts only can be deleted; 409 `folder-archived` in an archived subtree; 409 `contract-referenced` while another contract's published version links to it (see "Contract references (delete guard)"); 409 `contract-pinned` while any card has an active pin on any of its versions (`reasons` array naming every pinning card; no notice and no event); audit `contract.deleted` (metadata only)
- `POST /api/v1/contracts/contracts/{contract_id}/restore`: `contract:restore` (held by Organization admins and Global admins only, and its own visibility: an editor, Team admin, Project admin, anyone else and an unknown id get the same unknown-contract 404); restores a soft-deleted contract (see "Soft delete" below); 200 with the contract metadata; restoring a contract that is not deleted is a 200 no-op without an event; 409 `folder-archived` in an archived subtree; 409 `contract-referenced` when a published version of the contract links to a soft-deleted contract (`deleted_target_count`, never named; restore the target first); audit `contract.restored` (metadata only)
- `GET /api/v1/contracts/contracts/{contract_id}/document`: `contract:read`; original bytes by default, `Accept` converts

### Drafts

- `GET /api/v1/contracts/contracts/{contract_id}/drafts`: `contract:read`; the contract's drafts with declared version, revision and ETag; drafts are never shown to `contract:read-published`-only callers
- `GET /api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}`: `contract:read`; draft metadata, `ETag` header; never shown to `contract:read-published`-only callers
- `GET /api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/document`: `contract:read`; the exact draft bytes (`Accept` converts, with a `-json`/`-yaml` ETag), `Cache-Control: private, no-cache`; never shown to `contract:read-published`-only callers
- `POST /api/v1/contracts/contracts/{contract_id}/drafts`: `contract:update`; JSON body `from_version` (a semver of a published or unpublished version) or `from_draft_id` (a draft of the contract), exactly one, plus `bump` (`major`, `minor` default, `patch`, `none`); 201 with the draft (revision 1, `base_version_id` the source id), the structural report and the advisory `validation` report, `Location` the new draft, `ETag` `"r1"`; the copy keeps the source bytes and media type with only the top-level `version` token changed (`none` copies the bytes verbatim; a source `version` that is not valid SemVer is copied unchanged); 409 `draft-limit-reached` at `DC_MAX_DRAFTS_PER_CONTRACT` (default 25); audit `contract.draft_created`
- `PUT /api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}`: `contract:update`; body is the raw document; `If-Match` with the draft's strong ETag (`"r<revision>"`) is required (428 without, 412 when stale or weak, `*` matches), the document id must stay the contract's ODCS id; the response carries the structural report and the advisory `validation` report (see "Validation on save"); audit `contract.draft_updated`
- `DELETE /api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}`: `contract:update`; discards a draft; `If-Match` with the draft's strong ETag is required (428 without, 412 when stale or weak); 204 without a body; 409 `draft-already-published` for a published id and 409 `last-draft-protected` for the only row of a never-published contract; audit `contract.draft_deleted`
- `POST /api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/validate`: `contract:read` (a Policy Manager without a contract role gets the same 404 or 403 as for any unreadable draft); validates the stored bytes and the contract's attachments in save mode without changing the draft or storing a run, see "Validation on save"; 200 `draft_id`, `content_sha256`, `engine_version`, `schema_sha`, `pins`, `report`; 404 for a published or unknown draft; 503 `validation-unavailable` when validation cannot answer
- `POST /api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/publish`: `contract:publish`; `If-Match` with the draft's strong ETag is required (428 without, 412 when stale); the stored bytes first go through the publish gate (validation, see "The publish gate" below; 422 `contract-validation-failed` with the report and `run_id`, audited as `contract.version.publish_refused`, 503 `validation-unavailable` when validation cannot answer); the draft is published in place and the first publish becomes the Latest; 201 body `version_id` (the draft id), `contract_id`, `semver` (the document's own `version`), `status` (`latest`), `lifecycle` (`published`), `content_sha256`, `etag` (`"<content_sha256>"`), `fetch_url`; `Location` is `fetch_url`; the version records `validation_run_id`, `pinned_rule_sets`, `engine_version` and `schema_sha` and the audit event `contract.version_published` carries `validation_run_id` and `pinned_rule_sets`

### Attached rule sets

- `GET /api/v1/contracts/contracts/{contract_id}/rule-sets`: `contract:read-metadata` at the contract's folder (a Policy Manager reads it without content access; a caller who cannot read the contract and an unknown or deleted contract get the same 404); the rule sets attached to the contract, `{items, attachments_revision}`, each item `{rule_set_id, rule_set_name, version ("latest" or a number), attached_by, attached_at}`, ordered by lower-cased name then id; a contract without attachments is `{"items": [], "attachments_revision": 0}` (see "Attached rule sets")
- `PUT /api/v1/contracts/contracts/{contract_id}/rule-sets/{rule_set_id}`: `contract:update` (403 for a reader, 404 for a caller who cannot read the contract, 409 `folder-archived`); optional body `{"version": "latest" | n}` (default `latest`); the rule set must be a published rule set visible at the contract's folder with a non-deprecated version, otherwise 422 `rule-set-not-attachable` (one answer for an unknown, another branch's, never published or deprecated choice); 422 `validation-error` when a new attachment would exceed `DC_MAX_ATTACHMENTS` (default 50, the same limit validation applies to a run) attached rule sets; 201 `created` with `Location`, 200 `updated` (another version spec), 200 `unchanged` (same spec: no write, no event) or 200 `mandatory` (the set already applies to the folder as mandatory: no row, no event); body `{attachment, status, attachments_revision}`; 503 `validation-unavailable` when validation cannot say whether the set is visible; audit `contract.ruleset_attached`
- `DELETE /api/v1/contracts/contracts/{contract_id}/rule-sets/{rule_set_id}`: `contract:update` (403, 404 and 409 as above); 204; 404 when the rule set is not attached (a mandatory set is never an attachment, so it cannot be detached); audit `contract.ruleset_detached`

### Versions

- `GET /api/v1/contracts/contracts/{contract_id}/versions`: `contract:read-published` (a Consumer holds it); every visible version as `items`, highest SemVer precedence first (`2.0.0` > `2.0.0-rc.10` > `2.0.0-rc.2` > `1.2.0`), unpaginated and without a count; each item has `version_id`, `contract_id`, `semver`, `status` (`latest`, `preview` or `older`, derived), `lifecycle` (`published`; `unpublished` only for `contract:publish` holders), `deprecated`, `deprecated_at`, `sunset_date`, `content_sha256`, `etag`, `size_bytes`, `media_type`, `odcs_api_version`, `published_at`, `published_by`, `fetch_url`, `validation_run_id`, `pinned_rule_sets`, `engine_version`, `schema_sha` (what the version was validated against, null for a version published before the publish gate recorded it); drafts are never listed; an empty `items` list is returned only to `contract:read` holders, a caller with only `contract:read-published` gets the unknown-contract 404 when the contract has no Latest
- `GET /api/v1/contracts/contracts/{contract_id}/versions/{ref}/metadata`: `contract:read-published`; the same fields as one list item (the four validation fields included), `ref` as on the fetch route; a draft, an unknown ref and a contract without a Latest (for a published-only caller) are the same 404
- `GET /api/v1/contracts/contracts/{contract_id}/versions/{ref}`: `contract:read-published`; `ref` is the `latest` alias or a strict SemVer 2.0.0 string; serves published rows (and, for `contract:publish` holders only, unpublished ones); a draft, an unknown ref, an unreadable contract and, for a caller without `contract:read`, a contract with no Latest are the same 404; the stored bytes byte-for-byte with the stored `Content-Type` by default, `Accept` for the other format returns a converted rendering; strong `ETag` `"<content_sha256>"` (`-json`/`-yaml` suffix for conversions), `Vary: Accept`, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-cache`; `If-None-Match` (weak comparison, a list of tags or `*`) that matches the representation's ETag answers 304 with no body and the same `ETag`, `Vary` and `Cache-Control`, after authorization, visibility and the `Accept` decision (an unacceptable `Accept` stays 406); each representation has its own strong validator (`"<sha>"` original, `"<sha>-json"`, `"<sha>-yaml"`), so the original's tag never revalidates a conversion; build metadata in `ref` is ignored (`1.0.0+x` is `1.0.0`) and a ref that is not `latest` or strict SemVer is the unknown 404
- `GET /api/v1/contracts/contracts/{contract_id}/versions/{ref}/validation-report`: `contract:read-published` (the same policy and visibility as the version fetch: a Consumer reads the report of a published version it can fetch, a Policy Manager without a contract role and an editor of another team get the unknown-contract 404); `ref` is `latest` or strict SemVer; 200 `contract_id`, `version_id`, `semver`, `run_id`, `content_sha256`, `engine_version`, `dsl_version`, `schema_sha`, `validated_at`, `pins`, `report` (the stored publish report with `compatibility` and `ref_facts`, see "Validation runs and reports"); 404 `validation-report-not-recorded` for a version published before the publish gate recorded runs; strong `ETag` `"run-<run_id>"` and `Cache-Control: private, no-cache`; a matching `If-None-Match` is 304 only after authorization and visibility; 503 `validation-unavailable` when validation cannot answer (never an empty report); see "Stored validation reports"
- `GET /api/v1/contracts/contracts/{contract_id}/diff`: `contract:read-published`; a ref is strict SemVer, `latest`, or a draft id for a caller holding `contract:read` at the folder; each ref follows the version visibility rules (a Consumer naming an unpublished version or a draft, an unknown draft id and a Policy Manager without a contract role all get the same 404); JSON only, computed on the fly with the breaking-change classifier and never stored; 200 `from` and `to` (`ref`, `kind` `version` or `draft`, `version_id`, `semver`, `content_sha256`), `classifier_version`, `required_bump` (`major`, `minor`, `patch` or `none`, from the change classes), `counts`, `changes` (the report change shape), `changes_total`, `truncated` (more than 10,000 changes), `next_cursor`; `from` newer than `to` is compared in that order; `limit` 1..1000 (default 500) and an opaque `cursor` (422 otherwise); strong `ETag` from both content hashes and the classifier version, `Cache-Control: private, no-cache`, 304 only after authorization and visibility; see "Changelog between two versions"
- `POST /api/v1/contracts/contracts/{contract_id}/versions/{ref}/promote`: `contract:publish` (visible via `contract:read`: a Consumer or Policy Manager gets the unknown-contract 404, a Team reader 403, an archived folder 409 `folder-archived`); write routes address a version by strict SemVer only, the `latest` alias is read-only (404); promotes a published Preview or older version to the Latest (the previous Latest becomes `preview` or `older` by SemVer, exactly one Latest exists at all times) and clears the promoted version's deprecation; 200 with the version's metadata (`status` `latest`); promoting the current Latest is a 200 no-op without an event; 409 `version-not-published` for an unpublished version; the contract row is locked first; audit `contract.version_promoted` (before/after name the old and new Latest id and SemVer, never document content)
- `POST /api/v1/contracts/contracts/{contract_id}/versions/{ref}/unpublish`: `contract:publish` (same visibility and denial rules as promote: Team reader 403, Consumer and Policy Manager the unknown-contract 404, archived folder 409 `folder-archived`; SemVer-only `ref`); hides a published version from every caller without `contract:publish` (list, metadata and fetch answer the unknown-contract 404) while the row and its bytes stay; owners still list and fetch it with `lifecycle` `unpublished`; 200 with the metadata; already unpublished is a 200 no-op without an event; 409 `latest-version-protected` for the current Latest (promote another version first; the database also rejects it); 409 `version-pinned` while any card actively pins the version (`reasons` array naming every pinning card; the cards are notified through a `contract.version_unpublish_blocked` event, throttled per card and version; see "Blocked lifecycle operations"); a number is never reused (a draft declaring it gets 409 `semver-conflict`); the body is optional `{"reason"}` (1 to 500 characters, trimmed, extra fields 422) and **required** while the Global admin setting "require a reason" is on (422 `lifecycle-reason-required`, answered before any lock is taken); audit `contract.version_unpublished` (`after.reason` when given); a refused unpublish with pinning cards records one `contract.version_unpublish_blocked` event (`version_id`, `semver`, `notify_card_ids`)
- `POST /api/v1/contracts/contracts/{contract_id}/versions/{ref}/republish`: `contract:publish` (same visibility and denial rules as promote: Team reader 403, Consumer and Policy Manager the unknown-contract 404, archived folder 409 `folder-archived`; SemVer-only `ref`); restores an unpublished version with the same bytes and ETag as a Preview or an older version by SemVer, never automatically the Latest; 200 with the metadata; republishing a published version is a 200 no-op without an event; 409 `contract-referenced` when the version links to a soft-deleted contract (`deleted_target_count`, never named); audit `contract.version_republished`
- `POST /api/v1/contracts/contracts/{contract_id}/versions/{ref}/deprecate`: `contract:publish` (same visibility and denial rules as promote); JSON body `{"sunset_date": "YYYY-MM-DD", "reason"?}` (unknown fields 422; `reason` 1 to 500 characters, trimmed, required while the Global admin setting "require a reason" is on: 422 `lifecycle-reason-required` before any lock is taken; it is recorded in the event and carried by the deprecated notice as `{reason}`), the date must be today or later in UTC (422 `invalid-sunset-date`); flags a published version as deprecated (the "Deprecated" status is this `deprecated` flag plus `sunset_date`, orthogonal to latest, preview and older); the current Latest may be deprecated; list and metadata show `deprecated` true, `deprecated_at` and `sunset_date`, and every fetch of the version (200 and 304) and its metadata carry `Deprecation: @<unix seconds of deprecated_at>` (RFC 9745) and `Sunset: <HTTP-date at 23:59:59 GMT of the sunset date>` (RFC 8594); the file is never edited and stays fetchable before and after the sunset date; the same date again is a 200 no-op without an event, a different date changes only `sunset_date` (one event, `deprecated_at` kept); promoting a deprecated version clears the deprecation; 409 `version-not-published` for an unpublished version; audit `contract.version_deprecated` (`after.reason` when given)

### Rule impact

- `POST /api/v1/contracts/governance/rule-set-dry-runs`: `ruleset:dry-run` (Policy Manager, Global admin) at the rule set's folder, which contracts learns from validation's `GET /api/v1/validation/rule-sets/{rule_set_id}` with the caller's token: an unknown id, a rule set the caller cannot read and a platform set are the same 404, and no contract is queried before the check. JSON body `{rule_set_id, version: n \| "draft", folder_id?, cursor?}`, read only after authentication and capped at `DC_IMPACT_REQUEST_MAX_BYTES` (4096; 413 `payload-too-large` above it); unknown fields, a version other than a positive integer or `"draft"` and a malformed cursor are 422 `validation-error`. Lists the next page of non-deleted contracts with a Latest version under the rule set's folder (and under `folder_id` when given; a `folder_id` outside the rule set's subtree is an empty page with no evaluation) inside the caller's `ruleset:dry-run` scope, and evaluates each Latest as if the version were enforced: 200 `{rule_set_id, version, scope_folder_id, items: [{contract_id, name, odcs_id, folder_id, latest_semver, status, would_block, counts {fail, error, warn}, failing_rules}], summary {would_fail, would_error, pass, hidden}, next_cursor, total_in_scope}`; JSON Pointers only for contracts in the caller's `contract:read` scope (otherwise `pointers_withheld: true`); every answered call writes one counts-only audit event `governance.rule_set_dry_run`; see "Rule impact"
- `GET /api/v1/contracts/governance/drift`: `governance:drift-read` (Policy Manager, Org admin, Global admin) at `folder_id`; a missing or malformed `folder_id` is 422 before any authorisation call, a folder the caller cannot see 404, a folder reader without the permission 403. Pages through the published contracts under the folder inside the caller's `governance:drift-read` scope and evaluates each Latest against the current mandatory resolution of its own folder: 200 `{folder_id, items: [{contract_id, name, odcs_id, folder_id, latest_semver, status: drifted\|at_risk\|unparseable, counts, failing_rules, pinned_rule_sets, current_versions: [{rule_set_id, name, version_no, mode}]}], summary {drifted, at_risk, compliant, unparseable, hidden}, next_cursor, total_in_scope}`; compliant contracts are counted, not listed. JSON Pointers only for contracts in the caller's `contract:read` scope (otherwise `pointers_withheld: true`). Purely informational: no state change and no notice, but every answered call writes one counts-only audit event `governance.drift_read`; see "Rule impact"

### Subscribers of a contract

- `GET /api/v1/contracts/contracts/{contract_id}/subscribers`: `contract:read` at the contract's folder (Team reader and up, Org and Global reader, editors, admins); a Consumer, a Policy Manager, a caller who cannot read the contract and an unknown or deleted contract all get the same unknown-contract 404; the cards with an **active** pin on the contract (ended pins never appear), keyset-paged: `limit` (1..100, default 50) and an opaque `cursor`, no total, 422 for an invalid limit or cursor; pages follow subscription-id (uuidv7) order and each page is grouped by version, so a version's cards may continue on the next page and the same `version_id` can appear on consecutive pages (clients merge entries by `version_id`) while walking every page visits each active pin exactly once; body `{contract_id, versions, next_cursor}` where each version is `{version_id, semver, deprecated, sunset_date, cards}` (SemVer precedence descending within a page) and each card is exactly `{card_id, subscription_id, name, description, contacts, pinned_at, pinned_by}` (`subscription_id` is the pin's own id, the one the remove route takes) (cards by `pinned_at`, then `card_id`; `contacts` as stored); a card of another organization shows only its pins on this contract (SC3): never its members, delivery channels, webhook URL or its subscriptions to other contracts; a contract with no active pins is `{"contract_id": ..., "versions": [], "next_cursor": null}`
- `POST /api/v1/contracts/contracts/{contract_id}/subscriptions/{subscription_id}/remove`: `contract:publish` at the contract's folder, no new permission: a Team reader gets 403, a Consumer, a Policy Manager and a caller who cannot read the contract the unknown-contract 404, an archived folder 409 `folder-archived`; JSON body `{"reason": "..."}` is required (stripped, 1..500 characters, control characters other than newline refused, unknown fields 422); a subscription of another contract or an unknown id is the unknown 404; ends the pin with `end_reason` `removed_by_admin`, `end_note` the reason and `ended_by` the caller: 200 with the ended pin (same body as unsubscribe) and one `card.subscription_removed` event (aggregate the card, scoped to the contract's folder path, `after` carries `card_id`, `contract_id`, the subscription `id`, `version_id`, `semver` and `reason`); the request writes no notice, the card is told once through that event as a `card.subscription.removed` notice carrying the reason and `removed_by`; removing an already ended pin is a 200 no-op without an event or notice and leaves its end reason and note unchanged; the contract row is locked first

### Templates

- `POST /api/v1/contracts/folders/{folder_id}/templates`: `template:create`; body is the raw document; audit `template.created`
- `GET /api/v1/contracts/templates`: `template:list` (principal-level); optional `folder_id`, keyset-paginated
- `GET /api/v1/contracts/templates/{template_id}`: `template:read-published`; draft fields null and `draft_redacted` true without `template:read`; then `name`, `updated_by` and `updated_at` come from the latest published version
- `GET /api/v1/contracts/templates/{template_id}/document`: `template:read`; the draft bytes
- `PUT /api/v1/contracts/templates/{template_id}/draft`: `template:update`; replaces the draft, last-write-wins; audit `template.draft_updated`; an optional `name` query parameter follows the name rule (see "Names")
- `GET /api/v1/contracts/templates/{template_id}/versions/{version_no}/document`: `template:read-published`; the published bytes
- `GET /api/v1/contracts/folders/{folder_id}/available-templates`: `contract:create`; published, non-deprecated templates held at the folder or an ancestor
- `GET /api/v1/contracts/folders/{folder_id}/available-templates/{template_id}/versions/{version_no}/document`: `contract:create`; the published bytes of an available version

### Subscriber card routes

- `POST /api/v1/contracts/folders/{folder_id}/cards`: `card:create` at the folder (held by Consumers and by Team, Project, Organization and Global admins; a Team editor or reader of the folder gets 403, a caller who cannot read the folder 404); JSON body `name` (1..200), `description` (1..2000), optional `contacts` and `email_enabled`; 201 with the card and `Location` `/api/v1/contracts/cards/{id}`; the card belongs to the folder (any folder kind); 409 `card-name-taken` for a second active card with the same name (case-insensitive) in the folder; 409 `folder-archived`; audit `card.created` (the snapshot never carries a webhook URL); `name` follows the name rule (see "Names")
- `GET /api/v1/contracts/cards/{card_id}`: `card:read` (principal-level) and then the handler: anyone who can read the owning folder gets the card; every other caller and an unknown id get the same unknown-card 404; see "Subscriber cards" for the outage rule; `webhook_url` is the full stored URL only for a manager (the creator or a `card:manage` holder) and scheme plus host (for example `https://hooks.example.test`) for every other reader and member, because provider URLs can carry tokens in the path or query; `webhook_secret_configured` says whether a signing secret exists and the secret itself is never in a read
- `GET /api/v1/contracts/folders/{folder_id}/cards`: `folder:read` at the folder (a caller who cannot read it gets 404); the folder's cards in id order (uuidv7, so creation order), keyset-paginated with `limit` (1..100, default 50) and `cursor`, no total; archived cards are hidden unless `include_archived=true`; an empty folder is `{"items": [], "next_cursor": null}`
- `GET /api/v1/contracts/cards`: `card:read` (principal-level, any authenticated caller); every card the caller could open by id, across folders: folders in `accessible_folders(folder:read)` plus cards the caller is a linked member of; an empty page, never 403, when nothing is visible. Filters `folder_id` with `subtree` (default `true`: that folder and everything below it; an unreadable folder is an empty page), `q` (name or description contains, at most 200 characters), `include_archived` (default `false`), `has_active_subscriptions`; id order with `limit` (1..100, default 50) and `cursor`. Each item carries the folder id, its root-first `folder_ancestor_ids`, `active_subscription_count` and the newest `pin_preview_limit` (5) active pins (`contract_id`, `version_id`, `semver`, `contract_name` only when the caller may list the contract, else null). `total_cards` and `total_folders` count the filtered visible cards only. No audit or access event (no document content)
- `PATCH /api/v1/contracts/cards/{card_id}`: `card:write` (principal-level) and then the manager rule: the card's creator or a holder of `card:manage` at its folder (Team, Project, Organization and Global admins) edits `name`, `description`, `contacts`, `email_enabled`, `webhook_url` and `webhook_enabled`; a folder reader who is neither gets 403 `forbidden` (readable but not permitted), a caller who cannot read the card the unknown-card 404, a creator who lost access to the folder 404 as well; only the fields present change and an explicit null is 422; a request that changes nothing is a 200 no-op without an event; 409 `card-archived`, 409 `card-name-taken`, 409 `folder-archived`; a read-only PAT is refused with 403; the webhook fields follow "Webhook channel": a URL that fails the rules is 422 `invalid-webhook-url` (never echoed), a host that is not on the Global-admin list of allowed webhook hosts is 422 `webhook-host-not-allowed` (the list is mandatory: an empty list allows no host, so card owners cannot add a webhook until an admin lists its host in Settings > Notifications, and an IP-literal URL is always `invalid-webhook-url`; every other rule still applies to a listed host; checked after the URL shape and before DNS; the list is read from identity with the caller's bearer on every URL edit, and when it cannot be read at all, or the one-time first-start seed of the list from the existing channels is not confirmed yet, the call is 503 `settings-unavailable`), enabling a webhook with no URL is the same 422, enabling without a deployment key is 503 `service-unavailable`; the call that enables the webhook for the first time answers with `webhook_secret` (a `whsec_` value, shown once, encrypted at rest with Fernet), every other answer has `webhook_secret` null, and issuing that first secret needs an interactive sign-in: a PAT or a key gets 403 `interactive-session-required` and nothing changes, while every other edit, including disabling or re-enabling a webhook that already has a secret, works with a manager's read-write PAT; audit `card.updated` (before and after allow-listed fields, plus `webhook_host` and `webhook_secret_generation` when a URL is set; never the URL, the secret or its encrypted form); a `name`, when sent, follows the name rule (see "Names")
- `POST /api/v1/contracts/cards/{card_id}/archive`: `card:write` and the same manager rule and denials as PATCH; archives the card (it is never deleted, and its name becomes reusable); 200 with the card; archiving an archived card is a 200 no-op without an event; 409 `folder-archived`; audit `card.archived`
- `POST /api/v1/contracts/cards/{card_id}/webhook-secret`: `card:manage-secret` (principal-level, interactive sign-in only): a PAT or a service-account key is refused with 403 `interactive-session-required` before the card is looked up, so the refusal is the same for every card id, and a read-only PAT gets 403 `forbidden`; then the manager rule of PATCH: a signed-in member who is not a manager gets 403 `forbidden`, a caller who cannot read the card the unknown-card 404; no request body; rotates the signing secret: 200 with `{webhook_secret, generation, previous_valid_until}` where `webhook_secret` is the new `whsec_` value, shown once, and the previous secret stays valid until `previous_valid_until` (now plus `DC_WEBHOOK_SECRET_OVERLAP_S`, default 86400; only one previous secret is kept, so a second rotation inside the window ends the oldest); 409 `webhook-not-configured` for a card with no webhook URL, 409 `card-archived`, 409 `folder-archived`; 503 `service-unavailable` when identity cannot answer or the deployment has no `webhook_secret_key`; audit `card.webhook_secret_rotated` (the generation before and after, never a secret)
- `POST /api/v1/contracts/cards/{card_id}/members`: `card:write` and the manager rule of PATCH: a linked member who is not the creator or a folder admin gets 403 `forbidden`, a caller who cannot read the card the unknown-card 404; JSON body `member_type` (`local_group`, `entra_group` or `service_account`) and `member_id` (1..200 characters), unknown fields 422; identity's `members/check` decides eligibility, so a group or account owned outside the card's Organization, an unknown, inactive or malformed id and an Entra group Graph does not know are all 422 `invalid-member` with no hint which; 201 with `{member_type, member_id, added_by, added_at}`, 200 when the member was already linked (no change, no event); 409 `card-archived`, 409 `folder-archived`; 503 when identity cannot answer (nothing is written); audit `card.member_added`
- `DELETE /api/v1/contracts/cards/{card_id}/members/{member_type}/{member_id}`: `card:write` and the same manager rule and denials as POST members (a linked member who is not a manager gets 403 `forbidden`, a caller who cannot read the card 404); `member_type` is `local_group`, `entra_group` or `service_account` (422 `validation-error` otherwise); 204 without a body, also when the member was not linked (a no-op without an event); the unlinked principal loses member access at once; 409 `card-archived`, 409 `folder-archived`; audit `card.member_removed`
- `POST /api/v1/contracts/cards/{card_id}/subscriptions`: `card:write` and the capability rule: a card manager or a linked member subscribes, a folder reader who is neither gets 403 `forbidden`, a caller who cannot read the card the unknown-card 404; JSON body `contract_id`, `version` (a concrete SemVer, never `latest`) and optional `mode` (`pinned` only), unknown fields 422; the acting principal must itself hold `contract:read-published` at the contract's folder, otherwise the same 404 as an unknown contract, so the card adds no authority; Latest, older and Preview versions may be pinned; 201 with `{id, card_id, contract_id, version_id, semver, mode, pinned_by, pinned_at, ended_by, ended_at, end_reason, end_note, replaced_by_id}` and `Location` `/api/v1/contracts/cards/{card_id}/subscriptions/{id}`; pinning a version the card already actively pins is a 200 no-op (no row, no event); 404 for `latest`, a non-SemVer reference or an unknown version; 409 `version-deprecated` for a deprecated version (existing pins stay), 409 `version-not-published` for an unpublished version to a caller who may see unpublished versions (404 for everyone else), 409 `card-archived`, 409 `folder-archived` (managers only); 503 when identity cannot answer for a caller who already sees the card; audit `card.subscribed` (before null, after the pin and its SemVer)
- `POST /api/v1/contracts/cards/{card_id}/subscriptions/{subscription_id}/move`: `card:write` and the same capability rule and denials as subscribe; JSON body `{version}` (a concrete SemVer, unknown fields such as `contract_id` are 422); the target obeys the subscribe rules (the acting principal's own read access, not deprecated, published); atomically ends the old pin with `end_reason` `moved` and `replaced_by_id` set, and starts the new one: 200 with the pin now active, one `card.subscription_moved` event (before the old pin, after the new one); moving to the pinned version is a 200 no-op without an event; moving to a version the card already pins ends the old pin as `moved` with `replaced_by_id` set to the existing pin and adds no row; 409 `subscription-ended` for an ended pin, 409 `version-deprecated`, 409 `card-archived`; a pin of another card is the unknown 404
- `POST /api/v1/contracts/cards/{card_id}/subscriptions/{subscription_id}/unsubscribe`: `card:write` and the same capability rule and denials as subscribe; optional JSON body `{note}` (1..500 characters, no control characters); ends the pin with `end_reason` `unsubscribed` and keeps the note, never deleting the row: 200 with the ended pin and one `card.unsubscribed` event; unsubscribing an ended pin is a 200 no-op without an event; 409 `card-archived`; a pin of another card or an unreadable card is the unknown 404
- `GET /api/v1/contracts/cards/{card_id}/subscriptions`: `card:read` and the card visibility rule (the unknown-card 404 for everyone else); the complete pin record, active and ended, newest first (uuidv7 id order); `state` `active`, `ended` or `all` (default), `limit` (1..100, default 50) and a keyset `cursor`, no total; a card without pins is `{"items": [], "next_cursor": null}`. This is the full-record path: the card read embeds at most 500 rows and sets `subscriptions_truncated`, so clients page this route for the complete history
- `GET /api/v1/contracts/cards/{card_id}/notices`: `card:read` and the card visibility rule (the unknown-card 404 for everyone else, identical bodies); the card's recorded notices, newest first (uuidv7 id order), each `{id, event_type, contract_id, version_id, occurred_at, created_at, payload, deliveries}` with deliveries ordered by channel as `{channel, status, attempts, last_attempt_at, last_status, delivered_at, last_error}`; `limit` (1..100, default 50) and a keyset `cursor`, no total, and no `since` filter, read position or acknowledgement; a card without notices is `{"items": [], "next_cursor": null}`; 422 for an invalid limit or cursor

### Rule-set routes

- `POST /api/v1/validation/folders/{folder_id}/rule-sets`: `ruleset:create` at the folder (a Policy Manager at the folder or an ancestor; a Team editor or reader gets 403 or 404, a Policy Manager of a sibling Organization 404); JSON body `{name, description}`; 201 with the rule set, `Location` `/api/v1/validation/rule-sets/{id}` and `ETag` `"r0"`; the folder, its kind and ancestry come from identity's folder facts, never from the body; 409 `rule-set-name-taken`, 409 `folder-archived`; audit `ruleset.created`; `name` follows the name rule (see "Names")
- `GET /api/v1/validation/rule-sets/{rule_set_id}`: `ruleset:read` (drafts: authors only; an unknown id and a rule set the caller cannot read are the same 404); the draft rules and `ETag` `"r<draft_revision>"`; no check is run
- `PUT /api/v1/validation/rule-sets/{rule_set_id}/draft`: `ruleset:update`; body is the whole draft as JSON or YAML (`{description, rules}`, `Content-Type` `application/json` or `application/yaml`; unknown fields 422 `validation-error` with JSON pointers, a bad document 422 `malformed-document`, 415, 413 `payload-too-large` above `DC_MAX_DRAFT_BYTES`, default 2000000); `If-Match` is required (428 without, 412 stale); 200 with the draft, the new `ETag` and the advisory `check` report, or `check` `{"status": "unavailable"}` when the sandbox is busy; the save never fails because of rule problems; 409 `folder-archived`; audit `ruleset.draft_updated` (rule keys and content hash only)
- `PUT /api/v1/validation/rule-sets/{rule_set_id}/draft/rules/{rule_key}`: `ruleset:update`; creates or replaces one rule of the draft under the draft's `If-Match` and `ETag` (the same as `PUT .../draft`: 428 without, 412 stale); body is one rule as JSON or YAML, `key` optional, and the path key wins (a different body key is 422 `validation-error`, as is a path key that is not lower case letters, digits and hyphens starting with a letter, and unknown fields); an existing key is replaced in place, a new key is appended; 200 with the draft, the new `ETag` and the advisory `check`; an identical rule is a 200 no-op without an event; 413 `payload-too-large` above twice `DC_MAX_FIXTURE_BYTES` times `DC_MAX_FIXTURES_PER_KIND` plus 64 KiB; 409 `folder-archived`; audit `ruleset.draft_updated`
- `DELETE /api/v1/validation/rule-sets/{rule_set_id}/draft/rules/{rule_key}`: `ruleset:update`; removes one rule from the draft under `If-Match` (428 without, 412 stale); 200 with the draft, the new `ETag` and the advisory `check`; a key that is not in the draft is 404 `not-found`; 409 `folder-archived`; audit `ruleset.draft_updated`
- `POST /api/v1/validation/rule-sets/{rule_set_id}/draft/check`: `ruleset:read`; runs the full check on the stored draft: read only, no write and no event; 200 with the draft and the `check` report; 503 `validation-unavailable` when the sandbox is saturated (`Retry-After: 1`) or cannot start
- `POST /api/v1/validation/rule-sets/{rule_set_id}/publish`: `ruleset:publish`; `If-Match` is required (428 without, 412 stale); checks the draft in the sandbox and freezes exactly the checked content as the next immutable version: 201 with the version and `Location`, 200 with the existing version when the draft equals the newest version (nothing is recorded); 422 `rule-invalid` or `ruleset-fixtures-failed` with the per-rule `check` report; 503 `validation-unavailable` with `Retry-After: 1` when saturated; 409 `folder-archived`; audit `ruleset.version_published`
- `GET /api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}`: `ruleset:read`; a published version with its rules and fixtures (`version_no` 1..2147483647); an unknown version is the same 404
- `GET /api/v1/validation/folders/{folder_id}/rule-sets`: `ruleset:read` at the folder (authors only: a Team editor or reader gets 403 or 404); `{"items": [...]}` with the rule sets held at that folder, not its ancestors, each with `publish_counter` and `draft_revision`, ordered by lower-cased name and then id; a folder without rule sets is `{"items": []}`
- `GET /api/v1/validation/rule-sets/{rule_set_id}/versions`: `ruleset:read`; the published versions newest first as `{version_no, content_sha256, rule_keys, deprecated_at, published_at, published_by}` (no rules); a rule set that was never published is `{"items": []}` and its `versions/1` is 404
- `POST /api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}/deprecate`: `ruleset:deprecate`; no body; stamps `deprecated_at` and `deprecated_by` on that version: 200 with the version; an already deprecated version is a 200 no-op without an event; a deprecated version stays readable and keeps resolving wherever it is pinned; 404 for an unknown version; 409 `folder-archived`; audit `ruleset.version_deprecated`
- `DELETE /api/v1/validation/rule-sets/{rule_set_id}`: `ruleset:update`; deletes a rule set that has no published version: 204 without a body; 409 `rule-set-published` for one that has any (a contract version may pin it, and it is never deleted); 409 `folder-archived`; audit `ruleset.deleted` (before is the allow-listed snapshot)
- `PUT /api/v1/validation/rule-sets/{rule_set_id}/mandatory`: `ruleset:mandate` at the rule set's folder (a Policy Manager or Global admin at that folder or above; a lower level or a sibling Organization gets 403 or 404); JSON body `{"mandatory": true\|false}` (unknown fields 422); 200 with the rule set (`mandatory`, `mandatory_set_at`, `mandatory_set_by`); setting stamps `enforce_at` from the database clock (see "Mandatory rule sets and enforcement" below), 422 `rule-set-not-published` when the set has no published version; repeating the current state is a 200 no-op without an event and moves no date; 409 `folder-archived`; audit `ruleset.mandatory_set` (version, `enforce_at` per version, `restored_version_no`) or `ruleset.mandatory_cleared` (the version that was enforced, the versions still warning)
- `POST /api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}/enforce-now`: `ruleset:mandate`; no body; sets that version's `enforce_at` to the database's now so it blocks at once: 200 with the version; a version that is already enforced is a 200 no-op without an event; 409 `rule-set-not-mandatory` for a version with no `enforce_at` (its set is not mandatory); 404 for an unknown version; 409 `folder-archived`; audit `ruleset.enforced_early` (before and after, with the previous `enforce_at`)
- `POST /api/v1/validation/folders/{folder_id}/platform-rule-sets/{rule_set_id}/versions/{version_no}/enforce-now`: `ruleset:mandate` **at the folder in the path, which must be the ROOT folder** (a Global Policy Manager or Global admin); no body; the same audited write as `enforce-now` above, for the seeded platform rule sets (`odcs-consistency`, `odcs-references`, `odcs-publication`), which hold no folder and are unreachable through the folder-scoped route: 200 with the version, an already enforced one is a 200 no-op without an event; 404 for any other folder, a rule set that is not a platform set or an unknown version (one response); 409 `rule-set-not-mandatory`; audit `ruleset.enforced_early`
- `PUT /api/v1/validation/rule-sets/{rule_set_id}/versions/{version_no}/enforce-at`: `ruleset:mandate` at the rule set's folder; body `{"enforce_at": "<timestamp with a timezone>"}` (a naive timestamp or an unknown field is 422); moves that version's `enforce_at` to any date: a future date makes it warn until then (postponing an enforced version is allowed), now or a past date enforces it at once (a past date is stored as the database's now, never earlier); 200 with the version; the same effective date again, or a past date for an already enforced version, is a 200 no-op without an event; 409 `rule-set-not-mandatory` for a version with no `enforce_at`; 404 for an unknown version or a platform set id; 409 `folder-archived`; audit `ruleset.enforcement_rescheduled` (before and after, with `previous_enforce_at`, `previous_mode`, `mode` and `paused`). `enforce-now` above keeps working unchanged
- `PUT /api/v1/validation/folders/{folder_id}/platform-rule-sets/{rule_set_id}/versions/{version_no}/enforce-at`: `ruleset:mandate` **at the folder in the path, which must be the ROOT folder** (a Global Policy Manager or Global admin); the same body, rules and audited write as the folder-scoped `enforce-at` above, for the platform rule sets (`odcs-structural`, `odcs-consistency`, `odcs-references`, `odcs-publication`): 200 with the version, a repeat is a 200 no-op; 404 for any other folder, a rule set that is not a platform set or an unknown version (one response); 422 for a naive timestamp; 409 `rule-set-not-mandatory`; audit `ruleset.enforcement_rescheduled`
- `PUT /api/v1/validation/folders/{folder_id}/platform-rule-sets/{rule_set_id}/paused`: `ruleset:mandate` **at the folder in the path, which must be the ROOT folder** (a Global Policy Manager or Global admin); body `{"paused": true\|false}` (an unknown field is 422); pauses or resumes a platform rule set (see "Pausing a platform rule set"): 200 with the rule set (`paused_at`, `paused_by`, null when not paused); repeating the current state is a 200 no-op without an event; 404 for any other folder, a rule set that is not a platform set or an unknown id (one response); 409 `rule-set-not-mandatory`; audit `ruleset.paused` or `ruleset.resumed`
- `GET /api/v1/validation/folders/{folder_id}/available-rule-sets`: `ruleset:read-published` checked at this folder (every role that can read the folder holds it: Team editor, Team reader, Consumer, Policy Manager, admins; a caller with no grant gets 404); `{"items": [...]}` with the published rule sets that apply here: those the folder or any ancestor holds plus platform sets, each `{id, scope, folder_id, folder_kind, name, description, latest_version, latest_active_version, versions, mandatory, paused, paused_at, resolved}` where `versions` lists every published version newest first (`{version_no, content_sha256, rule_keys, deprecated_at, enforce_at, published_at, published_by}`), `mandatory` is the rule set's flag and `resolved` is, for a mandatory set, the entries that apply at this folder as `[{version_no, mode, enforce_at, paused}]` (`paused` is true for every entry of a paused platform set, whose mode is then `warn`; the effective-rules view before any contract is validated; `[]` for a set that is not mandatory) and `latest_active_version` is null when every version is deprecated; ordered by holder depth (platform, ROOT, ... the folder itself), then lower-cased name, then id; a set without a published version never appears, and no draft field is ever returned; `[]` when nothing applies
- `GET /api/v1/validation/folders/{folder_id}/available-rule-sets/{rule_set_id}`: `ruleset:read-published` at this folder; one entry of the list above; a set held in a sibling branch or below this folder, one with no published version and an unknown id are the same 404
- `GET /api/v1/validation/folders/{folder_id}/available-rule-sets/{rule_set_id}/versions/{version_no}`: `ruleset:read-published` at this folder; the published version with its rules, fixtures and messages (the same body as `GET .../rule-sets/{id}/versions/{n}`); a deprecated version stays readable; any set or version that is not available here is the same 404

### Audit

- `GET /api/v1/audit/events`: `audit:list` (principal-level; a PAT, a read-only PAT and a service-account key may query); rows trimmed to `accessible_folders(audit:query)` (see the audit visibility section below). Optional filters, combined with AND: `aggregate_type`, `aggregate_id`, `actor_id` (for example `sa:<uuid>`), `service`, `action` (exact, or a dotted prefix when it ends with a dot, such as `contract.version.`), `category` (`change`, `access`, `denial`), `from` and `to` (the half-open window `[from, to)` on `occurred_at`, RFC 3339 with an offset); `limit` 1..200 (default 50) and `cursor` (the opaque `next_cursor` of the previous page). 200 `AuditEventPageOut` `{items: [AuditEventOut], next_cursor}`, newest first by `(occurred_at, id)`; `next_cursor` is non-null exactly when more rows exist; no total. A caller who can see nothing gets `{"items": [], "next_cursor": null}`, never a 403. 422 `validation-error` for a malformed or non-canonical cursor, a time without an offset, `from` after `to`, a NUL character or more than 256 characters in a text filter, a `limit` outside 1..200 or an unknown `category`; `from` equal to `to` is an empty page. 503 `service-unavailable` when identity cannot answer the scope (never an unfiltered page)
- `GET /api/v1/audit/events/{event_id}`: `audit:list`; one `AuditEventOut` when the event is in the caller's `audit:query` scope; an unknown event and an invisible one are the same 404 `not-found` (the visibility predicate and the id are one SQL statement); 503 as above

### Service versions and API docs

- `GET /api/v1/{identity,contracts,validation,audit}/version`: Public, no token. `ServiceVersionOut` `{service, version, commit, built_at}`: the platform version, the git commit and the build time baked in as the `VERSION`, `GIT_SHA` and `BUILD_TIME` image build arguments (`DC_VERSION`, `DC_GIT_SHA`, `DC_BUILD_TIME`; `"dev"` for a local build or an empty value). Never a package version: the Release workflow builds every image of one release with the same `VERSION` (`X.Y.Z` or `X.Y.Z-rc.N`), the 12-character commit and the commit time. The per-service OpenAPI `info.version` is the same `DC_VERSION`; the merged document's stays `v1`, so the committed snapshot does not change per release. `Cache-Control: public, max-age=60`. Added by `create_app(version_route=True)`; mail is not routed and has none. The UI's "Versions" dialog reads all four plus the next route, and marks a component whose version differs from the UI build
- `GET /api/v1/docs/version`: Public, no token. The api-docs container's own build identity, the same `{service: "api-docs", version, commit, built_at}` shape and environment variables, `Cache-Control: public, max-age=60`. It is routed by the existing `PathPrefix(/api/v1/docs/)` rule and is not part of the merged document (api-docs serves it); the UI types it locally
- `GET /api/v1/openapi.json`: Public, no token. The merged OpenAPI 3.1 document of every service's public API (see "OpenAPI documents"); 503 problem+json until the first merge succeeds and while a merge collision exists, never a partial document
- `GET /api/v1/docs/`: Public, no token. Swagger UI over the merged document (the dropdown also lists each service's own document); `/api/v1/docs` without the slash answers 308 to `/api/v1/docs/`. "Try it out" sends the bearer the caller enters under Authorize (JWT, PAT or service-account key) to the real routes; the page calls no external validator

### Routes outside the public prefix

None of these is routed by the gateway or listed in the OpenAPI documents.

- `/healthz`, `/readyz`: Liveness and readiness, per service, not routed publicly
- `/.well-known/jwks.json`: Identity's JWKS endpoint
- `POST /internal/v1/mail/send`: The mail service: `mail:send` (held by the `notifier` role and a Global admin), internal, backend network only, never routed by the gateway. JSON body `{to, subject, text}` (`dc_core.mail.MailSendIn`: 1..20 addresses, a subject without control characters, plain text, nothing else; unknown fields 422); 200 `{accepted, refused, deferred}` (a partial refusal is still 200; `deferred` is the part of `refused` that was a temporary SMTP 4xx refusal), 422 for an invalid message (the offending value is never echoed), 401/403/404 for a caller that may not send, 503 when the SMTP server is unavailable. Called only by the contracts delivery worker, see "Email delivery".
- `GET /internal/v1/contracts/folders/{folder_id}/usage`: Folder usage for a folder delete, called by identity with the caller's forwarded bearer: `folder:delete` at the folder (Global admin only), counts only, internal, never in OpenAPI and never routed by the gateway. `{folder_id, contracts, templates, subscriber_cards, deleted_contracts, archived_subscriber_cards}`: the first three block a delete (contracts not soft-deleted in any lifecycle state drafts included, every template, cards that are not archived), the last two are leftovers a delete purges
- `POST /internal/v1/contracts/folders/{folder_id}/purge`: Permanent removal of a folder's leftovers, called by identity after every blocker is zero: `folder:delete`, forwarded bearer, 409 `folder-not-empty` (with `blockers`) if a blocker reappeared. Removes the soft-deleted contracts with their versions, references, rule-set attachments and ended pins, the archived cards with their members, notices, webhook secret and ended pins, and clears the origin link of copies made from a purged contract. Idempotent; answers the removed counts; audit `folder.purged` (counts only). Immutable rows are deleted under a transaction-local delete bypass (migration 0017) that the triggers honour only for soft-deleted contracts and ended pins of archived cards or deleted contracts
- `GET /internal/v1/validation/folders/{folder_id}/usage`: The same for validation: `{folder_id, rule_sets, unused_rule_sets}`; `rule_sets` counts folder rule sets that are in use (mandatory) and block a delete, `unused_rule_sets` the others
- `POST /internal/v1/validation/folders/{folder_id}/purge`: Permanent removal of the folder's unused rule sets and all their published versions (a transaction-local delete bypass, migration 0008, honoured only for non-mandatory folder sets); 409 `folder-not-empty` while a mandatory set is left; answers `{folder_id, rule_sets, rule_set_versions}`; audit `folder.purged` (counts only)
- `POST /internal/v1/validation/folders/{folder_id}/runs`: The validation run: `contract:validate` checked by validation with the caller's forwarded bearer, internal, never in OpenAPI and never routed by the gateway; see "Validation runs and reports"
- `GET /internal/v1/validation/runs/{run_id}?contract_id=`: The stored publish run for the contracts report fetch: validation authorises it itself with the caller's forwarded bearer, `contract:read-published` at the run's stored folder, the stored contract id equal to `contract_id` and only publishable runs; everything else (unknown id, another contract, a refused publish attempt, a caller without the permission) is the same 404 `not-found`; internal, never in OpenAPI and never routed by the gateway
- `POST /internal/v1/validation/rule-sets/{rule_set_id}/dry-run`: Rule-set dry-run page, called by contracts with the caller's forwarded bearer: `ruleset:dry-run` at the rule set's folder (an unknown id and a platform set are the same 404); a `"draft"` version also needs `ruleset:read` there (404 otherwise); an unknown version number is 404. JSON body `{version: n \| "draft", scope_folder_id, items: [{contract_id, folder_id, media_type, document_b64}]}` (unknown fields 422). `scope_folder_id` must be the rule set's folder or below it, and every item folder must lie under `scope_folder_id` and pass a PDP check of `ruleset:dry-run` with the caller's token, otherwise the whole call is 404. Each document is evaluated as if the version were enforced (source `mandatory`, mode `enforce`): 200 `{rule_set_id, version, items: [{contract_id, status: evaluated\|unparseable, would_block, counts {fail, error, warn}, failing_rules}], totals {would_fail, would_error, pass}}` (an unparseable document counts as `would_error`). A failing rule is `{rule_set_id, version_no (null for a draft), mode, rule_key, severity, outcome, message (the rule author's text, or the fixed text of error_code), error_code, blocking, pointers (at most 20 JSON Pointers), pointers_total}`: never a document value. Page caps `DC_MAX_BATCH_ITEMS` (25), `DC_MAX_BATCH_FOLDERS` (20) and `DC_MAX_BATCH_BYTES` (4 MiB decoded) are 422 `validation-error` at `/items` before any check or evaluation; a body over the base64 size of the byte cap is 413; a saturated batch pool is 503 `validation-unavailable` with `Retry-After: 1`. The batch pool (`DC_BATCH_SLOTS`; unset: half of `DC_MAX_CONCURRENT_RUNS` capped at `DC_EVAL_WORKERS - 1`, at least 1) is refused at start-up when it could hold every worker or every run slot, so a worker is always left for save and publish runs; a page whose wall-clock budget `DC_BATCH_PAGE_BUDGET_S` (25 s, below contracts' 30 s impact timeout) is spent, or whose caller has disconnected, stops before its next document and is 503 `validation-unavailable` with `Retry-After: 1`. Nothing is stored or audited. Internal, never in OpenAPI and never routed by the gateway
- `POST /internal/v1/validation/folders/{folder_id}/drift`: Compliance drift page, called by contracts with the caller's forwarded bearer: `governance:drift-read` at the path folder (Policy Manager, Org admin, Global admin). JSON body `{items: [...]}` (the dry-run item shape); every item folder must lie under the path folder and pass a PDP check of `governance:drift-read`, otherwise 404. Each document is evaluated against the current mandatory resolution of its own folder (the "What applies" rule at the database clock, no attachments, no compatibility): 200 `{folder_id, items: [{contract_id, folder_id, status, counts, failing_rules, current_versions: [{rule_set_id, name, version_no, mode}]}], totals {drifted, at_risk, compliant, unparseable}}`, where `drifted` is a blocking finding under an enforced version, `at_risk` only error-severity findings of warn-mode versions and `compliant` neither. Same caps, 413, 503 and value-free results as the dry-run; nothing is stored, audited or notified. Internal, never in OpenAPI and never routed by the gateway
- `/internal/v1/...`: Service-to-service (`/internal/v1/authz/check`, `/internal/v1/authz/accessible-folders`, `/internal/v1/authz/grantees` (the groups and identities a forwarded caller answers to), `/internal/v1/authz/members/check` (may a group or service account be linked at a folder), `/internal/v1/authz/principal` (resolves a forwarded `dcp_` token, never cached), `/internal/v1/authz/principals/resolve` (up to 100 user and 100 group names, each answered `found`, `not_found` or `unverifiable` and nothing else; 422 over the caps, 429 `too-many-requests` over the per-principal budget), `/internal/v1/events` (`audit:read`, Global admin only, kept for compose-network polling; the public `GET /api/v1/audit/events` is the supported way to read events), `/internal/v1/settings/runtime?sections=a,b` (identity only, `settings:read-runtime`: the stored overrides of the runtime sections other services read, 422 for any other section) and `GET /internal/v1/about` (every service, `settings:read`: version, commit, build time, database state and migration head, answered to the forwarded caller)). Never routed by the gateway.
