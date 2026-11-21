# Microsoft Entra ID setup

This guide takes you from an empty Entra tenant to a Contractum release that admits Entra users and
service principals. It uses placeholders only: replace `<tenant-id>`, `<api-client-id>`,
`<graph-client-id>` and `<spa-client-id>` with your own GUIDs. No secret value belongs in a values
file or a repository: the Graph client secret is a key in the identity Kubernetes Secret.

**How access works.** A token only proves who the caller is. What they may do comes from
registration in Contractum (admission) and from grants, which identity resolves against Microsoft
Graph. That is why the API app emits no groups claim, and why identity needs its own Graph
application.

To try Entra sign-in locally without a tenant, the compose development stack ships a mock identity
provider. See [the local stack guide](../dev/local-stack.md#mock-identity-provider-and-fake-graph).
Never use it in production.

## 1. App registrations

Create these registrations in the tenant (Entra admin center, App registrations).

### 1.1 API app (the audience of every token)

1. New registration, supported account types "Accounts in this organizational directory only"
   (single tenant). Note the Application (client) ID: this is `<api-client-id>`.
2. Expose an API: set the Application ID URI (the default `api://<api-client-id>` is fine) and add
   one delegated scope, for example `Contracts.Access`, so a user token can be requested.
3. App roles: create one application role named `Contracts.Access` with "Allowed member types:
   Applications", for daemon callers. Contractum does not require `roles` in a token (admission
   by registration is the gate), but assigning the role is how a daemon is allowed to ask for a
   token for this API.
4. Manifest: set

   ```json
   {
     "requestedAccessTokenVersion": 2,
     "groupMembershipClaims": null
   }
   ```

   `requestedAccessTokenVersion` is the current name of the legacy `accessTokenAcceptedVersion`.
   Left at `null` or `1`, Entra issues v1 tokens (a different issuer and audience form), and
   Contractum answers 401 to every one of them. `groupMembershipClaims` must stay `null`: group
   membership always comes from Graph, never from a token. This also removes the 200-group overage
   case from the token.

### 1.2 Identity Graph app (how identity reads the directory)

1. New registration, single tenant. Make it multi-tenant only when you connect several tenants
   (see [Connect another tenant](#23-connect-another-tenant)). Note its Application (client) ID:
   this is `<graph-client-id>`.
2. Certificates and secrets: create a client secret and copy its value once into the identity
   Secret (see [Identity's Graph settings](#22-identitys-graph-settings)). Do not paste it
   anywhere else.
3. API permissions, Microsoft Graph, **Application permissions** (not delegated), exactly these
   three, then "Grant admin consent for <tenant>":

   | Permission | Used for |
   |------------|----------|
   | `User.Read.All` | `GET /users/{id}/transitiveMemberOf` and `accountEnabled` of a user; also `GET /users?$search` (admin directory search) |
   | `Application.Read.All` | `GET /servicePrincipals/{id}/transitiveMemberOf` and `accountEnabled` of a service principal; also `GET /servicePrincipals?$search` and `?$filter=appId eq '<guid>'` (admin directory search) |
   | `GroupMember.Read.All` | `GET /groups/{id}` when a group is registered; also `GET /groups?$search` (admin directory search) and `GET /groups/{id}/transitiveMembers` (the live member list on an Entra group's page) |

   All three are read-only. Every directory search sends `ConsistencyLevel: eventual` (Graph
   requires it for `$search`), asks for at most 20 items and never follows a next link.
   `Directory.Read.All` is broader than needed and is not requested, not even as a fallback.

   **Permission exceptions.** `GroupMember.Read.All` is expected to cover `GET /groups/{id}`, which
   identity calls when a group is registered. If your tenant refuses it, registering a group
   (`POST /api/v1/identity/entra/groups`) answers 503, because identity treats a Graph 401 or 403
   as an outage. The only known fallback is adding the read-only application permission
   `Group.Read.All`. Add it, or any other permission beyond the three, only after your security
   team approves it, and record the approval (who, when, the exact permission, the failing call and
   its error).

   The same applies to the admin directory search. Microsoft lists `GroupMember.Read.All` as the
   least-privileged application permission for listing groups, so group search should work with
   it. If the tenant refuses `GET /groups?$search`, searching groups answers 503 (the dialog says
   the directory cannot be searched), and entering a group's object ID still registers it.

   The Entra group page lists the group's members live, from `GET /groups/{id}/transitiveMembers`
   with `$select=id,displayName,mail,userPrincipalName,appId,accountEnabled`, `$count=true` and
   `ConsistencyLevel: eventual`. Nested groups are expanded, at most 200 members are shown, and
   the answer is cached for 30 seconds. `GroupMember.Read.All` covers it. Contractum never stores
   Entra membership, so the list is always what Entra reports now. A Graph refusal or outage shows
   a warning on the card, not an error page.

### 1.3 Clients for testing and pipelines

- A delegated client: a SPA or native registration with the redirect URI your test tool needs and
  permission to the API app's `Contracts.Access` scope, for user access tokens.
- A daemon client: a registration with a client secret (or certificate), assigned the API app's
  `Contracts.Access` application role (Enterprise applications, the API app, Users and groups, or
  the client's API permissions with admin consent). It requests tokens with
  `scope=<api-client-id>/.default`.

A token for this API must be a **v2 access token**: `ver` is `2.0`, `iss` is
`https://login.microsoftonline.com/<tenant-id>/v2.0` and `aud` is the bare `<api-client-id>`
GUID. An ID token for the same user (no `scp`) is refused.

## 2. Configure the release

### 2.1 The issuer entry

The chart passes `auth.issuers` to every service as `DC_AUTH_ISSUERS`. Add one entry for the
tenant and keep the local issuer as the first entry:

```yaml
auth:
  issuers:
    - issuer: http://identity:8000
      jwks_uri: http://identity:8000/.well-known/jwks.json
      audiences: [contractum]
      namespace: local
    - issuer: https://login.microsoftonline.com/<tenant-id>/v2.0
      jwks_uri: https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys
      audiences: ["<api-client-id>"]
      namespace: entra
      kind: entra
      tenant_id: "<tenant-id>"
```

`tenant_id` is a lowercase GUID. The namespaces `entra` and `sa` are reserved: no generic OIDC
entry may use them. The gateway does no authentication, so this entry is the only place a tenant
is trusted.

### 2.2 Identity's Graph settings

| Setting | How to set it |
|---------|---------------|
| Graph client id | `auth.graph.clientId: "<graph-client-id>"` (the chart sets `DC_GRAPH_CLIENT_ID`) |
| Graph client secret | the key `graph_client_secret` in the `dc-identity` Secret, holding only the client secret value |
| Graph base URL | `auth.graph.baseUrl`, default `https://graph.microsoft.com/v1.0`; leave it |
| Graph token URL | `auth.graph.tokenUrlTemplate`, default `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token` (must contain `{tenant_id}`); change it only for a sovereign cloud or a proxy |
| Graph scope | `DC_GRAPH_SCOPE` through `identity.extraEnv`, default `https://graph.microsoft.com/.default` |
| Directory cache | `DC_DIRECTORY_CACHE_TTL_S` through `identity.extraEnv`, default `60`; the bound on revocation lag (see [Operations](#4-operations)) |

`create-secrets.sh` puts a random placeholder into `graph_client_secret`. Replace it with the real
client secret, for example:

```bash
kubectl -n contractum get secret dc-identity -o json \
  | jq --arg v "$(printf %s "$GRAPH_CLIENT_SECRET" | base64 | tr -d '\n')" '.data.graph_client_secret=$v' \
  | kubectl apply -f -
kubectl -n contractum rollout restart deployment/identity
```

Only identity talks to Graph, so only identity needs the secret.

**Network.** Identity must reach `login.microsoftonline.com` and `graph.microsoft.com`, and every
service must reach `login.microsoftonline.com` for the token signing keys. The chart restricts no
egress, but your platform may: allow these hosts there.

**Startup refusal.** An Entra issuer in `auth.issuers` without `auth.graph.clientId` makes
identity refuse to start ("an Entra issuer is configured but no Graph directory: set
DC_GRAPH_CLIENT_ID"). If identity stops starting after you add the entry, set the client id and
the secret.

### 2.3 Connect another tenant

A second tenant is configuration only:

1. Add one more issuer entry with its own `tenant_id`, its own `<api-client-id>` for that tenant's
   API registration, and its own namespace such as `entra-<name>`.
2. Make the Graph app multi-tenant and grant admin consent for its three application permissions
   in that tenant (admin consent URL:
   `https://login.microsoftonline.com/<other-tenant-id>/adminconsent?client_id=<graph-client-id>`).
   Identity requests one client-credentials token per tenant from that tenant's token endpoint.
3. Pass `tenant_id` when you register groups, users and service principals. It is required once
   several tenants are configured, unless a default tenant is set (see
   [Settings in the web app](#41-settings-in-the-web-app)).

Principal ids have the form `entra:<tid>:<oid>`, so two tenants never collide.

## 3. Admission

Nobody signs in just because their tenant is trusted. Before a person or pipeline can use
Contractum, a Global admin or the owning folder's admin registers them. In the web app, use the
**New** menu on **Administration > Users**, **Groups** or **Service accounts**. Through the API:

- a group: `POST /api/v1/identity/entra/groups` (every transitive member is admitted);
- a user: `POST /api/v1/identity/entra/users`;
- a service principal: `POST /api/v1/identity/entra/service-principals`.

Registration grants no role. Grant access separately to the registered group, user or service
principal (`principal_type: entra_group` with the group GUID, and so on; see
[First run](first-run.md#grant-access)). Principals are keyed by tenant id and object id, never by
email, UPN or `preferred_username`. An unregistered principal gets 403 `principal-not-registered`,
and identity stores nothing about it.

## 4. Operations

- **Cache and outage.** Directory answers are cached for 60 seconds per tenant and principal.
  Failures are never cached. Once a cached answer has expired, a Graph outage is a 503, never an
  admission from stale data.
- **Revocation lag.** Disabling or deleting an Entra user or service principal, or removing it
  from a registered group, takes effect within the cache window plus Graph replication delay, even
  though the access token is still valid. Continuous access evaluation does not apply to this API:
  there is no push revocation. Deactivating in Contractum (`POST .../deactivate`) is immediate.
- **Throttling.** Graph throttling answers 429 (with `Retry-After`), which identity treats as an
  outage. Watch for 503 bursts and for `graph_unexpected_status` or `graph_token_refused` in
  identity's logs. A 401 or 403 from Graph is the same 503: it usually means a missing or
  unconsented permission or an expired client secret. Rotate the client secret before it expires.
- **Reactivation.** A reactivated Entra user or service principal must present a token issued
  after the reactivation (whole-second precision). A token issued at or before it stays 401, so a
  daemon holding a cached token must get a new one. Entra may issue a token with a backdated
  `iat`, which is refused against the cutoff. If a fresh token is refused, request another one a
  second later.

### 4.1 Settings in the web app

**Administration > Settings > Entra** gives a Global admin three things without a redeploy:

- **Default tenant and default owner** (saved settings). With several tenants configured, a
  register call or directory search without `tenant_id` uses the default tenant, which must be one
  of the configured tenants. A tenant that is later removed from the issuers is ignored. The
  default owner (ROOT or an Organization) only pre-fills the register dialogs.
- **Overview** (view only, never stored or exported): the configured tenants, whether the Graph
  client and its secret are set, and the **client secret expiry**. Identity reads it with
  `GET /applications(appId='<graph client id>')?$select=passwordCredentials` and keeps only each
  credential's `endDateTime`. `Application.Read.All` covers this call. The expiry reads "unknown"
  when Graph refuses, or when the Graph app's home tenant is not one of the configured tenants; the
  page then shows a rotation reminder. It also warns when fewer than 30 days remain.
- **Test connection:** requests a Graph token and calls
  `GET /servicePrincipals?$filter=appId eq '<graph client id>'&$select=id&$top=1` (also covered by
  `Application.Read.All`), then answers one fixed code (`ok`, `not-configured`, `token-refused`,
  `graph-forbidden`, `graph-throttled`, `graph-unreachable` or `graph-error`). At most 6 per minute
  per admin. It changes nothing and is not audited.

Which tenants are trusted stays in Helm (`auth.issuers`). Nothing on the Settings page can change
token validation. See also [Runtime settings](settings.md).

## 5. Web UI sign-in (SPA app registration)

The web UI signs users in with MSAL Browser (authorization code with PKCE, full-page redirect, no
popups). For a real tenant:

1. **Register a SPA app** (or add the SPA platform to an existing public client). Add the redirect
   URI `https://<host>/redirect.html` under **Single-page application**, exactly, including any
   port. MSAL returns every flow to this bridge page, which then sends the browser back to
   `/auth/callback`. The `/auth/callback` route is not itself a redirect URI. Note the SPA's
   Application (client) ID: this is `<spa-client-id>`.
2. **API permission.** Give the SPA delegated access to the API app's scope (for example
   `api://<api-client-id>/Contracts.Access`) and grant admin consent, so the access token's `aud`
   is the API audience the issuer entry accepts.
3. **Configure the release** (public identifiers only):

   ```yaml
   ui:
     entra:
       enabled: true
       clientId: "<spa-client-id>"
       authority: https://login.microsoftonline.com/<tenant-id>
       scopes: "openid profile offline_access api://<api-client-id>/Contracts.Access"
       protocol: aad
   ```

   `ui.localLoginEnabled: false` hides the local sign-in form.
4. **Admission** is unchanged (see [Admission](#3-admission)): a user signs in only after they, or
   one of their groups, are registered. Otherwise the callback ends on a failure card with the
   API's `principal-not-registered` problem.
5. **Headers when you replace the UI's nginx.** The bridge page `/redirect.html` must be served
   with `frame-ancestors 'self'` and **no** `Cross-Origin-Opener-Policy` header (MSAL's silent
   renewal loads it in a hidden iframe). Every other page keeps `frame-ancestors 'none'` and COOP
   `same-origin`. The bundled `ui` image already does this.

MSAL keeps its cache in `sessionStorage` (per tab). The API access token itself lives only in
memory, as for local sign-in. A reload restores the session silently from that cache. A new tab
signs in again through Entra, usually without a prompt.

## 6. CLI and SDK

The `contractum` command line and Python library need no app registration. They sign in with a
token only:

- **People** (local or Entra users) create a personal access token in the web app (account menu,
  **Access tokens**) and store it with `contractum login --token -`.
- **CI with a service account:** a Contractum service-account key in `CONTRACTUM_TOKEN`.
- **CI with an Entra service principal:** its own access token for the Contractum API app in
  `CONTRACTUM_TOKEN`, for example from
  `az account get-access-token --scope api://<api-client-id>/.default`. Its app needs the API
  app's `Contracts.Access` application role (see [1.3](#13-clients-for-testing-and-pipelines)) and
  must be registered in Contractum with a role (see [Admission](#3-admission)). The token does not
  refresh, so fetch it in the job that uses it.

See [the SDK and CLI guide](../user/sdk-cli.md) for details.
