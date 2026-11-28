# API guide

This guide explains how the Contractum HTTP API works: where it lives, how to sign in, what you
may do, how contract versions and documents are served, and what an error or a validation report
looks like. For the full list of routes, see the [HTTP API reference](reference/http-api.md). If
you work in Python, the [SDK and CLI guide](sdk-cli.md) wraps most of this for you.

## Base URL and URL scheme

Every public route goes through the gateway under one prefix per module:

```text
https://<your-contractum-host>/api/v1/<module>/...
```

`<module>` is one of `identity` (sign-in, folders, users, groups, grants, tokens, settings),
`contracts` (contracts, drafts, versions, templates, subscriber cards), `validation` (rule sets
and on-demand validation) or `audit` (the audit trail).

Two more public paths sit outside a module:

- `/api/v1/docs/` is the interactive API reference (Swagger UI). Use **Authorize** to paste a
  token, then **Try it out** sends real requests.
- `/api/v1/openapi.json` is the merged OpenAPI 3.1 document of every module.

Each module also answers `GET /api/v1/<module>/version` without a token, and the API docs answer
`GET /api/v1/docs/version`, all with the same shape:

```json
{"service": "identity", "version": "1.4.0", "commit": "0123456789ab", "built_at": "2026-11-21T07:00:00+00:00"}
```

`version` is the platform version: one release gives every component the same version, and a
release candidate reads `1.4.0-rc.1`. A local build reads `dev`. `commit` is the 12-character git
commit and `built_at` the commit time. The answer may be cached for 60 seconds. `contractum
--version` prints the server's version under the CLI's own.

The [HTTP API reference](reference/http-api.md) lists every route with the permission it checks
and whether it needs a token.

## Sign in and tokens

Every route except sign-in, the version routes and the API docs needs a bearer token:

```text
Authorization: Bearer <token>
```

The same header works for every kind of token. Which one you use depends on who you are.

### Local sign-in

A local account signs in with `POST /api/v1/identity/auth/login` and a JSON body with `username`
and `password`. The answer holds:

- `access_token`: a short-lived signed token (15 minutes by default) for the `Authorization`
  header.
- `expires_in`: its lifetime in seconds.
- `refresh_token`: an opaque token that gets a new access token from
  `POST /api/v1/identity/auth/refresh`. Each refresh returns a new refresh token, and the old one
  stops working. Presenting an old one again ends the whole session.

`POST /api/v1/identity/auth/logout` ends the session. `GET /api/v1/identity/auth/policy` tells a
client which sign-in methods are on and which rules a new password must meet.

The very first sign-in with the bootstrap secret gets a restricted token that can only change the
password. Every other call answers 403 `password-rotation-required` until the password is changed.

> [!NOTE]
> The web UI keeps its refresh token in an HttpOnly cookie instead of the response body. It asks
> for this with the header `X-Token-Delivery: cookie` on login, refresh and logout. API clients do
> not need it: without the header you get the token in the body and no cookie.

### Microsoft Entra ID tokens

When your administrator has connected Microsoft Entra ID, a person or an application can call the
API with an Entra v2 access token issued for the Contractum API app registration. Ask your
administrator for the scope to request. Your Entra account, one of your Entra groups, or your
application must be registered in Contractum first; otherwise every call answers 403
`principal-not-registered`. Registration lets you in but grants nothing: access still comes from
role grants.

### Personal access tokens

A personal access token (PAT) is for scripts, pipelines and the CLI. It acts as you, with your
current access, and stops working when you lose that access.

- Create one in the web UI under **Account > Access tokens** (`/account/tokens`) or with
  `POST /api/v1/identity/pats`. Only a person signed in interactively can create one.
- Every token has an expiry date. The longest allowed lifetime is set by your administrator (365
  days by default).
- A token can be **read-only**: it then holds only your read permissions, and every write is
  refused.
- The token is shown once, when you create it. Store it in a secret store.
- A token looks like `dcp_`, then 16 lower-case hex characters, an underscore, and a 43-character
  random secret. The fixed `dcp_` prefix lets secret scanners find leaked tokens.
- You can revoke a token at any time with `DELETE /api/v1/identity/pats/{credential_id}`. Revocation
  takes effect on the next request.

### Service-account keys

A service account is a non-human identity for a shared pipeline. Its id looks like `sa:<uuid>`.
An administrator creates the account and its keys. A key has the same `dcp_` format as a PAT and
is sent the same way. The account holds only what is granted to it, directly or through a local
group. An account can hold two active keys at once, so you can rotate without downtime: create the
second key, move the pipeline to it, then revoke the first.

### Actions that need an interactive sign-in

Managing credentials, group members, registrations, grants and account deactivation needs a person
who signed in with a password or with Entra. A PAT, a service-account key or an Entra application
token is refused with 403 `interactive-session-required`, whatever roles it holds.

## Roles and permissions

Contractum keeps contracts in a folder tree: **ROOT**, then **Organization**, then **Project**,
then **Team**. Contracts, templates, rule sets and subscriber cards can live in a folder of any
kind.

Access comes from **role grants**. A grant gives one role to a user, a group, an Entra group, an
Entra application or a service account at one folder. A grant applies to that folder and to every
folder below it. Grants add up: what you can do is the union of all your grants, direct and
through groups. There are no deny rules.

| Role | Granted at | What it allows, in short |
| --- | --- | --- |
| Global admin | ROOT | Everything, everywhere |
| Global reader | ROOT | Read every folder, contract and published template, and the audit trail |
| Org admin | Organization | Manage folders, grants, groups, service accounts, Entra registrations and subscriber cards in the Organization; full contract access; restore deleted contracts; read the audit trail |
| Org reader | Organization | Read folders and contracts |
| Project admin | Project | Create folders, manage grants and subscriber cards; full contract access; read the audit trail |
| Project editor | Project | Create, edit, publish and delete contracts |
| Team admin | Team | Manage grants and subscriber cards; full contract access; read the audit trail |
| Team editor | Team | Create, edit, publish and delete contracts |
| Team reader | Team | Read folders and contracts |
| Policy Manager | any folder | Author templates and rule sets, make rule sets mandatory, see rule impact; sees contract metadata but never contract content |
| Consumer | any folder | Read published contract versions and create subscriber cards |

Every role that can read a folder can also read the published rule sets that apply there, so an
editor can always read the rule that blocks a publish. An administrator can grant roles only at or
below their own level, and, except a Global admin, never their own admin role.

An Organization or Team folder can **block inheritance**. Then grants from folders above it no
longer apply inside it. A Global admin is never blocked.

To see what you can do, call `GET /api/v1/identity/me` (your identity, groups and grants) and
`GET /api/v1/identity/me/capabilities?folder_id=<id>` (your roles and permissions at one folder).
These answers only inform a client. The server checks every request again.

### Not found or forbidden

Contractum does not reveal that something exists to a caller who may not see it:

- **404 `not-found`**: you cannot see the target. The answer is the same whether it exists or not.
- **403 `forbidden`**: you can see the target, but you may not do this action on it.
- **401 `unauthenticated`**: no token, or the token is invalid, expired or revoked.

List and search routes never answer 403. They return only what you can see, and an empty page when
you can see nothing. They carry no totals that would leak what is hidden.

## Contract versions

A contract has drafts and published versions.

- A **draft** is editable. Only readers, editors and admins of the folder see drafts.
- **Publishing** a draft turns it into a version. A published version is immutable: its bytes never
  change again.
- The document's top-level `version` must be a quoted SemVer 2.0.0 string of at most 255 characters,
  such as `"1.4.0"`, and higher than every earlier version of the contract. A version number is never reused.
- A released version (no prerelease part, such as `"1.4.0"`) must declare `status: active`. A
  prerelease such as `"1.4.0-rc.1"` may keep `draft` or `proposed` and gets a warning. The platform
  rule set `odcs-publication` checks this: a refused publish names `released-status-active` in its
  report. Like every new mandatory version, the check warns during its grace period, then blocks.

Every published version has a status, computed on every read:

| Status | Meaning |
| --- | --- |
| `latest` | The one current version. Every contract with a published version has exactly one. |
| `preview` | A version with a higher SemVer than the Latest: an upcoming release you can test against. |
| `older` | A version with a lower SemVer than the Latest. |

An owner can also act on a published version:

- **Promote** makes a Preview or older version the Latest.
- **Deprecate** flags a version with a sunset date. It stays fetchable, before and after that date.
  Fetches of a deprecated version carry the headers `Deprecation` (RFC 9745) and `Sunset`
  (RFC 8594).
- **Unpublish** hides a version from everyone except owners. The current Latest cannot be
  unpublished. **Republish** brings it back as a Preview or older version.

Each row of the contract list carries `version.deprecated`. It is true when the row's version is
deprecated: the Latest, or the newest draft before the first publish.

To fetch a version, use `latest` or an exact SemVer as `{ref}`:

```text
GET /api/v1/contracts/contracts/{contract_id}/versions/latest
GET /api/v1/contracts/contracts/{contract_id}/versions/1.4.0
GET /api/v1/contracts/contracts/{contract_id}/versions/1.4.0/metadata
GET /api/v1/contracts/contracts/{contract_id}/versions
```

The list returns every version you may see, highest SemVer first. A Consumer sees a contract only
once it has a Latest. `GET .../diff?from=<ref>&to=<ref>` lists the changes between two versions and
says which version bump they need.

## Document formats

Contracts are ODCS documents in YAML or JSON. A version fetch returns the exact stored bytes with
the stored media type. Ask for the other format with `Accept`:

- `Accept: application/json` on a YAML document, or `Accept: application/yaml` on a JSON document,
  returns a converted copy. It holds the same data, but comments and formatting are not kept.
- No `Accept`, `*/*` or the stored type returns the original bytes.
- An `Accept` that names no format the service can produce is 406 `not-acceptable`.

When you upload a document, send `Content-Type: application/yaml` or `application/json`. Another
type is 415 `unsupported-media-type`.

## Caching and conditional requests

Version fetches, draft documents and stored validation reports carry a strong `ETag` and
`Cache-Control: private, no-cache`:

- Send the tag back in `If-None-Match`. If nothing changed, the answer is 304 with no body. Each
  format has its own tag (the original, `-json` and `-yaml`), so one format's tag never matches
  another.
- A 304 is only sent after the permission check, so it never reveals more than a 200 would.

Drafts use the tag `"r<revision>"`. Saving, deleting and publishing a draft need it in `If-Match`:
without the header the answer is 428 `precondition-required`, and with a stale tag it is 412
`precondition-failed`. Fetch the draft again and retry.

Every other JSON answer carries `Cache-Control: no-store`, so no browser or proxy keeps a copy.

## Names

Names you type, such as folder, group, card, rule-set, token and contract names, may contain
letters, marks and numbers of any script, the normal space and `- _ . , ( ) & ' / : # + @`. Emoji,
other symbols, control and invisible characters are refused. Spaces at both ends are trimmed. A
refused name is 422 `validation-error`, and the message names the first bad character as `U+XXXX`.

## Paging

Long lists are paged with a cursor:

- `limit` sets the page size. Most lists allow 1 to 100 and default to 50; the audit trail allows
  up to 200.
- The answer has `items` and `next_cursor`. Pass `next_cursor` back as `cursor` to get the next
  page. It is `null` on the last page.
- The cursor is opaque. Do not build or change it. An invalid cursor is 422 `validation-error`.
- Lists carry no total count.
- `GET /api/v1/contracts/contracts` takes `sort=id` (the default, creation order) or `sort=name`.
  `sort=name` orders by the contract's name ignoring case, then by its ODCS id when it has no name,
  then by its id. Only the first 128 characters of a name take part, so names that match that far
  keep creation order.
- A cursor belongs to the sort it came from. Pass it back with the same `sort`; a cursor with
  another sort is 422 `validation-error`.

## Request IDs and limits

- Send your own `X-Request-ID` header (a UUID). Problem bodies echo it as `request_id`, and the
  audit trail records it, which helps support find your request.
- The gateway refuses a request body above 2 MiB with 413 `payload-too-large`. A single contract
  document is limited to 1 MiB by default.
- The gateway limits the request rate per client. Above it you get 429 `too-many-requests`. The
  sign-in routes have a stricter limit.

## Errors: RFC 9457 problem details

Every error is a JSON body with the media type `application/problem+json` (RFC 9457):

```json
{
  "type": "urn:dc:problem:validation-error",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "The request is not valid.",
  "instance": "/api/v1/identity/folders",
  "request_id": "6f1c8d2e-4a7b-4c1e-9d3f-2b5a8e7c1f04",
  "errors": [{"pointer": "/body/display_name", "detail": "Must not be blank."}]
}
```

- `type` is the stable identifier: `urn:dc:problem:<slug>`. Match on `type`, not on `title` or
  `detail`.
- `errors` lists each invalid field as a JSON Pointer with a message. It appears on 422 answers.
- Some problems add members, such as `report` and `run_id` on a refused publish, `blockers` on a
  refused folder delete, or `reasons` on a refused unpublish or delete.
- Problem bodies never echo a submitted value, a password or a token.

### Errors from the gateway

The gateway itself also answers in problem format for an unknown path (404), a body that is too
large (413), too many requests (429) and upstream failures (502, 503 and 504). These answers have a
generic `detail` and drop headers such as `Retry-After` that the service may have sent.

One case is not problem format: a request that is not valid HTTP, such as a broken `%` escape in
the path, is refused by the gateway with an empty 400 before any routing.

### Problem types

Statuses are the typical ones; the route reference and the interactive docs list what each route
can return.

#### General

| Type | Status | Meaning |
| --- | --- | --- |
| `urn:dc:problem:bad-request` | 400 | The request is malformed in a way no more specific type covers. |
| `urn:dc:problem:invalid-path` | 400 | The path contains an encoded character the API does not accept. |
| `urn:dc:problem:unauthenticated` | 401 | No token, or the token is invalid, expired or revoked. |
| `urn:dc:problem:forbidden` | 403 | You can see the target but may not do this action. |
| `urn:dc:problem:not-found` | 404 | The target does not exist, or you may not see it. |
| `urn:dc:problem:method-not-allowed` | 405 | The path does not support this HTTP method. |
| `urn:dc:problem:not-acceptable` | 406 | The `Accept` header names no format the service can produce. |
| `urn:dc:problem:conflict` | 409 | The request conflicts with the current state, for example a duplicate name. |
| `urn:dc:problem:precondition-failed` | 412 | The `If-Match` tag is stale. Fetch again and retry. |
| `urn:dc:problem:payload-too-large` | 413 | The body or document is above the size limit. |
| `urn:dc:problem:unsupported-media-type` | 415 | Send `application/yaml` or `application/json`. |
| `urn:dc:problem:validation-error` | 422 | A field or parameter is invalid. See `errors`. |
| `urn:dc:problem:precondition-required` | 428 | This write needs an `If-Match` header. |
| `urn:dc:problem:too-many-attempts` | 429 | Too many attempts of a limited action. Wait for `Retry-After`. |
| `urn:dc:problem:too-many-requests` | 429 | The rate limit was reached. Slow down. |
| `urn:dc:problem:internal-error` | 500 | An unexpected error. The detail is generic; quote the `request_id`. |
| `urn:dc:problem:bad-gateway` | 502 | The gateway got no valid answer from a service. |
| `urn:dc:problem:service-unavailable` | 503 | A service the request needs cannot answer. Try again later. |
| `urn:dc:problem:gateway-timeout` | 504 | A service did not answer the gateway in time. |
| `urn:dc:problem:gateway-error` | 4xx or 5xx | Any other error the gateway generates. |

#### Sign-in, accounts and tokens

| Type | Status | Meaning |
| --- | --- | --- |
| `urn:dc:problem:invalid-credentials` | 401 | Wrong username or password, or an unknown, expired or reused refresh token. |
| `urn:dc:problem:password-rotation-required` | 403 | Change the bootstrap password before anything else. |
| `urn:dc:problem:principal-not-registered` | 403 | Your Entra account or application is not registered in Contractum. |
| `urn:dc:problem:interactive-session-required` | 403 | This action needs a person signed in interactively, not a PAT or key. |
| `urn:dc:problem:sign-in-method-disabled` | 403 | An administrator turned this sign-in method off. Tokens and keys are not affected. |
| `urn:dc:problem:csrf-check-failed` | 403 | A browser request failed the cross-site request check. |
| `urn:dc:problem:password-policy` | 422 | The new password does not meet the password rules. |
| `urn:dc:problem:bootstrap-unavailable` | 503 | The bootstrap secret has not been provisioned, so the first sign-in cannot work yet. |
| `urn:dc:problem:bootstrap-consumed` | 409 | The bootstrap secret was already used to set a password. Sign in with that password. |
| `urn:dc:problem:current-session-unknown` | 409 | The current session cannot be identified. Sign in again and retry. |
| `urn:dc:problem:display-name-managed-externally` | 409 | The display name comes from Entra ID, or the account is renamed elsewhere. |
| `urn:dc:problem:last-global-admin` | 409 | The last direct Global admin cannot be revoked or deactivated. |
| `urn:dc:problem:expiry-out-of-range` | 422 | The token or key expiry is in the past or beyond the allowed lifetime. |
| `urn:dc:problem:credential-limit-reached` | 409 | The service account already has two active keys. Revoke one first. |
| `urn:dc:problem:token-limit-reached` | 409 | You already hold the maximum number of active tokens. The body has `limit` and `active_count`. |

#### Folders, grants and groups

| Type | Status | Meaning |
| --- | --- | --- |
| `urn:dc:problem:folder-archived` | 409 | The folder, or one above it, is archived and read-only. |
| `urn:dc:problem:folder-not-empty` | 409 | The folder still holds live content. `blockers` lists each kind with a count. |
| `urn:dc:problem:block-inheritance-not-allowed` | 422 | Only Organization and Team folders can block inheritance. |
| `urn:dc:problem:role-level-mismatch` | 422 | This role cannot be granted at this kind of folder. |
| `urn:dc:problem:grant-ceiling-exceeded` | 403 | The role is above what you may grant at this folder. |
| `urn:dc:problem:self-grant-forbidden` | 403 | You cannot grant a role to yourself, directly or through a group. |
| `urn:dc:problem:unknown-principal` | 422 | The grantee or member does not exist or cannot receive grants. |
| `urn:dc:problem:group-owner-not-allowed` | 422 | A group can only be owned by ROOT or an Organization. |
| `urn:dc:problem:owner-not-allowed` | 422 | This owner must be ROOT or an Organization. |
| `urn:dc:problem:principal-scope-mismatch` | 422 | The service account belongs to another Organization and cannot be used here. |
| `urn:dc:problem:group-scope-mismatch` | 422 | The group or application belongs to another Organization and cannot be granted here. |

#### Platform settings

| Type | Status | Meaning |
| --- | --- | --- |
| `urn:dc:problem:settings-section-not-found` | 404 | There is no settings section with this name. |
| `urn:dc:problem:settings-import-invalid` | 422 | The settings file has an unknown format, version, section or field. |
| `urn:dc:problem:settings-unavailable` | 503 | The list of allowed webhook hosts cannot be read right now. Try again. |
| `urn:dc:problem:signin-lockout-risk` | 409 or 422 | The change would lock admins out: it turns off your own sign-in method, the last one an admin can use, or both methods (422). |

#### Contracts, drafts and versions

| Type | Status | Meaning |
| --- | --- | --- |
| `urn:dc:problem:duplicate-contract-id` | 409 | A contract with this ODCS id already exists in this Organization (or at ROOT). |
| `urn:dc:problem:malformed-document` | 422 | The document is not valid YAML or JSON. |
| `urn:dc:problem:unsupported-odcs-version` | 422 | The document's `apiVersion` is not a supported ODCS version. |
| `urn:dc:problem:odcs-id-mismatch` | 422 | The document `id` must stay the contract's ODCS id. The body names both ids. |
| `urn:dc:problem:invalid-version` | 422 | The document `version` is not a quoted SemVer 2.0.0 string of at most 255 characters. |
| `urn:dc:problem:semver-conflict` | 409 | The version is not higher than every existing version. The body names `highest_semver`. |
| `urn:dc:problem:draft-already-published` | 409 | This draft was already published. The body names `version_id`. |
| `urn:dc:problem:draft-limit-reached` | 409 | The contract holds the maximum number of drafts. Publish or discard one. |
| `urn:dc:problem:last-draft-protected` | 409 | A never-published contract keeps at least one draft. Delete the contract instead. |
| `urn:dc:problem:copy-field-not-editable` | 422 | The source `version` or `id` cannot be changed in place. Copy with `bump: none` and edit it yourself. |
| `urn:dc:problem:version-not-published` | 409 | The action needs a published version, and this one is unpublished. |
| `urn:dc:problem:latest-version-protected` | 409 | The Latest cannot be unpublished. Promote another version first. |
| `urn:dc:problem:invalid-sunset-date` | 422 | The sunset date is in the past. |
| `urn:dc:problem:lifecycle-reason-required` | 422 | An administrator requires a `reason` to unpublish or deprecate. |
| `urn:dc:problem:contract-referenced` | 409 | Another contract's published version links to this one. The body lists the `referrers` you may see and counts the rest. |
| `urn:dc:problem:contract-pinned` | 409 | A subscriber card pins a version of this contract. `reasons` names the cards. |
| `urn:dc:problem:version-pinned` | 409 | A subscriber card pins this version. `reasons` names the cards. |
| `urn:dc:problem:template-version-deprecated` | 422 | A deprecated template version cannot start a contract. |
| `urn:dc:problem:template-override-not-allowed` | 422 | Template overrides may not replace `apiVersion`, `kind` or `id`. |

#### Publishing and validation

| Type | Status | Meaning |
| --- | --- | --- |
| `urn:dc:problem:contract-validation-failed` | 422 | The publish was refused by validation. The body has `report` and `run_id`. |
| `urn:dc:problem:validation-unavailable` | 503 | Validation cannot answer now. Nothing was published. Retry, honouring `Retry-After`. |
| `urn:dc:problem:rules-changed` | 409 | The attached rule sets changed during the publish. Nothing was published. Retry. |
| `urn:dc:problem:baseline-changed` | 409 | The previous version or a linked contract changed during the publish. Nothing was published. Retry. |
| `urn:dc:problem:validation-report-not-recorded` | 404 | This version was published before validation reports were stored. |
| `urn:dc:problem:rule-set-not-attachable` | 422 | The rule set is not a published rule set visible at this folder. |

#### Rule sets

| Type | Status | Meaning |
| --- | --- | --- |
| `urn:dc:problem:rule-set-name-taken` | 409 | Another rule set in the folder has this name. |
| `urn:dc:problem:rule-set-published` | 409 | A rule set with a published version cannot be deleted. Deprecate its versions instead. |
| `urn:dc:problem:rule-set-not-published` | 422 | Publish a version before making the rule set mandatory. |
| `urn:dc:problem:rule-set-not-mandatory` | 409 | The rule set is not mandatory, so it has no enforcement date to change. |
| `urn:dc:problem:rule-invalid` | 422 | The rule set failed its check. The `check` member lists the problems per rule. |
| `urn:dc:problem:ruleset-fixtures-failed` | 422 | The rule set failed only because of its test fixtures. See `check`. |

#### Subscriber cards

| Type | Status | Meaning |
| --- | --- | --- |
| `urn:dc:problem:card-name-taken` | 409 | Another active card in the folder has this name. |
| `urn:dc:problem:card-archived` | 409 | An archived card cannot be changed. |
| `urn:dc:problem:invalid-member` | 422 | This group or service account cannot be linked to the card. |
| `urn:dc:problem:invalid-webhook-url` | 422 | The webhook URL is not allowed (https, a public host, no credentials in the URL). |
| `urn:dc:problem:webhook-host-not-allowed` | 422 | The webhook host is not on the administrator's list of allowed hosts. |
| `urn:dc:problem:webhook-not-configured` | 409 | Set a webhook URL before asking for a webhook secret. |
| `urn:dc:problem:version-deprecated` | 409 | A deprecated version cannot be newly pinned. Existing pins stay. |
| `urn:dc:problem:subscription-ended` | 409 | The subscription has ended and cannot be moved. |

Two types are retired and no route returns them any more: `urn:dc:problem:contract-folder-not-team`
and `urn:dc:problem:card-folder-not-supported`. Contracts and cards may now live in a folder of any
kind.

## Validation reports

Saving a draft, validating on demand and publishing all return the same report shape:

```json
{
  "status": "completed",
  "structural": {
    "valid": true,
    "error_count": 0,
    "errors": []
  },
  "findings": [
    {
      "rule_set": {"id": "...", "name": "Org naming", "folder_id": "...",
                   "folder_kind": "organization", "scope": "folder", "version": 1},
      "source": "mandatory",
      "mode": "enforce",
      "enforce_at": null,
      "paused": false,
      "rule_key": "unique-property-names",
      "severity": "error",
      "outcome": "fail",
      "message": "Property names must be unique.",
      "location": {"pointer": "/schema/0", "path": "$.schema[0]"},
      "blocking": true,
      "error_code": null
    }
  ],
  "findings_total": 1,
  "findings_truncated": false,
  "rule_sets": [{"rule_set_id": "...", "version": 1, "source": "mandatory", "mode": "enforce",
                 "paused": false, "passed": 0, "failed": 1, "errored": 0}],
  "summary": {"publishable": false, "blocking_count": 1}
}
```

- `structural` holds the ODCS JSON Schema errors, each with a JSON Pointer, the schema keyword and
  a fixed message. Any structural error makes the document unpublishable.
- `findings` lists rule results. Each finding names the rule set (name, folder and version), the
  `rule_key`, the `severity` (`error`, `warning` or `info`), the `outcome` (`fail` or `error`) and
  the location as a JSON Pointer and a JSONPath.
- `source` says why the rule ran: `mandatory` (it applies to the folder), `explicit` (attached to
  this contract), `adhoc` (you asked for it on a validate call) or `native` (a platform check).
- `mode` is `enforce` or `warn`. A new mandatory rule-set version warns for a grace period (14 days
  by default) before it enforces.
- `blocking` is true only for an `error`-severity finding that is enforced. Warnings, info and any
  finding in `warn` mode never block. A rule that fails to evaluate (`outcome: error`, with an
  `error_code` such as `timeout`) blocks where a failure would block.
- `summary.publishable` is true when there is no structural error and `blocking_count` is 0.
- Reports never quote the document. Messages are the rule author's text or a fixed text.

Reports also carry a `compatibility` section that compares the document with the previous
published version. It lists each change as `breaking`, `warning` or `info` and gives
`required_bump`. A breaking change needs a new major version; otherwise the report carries the blocking finding `semver-breaking-change`.

To read the report a version was published with, call
`GET /api/v1/contracts/contracts/{contract_id}/versions/{ref}/validation-report`.

### Validate a document on demand

`POST /api/v1/validation/folders/{folder_id}/validate` checks a document against the rules of a
folder and stores nothing. Send the raw YAML or JSON as the body. Add `?rule_set=<id>` or
`?rule_set=<id>@<version>` (repeatable) to try extra published rule sets. A stored draft can be
checked with `POST /api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/validate`.

### Which refusal slug

When a **contract publish** is refused, the type tells you what to do:

| Type | What happened | What to do |
| --- | --- | --- |
| `contract-validation-failed` (422) | The document fails its ODCS schema, an enforced mandatory rule, an attached error-severity rule, or the breaking-change check. | Read `report`, fix the draft, publish again. |
| `validation-unavailable` (503) | Validation could not give a verdict. | Retry later. Publishing never passes without a verdict. |
| `rules-changed` or `baseline-changed` (409) | Something the check relied on changed before the publish finished. | Retry the publish. |
| `precondition-failed` (412) | The draft changed since you read it. | Fetch the draft and its new `ETag`, then retry. |

When a **rule-set publish** is refused with 422, the slug is `ruleset-fixtures-failed` only when
every problem is about the rule set's test fixtures (a missing pass or fail fixture, too many or
too large fixtures, or a fixture that does not evaluate as declared). Any other problem, such as a
compile error, a size limit, an empty set or a duplicate key, makes the whole refusal
`rule-invalid`. In both cases the `check` member lists the problems per rule, and no version is
created.

## Reading the audit trail

`GET /api/v1/audit/events` shows who changed, read or was refused what, and when. Filter with
`aggregate_type`, `aggregate_id`, `actor_id`, `action`, `category` (`change`, `access` or
`denial`) and the time window `from` and `to`. You see the events of the folders where you hold an
admin or Global reader role; everyone else gets an empty page. For example,
`?actor_id=sa:<uuid>&category=access` shows what one pipeline read.

## Where to go next

- [HTTP API reference](reference/http-api.md): every route, its permission and whether it needs a
  token.
- [SDK and CLI guide](sdk-cli.md): fetch and test contracts from Python or the command line.
- [Writing rules](writing-rules.md): how Policy Managers write rule sets.
